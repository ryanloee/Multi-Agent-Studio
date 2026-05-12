"""Tests for ToolRegistry.validate_execution — agent-type-specific constraint checks.

Covers coarse permission (allowed_agent_types), fine-grained parameter checks
(review edit limits, planner file-type restrictions, shell code-file restrictions),
and edge cases like unknown tools.
"""

from __future__ import annotations

from typing import Any

import pytest

from mas_agent.tools import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# Lightweight stub tools for isolated tests
# ---------------------------------------------------------------------------

class _StubTool(Tool):
    """Minimal concrete Tool for testing."""

    def __init__(
        self,
        name: str = "stub",
        allowed_agent_types: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = f"Stub tool {name}"
        self.input_schema: dict[str, Any] = {}
        self.allowed_agent_types = allowed_agent_types

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        return "ok"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot and restore the global registry around each test."""
    saved = dict(ToolRegistry._tools) if ToolRegistry._tools else None
    ToolRegistry.reset()
    yield
    ToolRegistry._tools = saved


def _register_real_tools():
    """Register tools that mirror the real set with allowed_agent_types."""
    # Tools available to all (allowed_agent_types=None)
    ToolRegistry.register(_StubTool("glob"))
    ToolRegistry.register(_StubTool("read"))
    ToolRegistry.register(_StubTool("grep"))

    # Restricted tools — matching the real registration
    ToolRegistry.register(_StubTool("edit", allowed_agent_types=["plan", "coder", "merge", "review", "shell"]))
    ToolRegistry.register(_StubTool("write", allowed_agent_types=["plan", "coder", "merge", "shell"]))
    ToolRegistry.register(_StubTool("shell", allowed_agent_types=["plan", "coder", "merge", "shell"]))
    ToolRegistry.register(_StubTool("apply_patch", allowed_agent_types=["plan", "coder", "merge"]))


# ===========================================================================
# Test class — mirrors the user-specified test cases
# ===========================================================================

class TestValidateExecution:
    """Validate tool execution against agent-type constraints."""

    # ------------------------------------------------------------------
    # Explorer (explore) — only read-only tools
    # ------------------------------------------------------------------

    def test_explorer_cannot_edit(self):
        """Explorer using edit tool should be blocked."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("explore", "edit", {"path": "/workspace/test.py"})
        assert any("Permission denied" in w for w in warnings)

    def test_explorer_cannot_write(self):
        """Explorer using write tool should be blocked."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("explore", "write", {"path": "/workspace/test.py"})
        assert any("Permission denied" in w for w in warnings)

    def test_explorer_cannot_shell(self):
        """Explorer using shell tool should be blocked."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("explore", "shell", {"command": "ls"})
        assert any("Permission denied" in w for w in warnings)

    def test_explorer_can_read(self):
        """Explorer can use read-only tools."""
        _register_real_tools()
        for tool_name in ("read", "glob", "grep"):
            warnings = ToolRegistry.validate_execution("explore", tool_name, {"path": "/workspace/test.py"})
            assert not any("Permission denied" in w for w in warnings), (
                f"explore should be allowed to use {tool_name}"
            )

    # ------------------------------------------------------------------
    # Human — no tools at all
    # ------------------------------------------------------------------

    def test_human_no_write_tools(self):
        """Human using restricted tools should be blocked."""
        _register_real_tools()
        for tool_name in ("edit", "write", "shell"):
            warnings = ToolRegistry.validate_execution("human", tool_name, {})
            assert any("Permission denied" in w for w in warnings), (
                f"human should not use {tool_name}"
            )

    def test_human_no_apply_patch(self):
        """Human cannot use apply_patch."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("human", "apply_patch", {})
        assert any("Permission denied" in w for w in warnings)

    def test_human_readonly_tools_allowed(self):
        """Human can technically use read-only tools (allowed_agent_types=None).

        Note: for_agent_type("human") returns [] so the frontend never sends
        these tools, but validate_execution's coarse check only looks at
        allowed_agent_types on the Tool, which is None for read/glob/grep.
        """
        _register_real_tools()
        for tool_name in ("read", "glob", "grep"):
            warnings = ToolRegistry.validate_execution("human", tool_name, {})
            assert not any("Permission denied" in w for w in warnings), (
                f"human coarse check on {tool_name}"
            )

    # ------------------------------------------------------------------
    # Reviewer (review)
    # ------------------------------------------------------------------

    def test_reviewer_edit_within_limit(self):
        """Reviewer small edit should have no warnings."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("review", "edit", {
            "path": "/workspace/test.py",
            "old_text": "x = 1",
            "new_text": "x = 2",
        })
        assert not any("Permission denied" in w for w in warnings)
        # No fine-grained warning either — diff is small
        assert len(warnings) == 0

    def test_reviewer_edit_exceeds_limit(self):
        """Reviewer large edit should get a warning about line limit."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("review", "edit", {
            "path": "/workspace/test.py",
            "old_text": "x" * 10,
            "new_text": "y" * 300,
        })
        assert len(warnings) > 0
        assert "10 lines" in warnings[0]

    def test_reviewer_cannot_write(self):
        """Reviewer cannot use write tool (not in allowed_agent_types)."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("review", "write", {"path": "/workspace/test.py"})
        assert any("Permission denied" in w for w in warnings)

    def test_reviewer_cannot_shell(self):
        """Reviewer cannot use shell tool."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("review", "shell", {"command": "ls"})
        assert any("Permission denied" in w for w in warnings)

    # ------------------------------------------------------------------
    # Planner (plan)
    # ------------------------------------------------------------------

    def test_planner_write_md_allowed(self):
        """Planner writing .md file should be allowed."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "write", {"path": "/workspace/plan.md"})
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_planner_write_txt_allowed(self):
        """Planner writing .txt file should be allowed."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "write", {"path": "/workspace/notes.txt"})
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_planner_write_json_allowed(self):
        """Planner writing .json file should be allowed."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "write", {"path": "/workspace/config.json"})
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_planner_write_py_warns(self):
        """Planner writing .py file should get a warning."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "write", {"path": "/workspace/main.py"})
        assert len(warnings) > 0
        assert ".py" in warnings[0] or "planning files" in warnings[0].lower()

    def test_planner_write_ts_warns(self):
        """Planner writing .ts file should get a warning."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "write", {"path": "/workspace/app.ts"})
        assert len(warnings) > 0

    def test_planner_can_edit(self):
        """Planner can use edit tool."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "edit", {
            "path": "/workspace/test.py",
            "old_text": "a",
            "new_text": "b",
        })
        assert not any("Permission denied" in w for w in warnings)

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    def test_shell_write_code_file_warns(self):
        """Shell writing .py file should get a warning."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "write", {"path": "/workspace/main.py"})
        assert len(warnings) > 0

    def test_shell_write_config_allowed(self):
        """Shell writing config file should be allowed."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "write", {"path": "/workspace/config.json"})
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_shell_edit_non_config_warns(self):
        """Shell editing a .py file should get a warning."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "edit", {
            "path": "/workspace/main.py",
            "old_text": "x",
            "new_text": "y",
        })
        assert len(warnings) > 0
        assert "config files" in warnings[0].lower() or ".py" in warnings[0]

    def test_shell_edit_yml_allowed(self):
        """Shell editing a .yml file should be allowed."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "edit", {
            "path": "/workspace/docker-compose.yml",
            "old_text": "old",
            "new_text": "new",
        })
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_shell_edit_toml_allowed(self):
        """Shell editing a .toml file should be allowed."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "edit", {
            "path": "/workspace/pyproject.toml",
            "old_text": "old",
            "new_text": "new",
        })
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_shell_write_ts_warns(self):
        """Shell writing .ts file should get a warning (code file)."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "write", {"path": "/workspace/app.ts"})
        assert len(warnings) > 0

    # ------------------------------------------------------------------
    # Coder — full access
    # ------------------------------------------------------------------

    def test_coder_full_access(self):
        """Coder should have no restrictions on any tool."""
        _register_real_tools()
        for tool_name in ("edit", "write", "shell", "read", "glob", "grep"):
            warnings = ToolRegistry.validate_execution("coder", tool_name, {"path": "/workspace/test.py"})
            assert not any("Permission denied" in w for w in warnings), (
                f"coder should use {tool_name}"
            )

    def test_coder_edit_no_length_warning(self):
        """Coder editing with large diff should not get review-style warnings."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("coder", "edit", {
            "path": "/workspace/test.py",
            "old_text": "x" * 10,
            "new_text": "y" * 500,
        })
        assert not any("Permission denied" in w for w in warnings)
        assert len(warnings) == 0

    def test_coder_write_any_file(self):
        """Coder can write any file type without warnings."""
        _register_real_tools()
        for path in ("/workspace/main.py", "/workspace/config.json", "/workspace/README.md"):
            warnings = ToolRegistry.validate_execution("coder", "write", {"path": path})
            assert not any("Permission denied" in w for w in warnings)
            assert len(warnings) == 0, f"coder write {path} got warnings: {warnings}"

    def test_merge_full_access(self):
        """Merge should be able to use integration-oriented tools without coarse denial."""
        _register_real_tools()
        for tool_name in ("edit", "write", "shell", "read", "glob", "grep", "apply_patch"):
            warnings = ToolRegistry.validate_execution("merge", tool_name, {"path": "/workspace/test.py"})
            assert not any("Permission denied" in w for w in warnings), (
                f"merge should use {tool_name}"
            )

    # ------------------------------------------------------------------
    # Unknown tool
    # ------------------------------------------------------------------

    def test_unknown_tool(self):
        """Unknown tool should return error."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("coder", "nonexistent_tool", {})
        assert len(warnings) > 0
        assert "Unknown tool" in warnings[0]

    def test_unknown_tool_any_agent(self):
        """Unknown tool returns error regardless of agent type."""
        _register_real_tools()
        for agent_type in ("coder", "explore", "merge", "plan", "review", "shell", "human"):
            warnings = ToolRegistry.validate_execution(agent_type, "nonexistent", {})
            assert len(warnings) > 0
            assert "Unknown tool" in warnings[0]

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_arguments(self):
        """Empty arguments should not crash, relies on coarse check."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("review", "edit", {})
        # No "Permission denied" since review is in allowed_agent_types
        assert not any("Permission denied" in w for w in warnings)

    def test_write_no_path(self):
        """Write with no path should not crash (fine-grained checks are defensive)."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "write", {})
        assert not any("Permission denied" in w for w in warnings)
        # No path -> fine-grained check skips (path is empty/falsy)
        assert len(warnings) == 0

    def test_edit_no_path(self):
        """Edit with no path from shell should not crash."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "edit", {})
        assert not any("Permission denied" in w for w in warnings)

    def test_apply_patch_plan_allowed(self):
        """Plan agent can use apply_patch."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("plan", "apply_patch", {})
        assert not any("Permission denied" in w for w in warnings)

    def test_apply_patch_coder_allowed(self):
        """Coder agent can use apply_patch."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("coder", "apply_patch", {})
        assert not any("Permission denied" in w for w in warnings)

    def test_apply_patch_review_denied(self):
        """Review agent cannot use apply_patch."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("review", "apply_patch", {})
        assert any("Permission denied" in w for w in warnings)

    def test_apply_patch_shell_denied(self):
        """Shell agent cannot use apply_patch."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("shell", "apply_patch", {})
        assert any("Permission denied" in w for w in warnings)

    def test_apply_patch_explore_denied(self):
        """Explore agent cannot use apply_patch."""
        _register_real_tools()
        warnings = ToolRegistry.validate_execution("explore", "apply_patch", {})
        assert any("Permission denied" in w for w in warnings)

    def test_reviewer_edit_exact_limit(self):
        """Reviewer edit at exactly the 200-char boundary should not warn."""
        _register_real_tools()
        # diff = abs(200 - 0) = 200, which is NOT > 200
        warnings = ToolRegistry.validate_execution("review", "edit", {
            "path": "/workspace/test.py",
            "old_text": "",
            "new_text": "x" * 200,
        })
        assert len(warnings) == 0

    def test_reviewer_edit_just_over_limit(self):
        """Reviewer edit just over the 200-char boundary should warn."""
        _register_real_tools()
        # diff = abs(201 - 0) = 201, which IS > 200
        warnings = ToolRegistry.validate_execution("review", "edit", {
            "path": "/workspace/test.py",
            "old_text": "",
            "new_text": "x" * 201,
        })
        assert len(warnings) > 0
        assert "10 lines" in warnings[0]
