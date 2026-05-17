"""Tests for the new Director dispatch loop architecture."""

import json

import pytest

from app.core.director_tools import DIRECTOR_TOOLS, DIRECTOR_TOOL_CHOICE
from app.core.director_prompts import DIRECTOR_SYSTEM, SCOUT_SYSTEM, WORKER_SYSTEM
from app.core.world_model import WorldModel, TaskSummary, FailureRecord
from app.core.director_loop import _extract_structured_block, _extract_llm_text


class TestDirectorTools:
    def test_tool_schema_valid(self):
        assert len(DIRECTOR_TOOLS) == 1
        tool = DIRECTOR_TOOLS[0]
        assert tool["name"] == "decide"
        schema = tool["input_schema"]
        assert "action" in schema["properties"]
        assert set(schema["properties"]["action"]["enum"]) == {"scout", "worker", "test", "done", "failed"}
        assert "prompt" in schema["properties"]

    def test_tool_choice(self):
        assert DIRECTOR_TOOL_CHOICE["type"] == "function"
        assert DIRECTOR_TOOL_CHOICE["function"]["name"] == "decide"


class TestDirectorPrompts:
    def test_system_prompt_has_placeholders(self):
        formatted = DIRECTOR_SYSTEM.format(
            max_iterations=30,
            world_model="test world model",
        )
        assert "30" in formatted
        assert "test world model" in formatted

    def test_scout_system_has_markers(self):
        assert "===SCOUT_FINDINGS===" in SCOUT_SYSTEM
        assert "===END_SCOUT_FINDINGS===" in SCOUT_SYSTEM

    def test_worker_system_has_markers(self):
        assert "===WORKER_RESULT===" in WORKER_SYSTEM
        assert "===END_WORKER_RESULT===" in WORKER_SYSTEM


class TestWorldModel:
    def test_to_prompt_context_basic(self):
        model = WorldModel(goal="Build a TODO app")
        ctx = model.to_prompt_context()
        assert "Build a TODO app" in ctx
        assert "0/30" in ctx

    def test_record_success(self):
        model = WorldModel(goal="test")
        model.record_success("s1", "scout", "Found 3 files", ["a.py", "b.py"])
        assert len(model.completed_tasks) == 1
        assert model.completed_tasks[0].files_changed == ["a.py", "b.py"]

    def test_record_failure(self):
        model = WorldModel(goal="test")
        model.record_failure("w1", "worker", "Build error", prompt_hint="fix main.py")
        assert len(model.failed_attempts) == 1
        assert model.failed_attempts[0].prompt_hint == "fix main.py"

    def test_json_roundtrip(self):
        model = WorldModel(goal="test", project_structure="src/")
        model.record_success("s1", "scout", "ok")
        model.record_failure("w1", "worker", "err")
        json_str = model.to_json()
        restored = WorldModel.from_json(json_str)
        assert restored.goal == "test"
        assert restored.project_structure == "src/"
        assert len(restored.completed_tasks) == 1
        assert len(restored.failed_attempts) == 1

    def test_prompt_context_keeps_limits(self):
        model = WorldModel(goal="test")
        for i in range(20):
            model.record_success(f"t{i}", "worker", f"task {i}")
        ctx = model.to_prompt_context()
        # Should only include last 15 tasks
        lines = [l for l in ctx.split("\n") if l.strip().startswith("[+]")]
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

    def test_skips_non_text(self):
        jsonl = '{"type":"shell_stdout","content":"$ ls"}\n{"type":"llm_token","content":"hi"}'
        assert _extract_llm_text(jsonl) == "hi"

    def test_empty_input(self):
        assert _extract_llm_text("") == ""
