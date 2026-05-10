"""Tests for LocalDAGExecutor helper methods and dual-mode workflow features.

Focuses on _build_upstream_context, _extract_llm_text, parse_plan_to_dag
integration, and edge transfer attributes.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.local_engine import LocalDAGExecutor
from app.workflows.plan_parser import parse_plan_to_dag


# ---------------------------------------------------------------------------
# Fixtures: mock the four constructor dependencies
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_sandbox():
    sandbox = AsyncMock()
    sandbox.create = AsyncMock(return_value="sandbox-1234")
    sandbox.destroy = AsyncMock()
    sandbox.exec = AsyncMock(return_value=("", 0))
    sandbox.exec_async = AsyncMock(return_value="exec-5678")
    sandbox.write_file = AsyncMock()
    sandbox.get_process = MagicMock()
    sandbox.clone = AsyncMock(return_value="cloned-9012")
    return sandbox


@pytest.fixture()
def mock_event_bus():
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture()
def mock_checkpoint():
    cp = AsyncMock()
    cp.auto_commit = AsyncMock(return_value="abc123")
    return cp


@pytest.fixture()
def mock_provisioner():
    return AsyncMock()


@pytest.fixture()
def engine(mock_sandbox, mock_event_bus, mock_checkpoint, mock_provisioner):
    """Create a LocalDAGExecutor with all mocked dependencies."""
    return LocalDAGExecutor(
        sandbox=mock_sandbox,
        event_bus=mock_event_bus,
        checkpoint=mock_checkpoint,
        provisioner=mock_provisioner,
    )


# ===========================================================================
# _extract_llm_text
# ===========================================================================

class TestExtractLlmText:
    """Tests for the static _extract_llm_text helper."""

    def test_extracts_llm_tokens(self):
        lines = [
            json.dumps({"type": "llm_token", "content": "Hello "}),
            json.dumps({"type": "llm_token", "content": "world"}),
        ]
        result = LocalDAGExecutor._extract_llm_text("\n".join(lines))
        assert result == "Hello world"

    def test_extracts_llm_chunks(self):
        lines = [
            json.dumps({"type": "llm_chunk", "content": "chunk1"}),
            json.dumps({"type": "llm_chunk", "content": " chunk2"}),
        ]
        result = LocalDAGExecutor._extract_llm_text("\n".join(lines))
        assert result == "chunk1 chunk2"

    def test_mixed_event_types(self):
        lines = [
            json.dumps({"type": "llm_token", "content": "text "}),
            json.dumps({"type": "tool_call", "content": "ignored"}),
            json.dumps({"type": "text", "content": "here"}),
        ]
        result = LocalDAGExecutor._extract_llm_text("\n".join(lines))
        assert result == "text here"

    def test_empty_input(self):
        assert LocalDAGExecutor._extract_llm_text("") == ""
        assert LocalDAGExecutor._extract_llm_text("  ") == ""

    def test_skips_non_json_lines(self):
        lines = "some plain text\nnot json at all"
        assert LocalDAGExecutor._extract_llm_text(lines) == ""

    def test_unknown_event_type_ignored(self):
        lines = json.dumps({"type": "shell_stdout", "content": "noise"})
        assert LocalDAGExecutor._extract_llm_text(lines) == ""

    def test_preserves_contiguous_tokens(self):
        """Tokens are joined with empty string, no extra newlines injected."""
        lines = [
            json.dumps({"type": "llm_token", "content": "def "}),
            json.dumps({"type": "llm_token", "content": "foo():\n"}),
            json.dumps({"type": "llm_token", "content": "    pass"}),
        ]
        result = LocalDAGExecutor._extract_llm_text("\n".join(lines))
        assert result == "def foo():\n    pass"


# ===========================================================================
# _build_upstream_context
# ===========================================================================

class TestBuildUpstreamContext:
    """Tests for _build_upstream_context method."""

    def test_with_result_summary(self, engine):
        """When upstream has result_summary, use it."""
        edges = [{"source": "node_a", "target": "node_b"}]
        layer_results = {
            "node_a": {
                "result_summary": "Completed analysis successfully",
                "raw_output": "",
            }
        }
        result = engine._build_upstream_context("node_b", edges, layer_results)
        assert "node_a" in result
        assert "Completed analysis successfully" in result
        assert "## 上游节点输出" in result

    def test_with_raw_output_fallback(self, engine):
        """When upstream has no result_summary, fall back to raw_output."""
        raw_jsonl = "\n".join([
            json.dumps({"type": "llm_token", "content": "Some LLM output"}),
        ])
        edges = [{"source": "node_a", "target": "node_b"}]
        layer_results = {
            "node_a": {
                "result_summary": "",
                "raw_output": raw_jsonl,
            }
        }
        result = engine._build_upstream_context("node_b", edges, layer_results)
        assert "node_a" in result
        assert "Some LLM output" in result

    def test_no_upstream_edges(self, engine):
        """No upstream edges returns empty string."""
        edges = [{"source": "node_a", "target": "node_c"}]
        layer_results = {"node_a": {"result_summary": "data"}}
        result = engine._build_upstream_context("node_b", edges, layer_results)
        assert result == ""

    def test_empty_edges_list(self, engine):
        """Empty edges list returns empty string."""
        result = engine._build_upstream_context("node_a", [], {})
        assert result == ""

    def test_multiple_upstream_nodes(self, engine):
        """Multiple upstream nodes are all included."""
        edges = [
            {"source": "node_a", "target": "node_c"},
            {"source": "node_b", "target": "node_c"},
        ]
        layer_results = {
            "node_a": {"result_summary": "Result from A"},
            "node_b": {"result_summary": "Result from B"},
        }
        result = engine._build_upstream_context("node_c", edges, layer_results)
        assert "node_a" in result
        assert "Result from A" in result
        assert "node_b" in result
        assert "Result from B" in result

    def test_missing_source_result(self, engine):
        """Edge pointing to a source with no result produces no section."""
        edges = [{"source": "missing_node", "target": "node_b"}]
        layer_results = {}
        result = engine._build_upstream_context("node_b", edges, layer_results)
        assert result == ""

    def test_empty_source_id_skipped(self, engine):
        """Edge with empty source is skipped."""
        edges = [{"source": "", "target": "node_b"}]
        layer_results = {"": {"result_summary": "should not appear"}}
        result = engine._build_upstream_context("node_b", edges, layer_results)
        assert result == ""

    def test_non_dict_source_result_skipped(self, engine):
        """Non-dict source result is skipped gracefully."""
        edges = [{"source": "node_a", "target": "node_b"}]
        layer_results = {"node_a": "not a dict"}
        result = engine._build_upstream_context("node_b", edges, layer_results)
        assert result == ""

    def test_raw_output_truncated_to_2000_chars(self, engine):
        """Raw output is truncated to last 2000 characters when used as fallback."""
        long_text = "x" * 3000
        raw_jsonl = json.dumps({"type": "llm_token", "content": long_text})
        edges = [{"source": "node_a", "target": "node_b"}]
        layer_results = {
            "node_a": {
                "result_summary": "",
                "raw_output": raw_jsonl,
            }
        }
        result = engine._build_upstream_context("node_b", edges, layer_results)
        # The summary should contain at most 2000 chars of the extracted text
        assert len(result) < 3000  # much less than 3000 raw chars
        assert long_text[-2000:] in result or long_text[:2000] in result


# ===========================================================================
# parse_plan_to_dag integration
# ===========================================================================

class TestParsePlanToDag:
    """Integration tests for parse_plan_to_dag used in dual-mode execution."""

    def test_linear_chain(self):
        """Linear chain: A -> B -> C produces correct nodes and edges."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "type": "coder", "prompt": "do a", "depends_on": []},
                {"id": "b", "type": "coder", "prompt": "do b", "depends_on": ["a"]},
                {"id": "c", "type": "review", "prompt": "review", "depends_on": ["b"]},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is not None
        nodes, edges = result
        assert len(nodes) == 3
        assert len(edges) == 2
        assert edges[0]["source"] == "a"
        assert edges[0]["target"] == "b"
        assert edges[1]["source"] == "b"
        assert edges[1]["target"] == "c"

    def test_diamond(self):
        """Diamond: A -> B, A -> C, B -> D, C -> D produces correct structure."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "type": "explore", "prompt": "explore", "depends_on": []},
                {"id": "b", "type": "coder", "prompt": "code b", "depends_on": ["a"]},
                {"id": "c", "type": "coder", "prompt": "code c", "depends_on": ["a"]},
                {"id": "d", "type": "review", "prompt": "review all", "depends_on": ["b", "c"]},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is not None
        nodes, edges = result
        assert len(nodes) == 4
        assert len(edges) == 4
        # Verify all expected edges exist
        edge_pairs = {(e["source"], e["target"]) for e in edges}
        assert ("a", "b") in edge_pairs
        assert ("a", "c") in edge_pairs
        assert ("b", "d") in edge_pairs
        assert ("c", "d") in edge_pairs

    def test_missing_field_returns_none(self):
        """Missing required field returns None."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "type": "coder", "prompt": "do a"},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is None

    def test_missing_id_returns_none(self):
        """Missing 'id' field returns None."""
        plan = json.dumps({
            "tasks": [
                {"type": "coder", "prompt": "do a", "depends_on": []},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is None

    def test_missing_type_returns_none(self):
        """Missing 'type' field returns None."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "prompt": "do a", "depends_on": []},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is None

    def test_missing_prompt_returns_none(self):
        """Missing 'prompt' field returns None."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "type": "coder", "depends_on": []},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is None

    def test_non_dict_task_returns_none(self):
        """Non-dict task entry returns None."""
        plan = json.dumps({
            "tasks": ["not a dict"],
        })
        result = parse_plan_to_dag(plan)
        assert result is None

    def test_no_tasks_key_returns_none(self):
        """JSON without 'tasks' key returns None."""
        plan = json.dumps({"something": "else"})
        result = parse_plan_to_dag(plan)
        assert result is None

    def test_in_markdown_code_block(self):
        """Plan wrapped in markdown JSON code block is parsed correctly."""
        plan = '```json\n{"tasks": [{"id": "s1", "type": "coder", "prompt": "code", "depends_on": []}]}\n```'
        result = parse_plan_to_dag(plan)
        assert result is not None
        nodes, edges = result
        assert len(nodes) == 1
        assert nodes[0]["id"] == "s1"

    def test_node_data_fields(self):
        """Parsed nodes contain proper data fields."""
        plan = json.dumps({
            "tasks": [
                {"id": "t1", "type": "coder", "prompt": "implement feature", "depends_on": []},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is not None
        nodes, _ = result
        node = nodes[0]
        assert node["id"] == "t1"
        assert node["data"]["agent_type"] == "coder"
        assert node["data"]["prompt"] == "implement feature"
        assert node["data"]["label"] == "t1"

    def test_edge_ids_generated(self):
        """Edge IDs follow the 'e_{source}_{target}' pattern."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "type": "coder", "prompt": "a", "depends_on": []},
                {"id": "b", "type": "coder", "prompt": "b", "depends_on": ["a"]},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is not None
        _, edges = result
        assert edges[0]["id"] == "e_a_b"

    def test_parallel_tasks_no_edges(self):
        """Tasks with no dependencies produce nodes but no edges."""
        plan = json.dumps({
            "tasks": [
                {"id": "a", "type": "coder", "prompt": "a", "depends_on": []},
                {"id": "b", "type": "coder", "prompt": "b", "depends_on": []},
            ]
        })
        result = parse_plan_to_dag(plan)
        assert result is not None
        nodes, edges = result
        assert len(nodes) == 2
        assert len(edges) == 0


# ===========================================================================
# Engine public API helpers
# ===========================================================================

class TestEnginePublicAPI:
    """Tests for the engine's public methods with mocked internals."""

    @pytest.mark.asyncio
    async def test_start_workflow_creates_run(self, engine):
        """start_workflow should register a run with 'running' status."""
        layers = [[{"id": "n1", "type": "coder", "data": {"prompt": "test"}}]]
        run_id = await engine.start_workflow("run-001", layers, {"_edges": []})
        assert run_id == "run-001"
        status = await engine.get_status("run-001")
        assert status["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_status_unknown(self, engine):
        """Unknown run_id returns status 'unknown'."""
        status = await engine.get_status("nonexistent")
        assert status["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_cancel_running_workflow(self, engine):
        """Cancel sets the cancel_event and status to 'cancelling'."""
        layers = [[{"id": "n1", "type": "coder", "data": {"prompt": "test"}}]]
        await engine.start_workflow("run-002", layers, {"_edges": []})
        await engine.cancel("run-002")
        status = await engine.get_status("run-002")
        assert status["status"] == "cancelling"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_run(self, engine):
        """Cancelling a non-existent run does not raise."""
        await engine.cancel("no-such-run")  # should not raise

    @pytest.mark.asyncio
    async def test_cancel_completed_run_noop(self, engine):
        """Cancelling a completed run does nothing."""
        layers = [[{"id": "n1", "type": "coder", "data": {"prompt": "test"}}]]
        await engine.start_workflow("run-003", layers, {"_edges": []})
        # Manually set to completed
        engine._runs["run-003"]["status"] = "completed"
        await engine.cancel("run-003")
        status = await engine.get_status("run-003")
        assert status["status"] == "completed"
