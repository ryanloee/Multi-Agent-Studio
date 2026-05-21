"""Tests for Planner-driven dynamic execution loop."""

from app.core.director_loop import (
    _build_node_pool,
    _extract_llm_text,
    _extract_structured_block,
    _format_available_nodes,
    _pick_node_id,
)
from app.core.director_prompts import (
    PLANNER_DIRECTOR_SYSTEM,
    PLANNER_REVIEW_SYSTEM,
    SCOUT_SYSTEM,
    WORKER_SYSTEM,
)
from app.core.director_tools import (
    DECIDE_TOOL,
    DECIDE_TOOL_NAME,
    PLANNER_TOOLS,
    REVIEW_TOOL,
    REVIEW_TOOL_NAME,
)
from app.core.world_model import WorldModel


class TestPlannerTools:
    def test_decide_tool_schema(self):
        assert DECIDE_TOOL["name"] == DECIDE_TOOL_NAME
        schema = DECIDE_TOOL["input_schema"]
        assert set(schema["properties"]["action"]["enum"]) == {
            "explore", "coder", "shell", "done", "failed",
        }

    def test_review_tool_schema(self):
        assert REVIEW_TOOL["name"] == REVIEW_TOOL_NAME
        schema = REVIEW_TOOL["input_schema"]
        assert set(schema["properties"]["result"]["enum"]) == {"pass", "reject"}

    def test_planner_tools_has_both(self):
        names = {t["name"] for t in PLANNER_TOOLS}
        assert names == {DECIDE_TOOL_NAME, REVIEW_TOOL_NAME}


class TestNodePool:
    def test_build_pool_groups_by_type(self):
        dag = {"nodes": [
            {"id": "A", "data": {"agentType": "explore"}},
            {"id": "B", "data": {"agentType": "coder"}},
            {"id": "C", "data": {"agentType": "coder"}},
            {"id": "D", "data": {"agentType": "shell"}},
        ]}
        pool = _build_node_pool(dag)
        assert [n["id"] for n in pool["explore"]] == ["A"]
        assert [n["id"] for n in pool["coder"]] == ["B", "C"]
        assert [n["id"] for n in pool["shell"]] == ["D"]

    def test_pick_node_id_prefers_match(self):
        pool = {"explore": [{"id": "E1"}], "coder": [{"id": "C1"}, {"id": "C2"}]}
        assert _pick_node_id(pool, "coder") == "C1"
        assert _pick_node_id(pool, "explore") == "E1"

    def test_pick_node_id_fallback(self):
        pool = {"coder": [{"id": "C1"}]}
        assert _pick_node_id(pool, "explore") == "C1"

    def test_format_available_nodes(self):
        pool = {"explore": [{"id": "E1"}], "coder": [{"id": "C1"}]}
        text = _format_available_nodes(pool)
        assert "explore" in text
        assert "coder" in text

    def test_format_empty_pool(self):
        text = _format_available_nodes({})
        assert "No DAG nodes" in text


class TestPrompts:
    def test_planner_director_has_placeholders(self):
        formatted = PLANNER_DIRECTOR_SYSTEM.format(
            world_model="test goal",
            available_nodes="- coder: C1",
        )
        assert "test goal" in formatted
        assert "coder: C1" in formatted
        assert "decide" in formatted

    def test_scout_system_has_markers(self):
        assert "===SCOUT_FINDINGS===" in SCOUT_SYSTEM

    def test_worker_system_has_markers(self):
        assert "===WORKER_RESULT===" in WORKER_SYSTEM

    def test_planner_review_mentions_review(self):
        assert "review" in PLANNER_REVIEW_SYSTEM


class TestWorldModel:
    def test_json_roundtrip(self):
        model = WorldModel(goal="test", project_structure="src/")
        model.record_success("s1", "scout", "ok")
        model.record_failure("w1", "worker", "err")
        model.iteration = 5
        model.last_node_id = "C1"
        json_str = model.to_json()
        restored = WorldModel.from_json(json_str)
        assert restored.goal == "test"
        assert restored.iteration == 5
        assert restored.last_node_id == "C1"
        assert len(restored.completed_tasks) == 1
        assert len(restored.failed_attempts) == 1


class TestStructuredBlockExtraction:
    def test_extract_scout_findings(self):
        text = '===SCOUT_FINDINGS===\n{"files_found": ["a.py"]}\n===END_SCOUT_FINDINGS==='
        result = _extract_structured_block(text, "SCOUT_FINDINGS")
        assert result["files_found"] == ["a.py"]

    def test_missing_block_returns_none(self):
        assert _extract_structured_block("no block", "SCOUT_FINDINGS") is None


class TestExtractLlmText:
    def test_extracts_tokens(self):
        jsonl = '{"type":"llm_token","content":"hello"}\n{"type":"llm_token","content":" world"}'
        assert _extract_llm_text(jsonl) == "hello world"
