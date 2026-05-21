"""Tests for the DirectorLoop serial DAG executor.

The Director is rule-based (no LLM). Planner review uses tool-use.
"""

from app.core.director_loop import _extract_llm_text, _extract_structured_block
from app.core.director_prompts import (
    MERGER_SYSTEM,
    PLANNER_REVIEW_SYSTEM,
    SCOUT_SYSTEM,
    TESTER_SYSTEM,
    WORKER_SYSTEM,
)
from app.core.director_tools import (
    MAX_REVIEW_RETRIES,
    REVIEW_TOOL,
    REVIEW_TOOL_CHOICE_OPENAI,
    REVIEW_TOOL_OPENAI,
)
from app.core.world_model import WorldModel


class TestPlannerReviewTools:
    def test_review_tool_schema_valid(self):
        assert REVIEW_TOOL["name"] == "review"
        schema = REVIEW_TOOL["input_schema"]
        assert schema["type"] == "object"
        assert set(schema["properties"]["result"]["enum"]) == {"pass", "reject"}
        assert set(schema["required"]) == {"result", "reason"}

    def test_openai_tool_wrapper(self):
        assert REVIEW_TOOL_OPENAI["type"] == "function"
        assert REVIEW_TOOL_OPENAI["function"]["name"] == "review"
        assert REVIEW_TOOL_CHOICE_OPENAI["type"] == "function"
        assert REVIEW_TOOL_CHOICE_OPENAI["function"]["name"] == "review"

    def test_review_retry_limit(self):
        assert MAX_REVIEW_RETRIES >= 1


class TestPrompts:
    def test_scout_system_has_markers(self):
        assert "===SCOUT_FINDINGS===" in SCOUT_SYSTEM
        assert "===END_SCOUT_FINDINGS===" in SCOUT_SYSTEM

    def test_worker_system_has_markers(self):
        assert "===WORKER_RESULT===" in WORKER_SYSTEM
        assert "===END_WORKER_RESULT===" in WORKER_SYSTEM

    def test_tester_system_has_markers(self):
        assert "===WORKER_RESULT===" in TESTER_SYSTEM
        assert "===END_WORKER_RESULT===" in TESTER_SYSTEM

    def test_merger_system_has_markers(self):
        assert "===WORKER_RESULT===" in MERGER_SYSTEM
        assert "===END_WORKER_RESULT===" in MERGER_SYSTEM

    def test_planner_review_prompt_mentions_review_tool(self):
        assert "review" in PLANNER_REVIEW_SYSTEM


class TestWorldModel:
    def test_to_prompt_context_basic(self):
        model = WorldModel(goal="Build a TODO app")
        ctx = model.to_prompt_context()
        assert "Build a TODO app" in ctx

    def test_record_success(self):
        model = WorldModel(goal="test")
        model.record_success("s1", "scout", "Found 3 files", ["a.py", "b.py"])
        assert len(model.completed_tasks) == 1
        assert model.completed_tasks[0].files_changed == ["a.py", "b.py"]
        assert model.node_statuses["s1"] == "completed"

    def test_record_failure(self):
        model = WorldModel(goal="test")
        model.record_failure("w1", "worker", "Build error", prompt_hint="fix main.py")
        assert len(model.failed_attempts) == 1
        assert model.failed_attempts[0].prompt_hint == "fix main.py"
        assert model.node_statuses["w1"] == "failed"

    def test_json_roundtrip(self):
        model = WorldModel(goal="test", project_structure="src/")
        model.record_success("s1", "scout", "ok")
        model.record_failure("w1", "worker", "err")
        model.current_node_index = 2
        model.node_queue = [{"id": "A"}, {"id": "B"}]
        json_str = model.to_json()
        restored = WorldModel.from_json(json_str)
        assert restored.goal == "test"
        assert restored.project_structure == "src/"
        assert len(restored.completed_tasks) == 1
        assert len(restored.failed_attempts) == 1
        assert restored.current_node_index == 2
        assert restored.node_queue == [{"id": "A"}, {"id": "B"}]

    def test_prompt_context_keeps_limits(self):
        model = WorldModel(goal="test")
        for i in range(20):
            model.record_success(f"t{i}", "worker", f"task {i}")
        ctx = model.to_prompt_context()
        # Should only include last 15 tasks
        lines = [line for line in ctx.split("\n") if line.strip().startswith("[+]")]
        assert len(lines) <= 15


class TestStructuredBlockExtraction:
    def test_extract_scout_findings(self):
        text = """Some output here
===SCOUT_FINDINGS===
{"files_found": ["a.py"], "summary": "found files"}
===END_SCOUT_FINDINGS===
"""
        result = _extract_structured_block(text, "SCOUT_FINDINGS")
        assert result is not None
        assert result["files_found"] == ["a.py"]

    def test_extract_worker_result(self):
        text = """Working...
===WORKER_RESULT===
{"files_changed": ["main.py"], "summary": "fixed bug", "tests_passed": 3}
===END_WORKER_RESULT===
"""
        result = _extract_structured_block(text, "WORKER_RESULT")
        assert result is not None
        assert result["files_changed"] == ["main.py"]
        assert result["tests_passed"] == 3

    def test_missing_block_returns_none(self):
        result = _extract_structured_block("no block here", "SCOUT_FINDINGS")
        assert result is None

    def test_malformed_json_fallback(self):
        text = """===SCOUT_FINDINGS===
{broken json here}
===END_SCOUT_FINDINGS===
"""
        result = _extract_structured_block(text, "SCOUT_FINDINGS")
        assert result is None


class TestExtractLlmText:
    def test_extracts_tokens(self):
        jsonl = '{"type":"llm_token","content":"hello"}\n{"type":"llm_token","content":" world"}'
        assert _extract_llm_text(jsonl) == "hello world"

    def test_includes_shell_and_text(self):
        jsonl = '{"type":"shell_stdout","content":"$ ls"}\n{"type":"llm_token","content":"hi"}'
        assert _extract_llm_text(jsonl) == "\n[Shell Output]\n$ ls\nhi"

    def test_empty_input(self):
        assert _extract_llm_text("") == ""


class TestCheckpointRoundtrip:
    def test_world_model_json_roundtrip(self):
        model = WorldModel(goal="Build app", project_structure="src/")
        model.record_success("s1", "scout", "Found files", ["a.py", "b.py"])
        model.record_failure("w1", "worker", "Build error", prompt_hint="fix main.py")
        model.current_node_index = 5
        model.current_file_snapshot = "M src/main.py"

        json_str = model.to_json()
        restored = WorldModel.from_json(json_str)

        assert restored.goal == "Build app"
        assert restored.project_structure == "src/"
        assert restored.current_node_index == 5
        assert restored.current_file_snapshot == "M src/main.py"
        assert len(restored.completed_tasks) == 1
        assert restored.completed_tasks[0].files_changed == ["a.py", "b.py"]
        assert len(restored.failed_attempts) == 1
        assert restored.failed_attempts[0].prompt_hint == "fix main.py"

    def test_checkpoint_data_structure(self):
        model = WorldModel(goal="test")
        model.current_node_index = 3
        json_str = model.to_json()

        checkpoint = {
            "world_model_json": json_str,
            "sandbox_id": "ws-director-abc12345",
            "global_config": {"_goal": "test"},
            "workspace_directory": "/tmp/workspace",
            "dag_json": {"nodes": [], "edges": []},
            "checkpoint_iteration": 3,
        }

        assert checkpoint["sandbox_id"] == "ws-director-abc12345"
        restored = WorldModel.from_json(checkpoint["world_model_json"])
        assert restored.current_node_index == 3
