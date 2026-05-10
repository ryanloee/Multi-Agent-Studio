"""Tests for ToolRegistry.for_agent_type filtering logic."""
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
    saved = dict(ToolRegistry._tools)
    ToolRegistry.reset()
    yield
    ToolRegistry._tools = saved


def _register_stubs():
    """Register a representative set of stub tools mirroring the real set."""
    # Tools available to all (allowed_agent_types=None)
    ToolRegistry.register(_StubTool("glob"))
    ToolRegistry.register(_StubTool("read"))
    ToolRegistry.register(_StubTool("grep"))

    # Restricted tools
    ToolRegistry.register(_StubTool("edit", allowed_agent_types=["plan", "coder", "review"]))
    ToolRegistry.register(_StubTool("write", allowed_agent_types=["plan", "coder", "shell"]))
    ToolRegistry.register(_StubTool("shell", allowed_agent_types=["plan", "coder", "shell"]))


def _tool_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {s["name"] for s in schemas}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestForAgentType:
    """Core filtering behaviour."""

    def test_explore_gets_readonly_tools(self) -> None:
        """explore agents should only see glob, read, grep (no write/shell)."""
        _register_stubs()
        tools = _tool_names(ToolRegistry.for_agent_type("explore"))
        assert "glob" in tools
        assert "read" in tools
        assert "grep" in tools
        assert "write" not in tools
        assert "shell" not in tools
        assert "edit" not in tools

    def test_human_gets_no_tools(self) -> None:
        """human agents never receive tools."""
        _register_stubs()
        assert ToolRegistry.for_agent_type("human") == []

    def test_coder_gets_all_tools(self) -> None:
        """coder agents should see every registered tool."""
        _register_stubs()
        tools = _tool_names(ToolRegistry.for_agent_type("coder"))
        assert tools == {"glob", "read", "grep", "edit", "write", "shell"}

    def test_none_means_available_to_all(self) -> None:
        """A tool with allowed_agent_types=None is returned for every type."""
        ToolRegistry.register(_StubTool("universal", allowed_agent_types=None))
        for agent_type in ("plan", "coder", "explore", "review", "shell"):
            tools = _tool_names(ToolRegistry.for_agent_type(agent_type))
            assert "universal" in tools, f"universal missing for {agent_type}"

    def test_explicit_list_filters_strictly(self) -> None:
        """A tool with allowed_agent_types=['coder'] only appears for coder."""
        ToolRegistry.register(_StubTool("secret", allowed_agent_types=["coder"]))
        # coder sees it
        assert "secret" in _tool_names(ToolRegistry.for_agent_type("coder"))
        # other types do not
        for agent_type in ("plan", "explore", "review", "shell", "human"):
            assert "secret" not in _tool_names(ToolRegistry.for_agent_type(agent_type)), (
                f"secret should not be visible to {agent_type}"
            )

    def test_plan_sees_edit_and_write(self) -> None:
        """plan agents should see edit, write, and shell in addition to base tools."""
        _register_stubs()
        tools = _tool_names(ToolRegistry.for_agent_type("plan"))
        assert "edit" in tools
        assert "write" in tools
        assert "shell" in tools

    def test_review_sees_edit_but_not_write_nor_shell(self) -> None:
        """review agents can edit (for review files) but cannot write or shell."""
        _register_stubs()
        tools = _tool_names(ToolRegistry.for_agent_type("review"))
        assert "edit" in tools
        assert "write" not in tools
        assert "shell" not in tools

    def test_shell_agent_sees_shell_but_not_edit(self) -> None:
        """shell agents have write and shell access but not edit."""
        _register_stubs()
        tools = _tool_names(ToolRegistry.for_agent_type("shell"))
        assert "write" in tools
        assert "shell" in tools
        assert "edit" not in tools


class TestReset:
    """Verify reset clears the registry."""

    def test_reset_clears_tools(self) -> None:
        ToolRegistry.register(_StubTool("a"))
        assert len(ToolRegistry._tools) == 1
        ToolRegistry.reset()
        assert ToolRegistry._tools is None
