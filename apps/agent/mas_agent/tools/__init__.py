"""Tool registry and built-in tools."""
from __future__ import annotations

from typing import Any


class Tool:
    """Base class for agent tools."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        raise NotImplementedError

    def to_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """Registry of available tools, optionally filtered by agent type."""

    _tools: dict[str, Tool] = {}

    @classmethod
    def register(cls, tool: Tool) -> None:
        cls._tools[tool.name] = tool

    @classmethod
    def get(cls, name: str) -> Tool | None:
        return cls._tools.get(name)

    @classmethod
    def all_schemas(cls) -> list[dict[str, Any]]:
        return [t.to_api_schema() for t in cls._tools.values()]

    @classmethod
    def for_agent_type(cls, agent_type: str) -> list[dict[str, Any]]:
        """Return API schemas for tools available to this agent type."""
        # All agent types get the same base tool set for now
        if agent_type in ("human",):
            return []
        return cls.all_schemas()


# Import and register built-in tools
from mas_agent.tools.glob_tool import GlobTool  # noqa: E402
from mas_agent.tools.read_tool import ReadTool  # noqa: E402
from mas_agent.tools.write_tool import WriteTool  # noqa: E402
from mas_agent.tools.shell_tool import ShellTool  # noqa: E402

ToolRegistry.register(GlobTool())
ToolRegistry.register(ReadTool())
ToolRegistry.register(WriteTool())
ToolRegistry.register(ShellTool())
