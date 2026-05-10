"""Tests for tool_repair.repair_tool_call."""
from __future__ import annotations

import json

import pytest

from mas_agent.tool_repair import repair_tool_call


class TestToolNameFix:
    """(a) Tool-name casing / alias fixes."""

    @pytest.mark.parametrize(
        ("input_name", "expected_name"),
        [
            ("Grep", "grep"),
            ("ReadFile", "read"),
            ("Readfile", "read"),
            ("WriteFile", "write"),
            ("EditFile", "edit"),
            ("Shell", "shell"),
            ("Glob", "glob"),
            ("Write", "write"),
            ("Edit", "edit"),
            ("Read", "read"),
        ],
    )
    def test_known_alias_corrected(self, input_name: str, expected_name: str) -> None:
        name, _, repairs = repair_tool_call(input_name, {})
        assert name == expected_name
        assert len(repairs) >= 1
        assert any("tool name" in r for r in repairs)

    def test_lowercase_name_unchanged(self) -> None:
        name, args, repairs = repair_tool_call("grep", {"pattern": "hello"})
        assert name == "grep"
        assert args == {"pattern": "hello"}
        assert repairs == []


class TestParamNameFix:
    """(b) Parameter-name alias fixes."""

    def test_file_path_to_path_for_read(self) -> None:
        _, args, repairs = repair_tool_call("read", {"file_path": "main.py"})
        assert "path" in args
        assert args["path"] == "main.py"
        assert "file_path" not in args
        assert any("param" in r and "file_path" in r for r in repairs)

    def test_file_path_to_path_for_grep(self) -> None:
        _, args, repairs = repair_tool_call("grep", {"query": "TODO", "file_path": "src/"})
        assert args["pattern"] == "TODO"
        assert args["path"] == "src/"
        assert len(repairs) == 2

    def test_content_to_new_text_for_edit(self) -> None:
        _, args, repairs = repair_tool_call("edit", {
            "file_path": "a.py",
            "text": "old",
            "content": "new",
        })
        assert args["path"] == "a.py"
        assert args["old_text"] == "old"
        assert args["new_text"] == "new"

    def test_search_to_pattern_for_grep(self) -> None:
        _, args, repairs = repair_tool_call("grep", {"search": "def "})
        assert args["pattern"] == "def "
        assert any("param" in r for r in repairs)

    def test_file_to_path_for_read(self) -> None:
        _, args, repairs = repair_tool_call("read", {"file": "hello.py"})
        assert args["path"] == "hello.py"

    def test_correct_key_not_overwritten(self) -> None:
        """If both alias and correct key are present, keep the correct one."""
        _, args, repairs = repair_tool_call("read", {"path": "a.py", "file_path": "b.py"})
        assert args["path"] == "a.py"

    def test_cmd_to_command_for_shell(self) -> None:
        _, args, repairs = repair_tool_call("shell", {"cmd": "ls -la"})
        assert args["command"] == "ls -la"


class TestJsonFix:
    """(c) JSON formatting fixes in string argument values."""

    def test_trailing_comma_removed(self) -> None:
        _, args, repairs = repair_tool_call("shell", {
            "command": '{"path": "a",}',
        })
        parsed = json.loads(args["command"])
        assert parsed == {"path": "a"}
        assert any("trailing comma" in r for r in repairs)

    def test_single_quotes_replaced(self) -> None:
        _, args, repairs = repair_tool_call("shell", {
            "command": "{'path': 'a'}",
        })
        parsed = json.loads(args["command"])
        assert parsed == {"path": "a"}
        assert any("single quotes" in r for r in repairs)

    def test_truncated_json_no_crash(self) -> None:
        """Truncated JSON should not crash — best-effort repair."""
        _, args, repairs = repair_tool_call("shell", {
            "command": '{"path": "main.py",',
        })
        # Just verify it didn't crash — the repair may or may not succeed
        assert isinstance(args["command"], str)

    def test_non_json_string_untouched(self) -> None:
        _, args, repairs = repair_tool_call("shell", {
            "command": "echo hello world",
        })
        assert args["command"] == "echo hello world"
        # No JSON repair should be applied to a plain string
        assert not any("json_fix" in r for r in repairs)


class TestTypeConversion:
    """(d) String-to-int conversion for numeric fields."""

    def test_offset_converted_for_read(self) -> None:
        _, args, repairs = repair_tool_call("read", {"path": "a.py", "offset": "10"})
        assert args["offset"] == 10
        assert isinstance(args["offset"], int)
        assert any("type_fix" in r for r in repairs)

    def test_limit_converted_for_read(self) -> None:
        _, args, repairs = repair_tool_call("read", {"path": "a.py", "limit": "50"})
        assert args["limit"] == 50
        assert isinstance(args["limit"], int)

    def test_timeout_converted_for_shell(self) -> None:
        _, args, repairs = repair_tool_call("shell", {"command": "sleep 1", "timeout": "30"})
        assert args["timeout"] == 30
        assert isinstance(args["timeout"], int)

    def test_context_converted_for_grep(self) -> None:
        _, args, repairs = repair_tool_call("grep", {"pattern": "hi", "context": "3"})
        assert args["context"] == 3
        assert isinstance(args["context"], int)

    def test_non_numeric_string_not_converted(self) -> None:
        _, args, repairs = repair_tool_call("read", {"path": "a.py", "offset": "abc"})
        assert args["offset"] == "abc"
        assert not any("type_fix" in r and "offset" in r for r in repairs)


class TestInvalidToolName:
    """Edge case: unknown tool name should not crash."""

    def test_nonexistent_tool_no_crash(self) -> None:
        name, args, repairs = repair_tool_call("nonexistent_tool", {"foo": "bar"})
        assert name == "nonexistent_tool"
        assert args == {"foo": "bar"}
        # No repairs expected — name not in aliases
        assert repairs == []


class TestNoRepairNeeded:
    """Normal inputs should pass through unchanged."""

    def test_correct_read_call(self) -> None:
        name, args, repairs = repair_tool_call("read", {"path": "main.py", "offset": 0})
        assert name == "read"
        assert args == {"path": "main.py", "offset": 0}
        assert repairs == []

    def test_correct_grep_call(self) -> None:
        name, args, repairs = repair_tool_call("grep", {"pattern": "TODO", "path": "src/"})
        assert name == "grep"
        assert args == {"pattern": "TODO", "path": "src/"}
        assert repairs == []

    def test_correct_edit_call(self) -> None:
        name, args, repairs = repair_tool_call("edit", {
            "path": "a.py",
            "old_text": "foo",
            "new_text": "bar",
        })
        assert name == "edit"
        assert args == {"path": "a.py", "old_text": "foo", "new_text": "bar"}
        assert repairs == []

    def test_empty_args(self) -> None:
        name, args, repairs = repair_tool_call("glob", {})
        assert name == "glob"
        assert args == {}
        assert repairs == []


class TestMultipleRepairs:
    """Multiple repairs applied in a single call."""

    def test_name_and_param_and_type(self) -> None:
        name, args, repairs = repair_tool_call("ReadFile", {"file_path": "a.py", "limit": "20"})
        assert name == "read"
        assert args["path"] == "a.py"
        assert args["limit"] == 20
        assert isinstance(args["limit"], int)
        # Should have at least 3 repairs: name, param, type
        assert len(repairs) >= 3

    def test_non_dict_arguments_handled(self) -> None:
        """If arguments is not a dict, it should be treated as empty."""
        name, args, repairs = repair_tool_call("grep", None)  # type: ignore[arg-type]
        assert name == "grep"
        assert args == {}
        assert len(repairs) == 0
