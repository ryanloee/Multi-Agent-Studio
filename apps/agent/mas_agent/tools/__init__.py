"""Tool registry and built-in tools."""
from __future__ import annotations

from typing import Any


class Tool:
    """Base class for agent tools."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}
    allowed_agent_types: list[str] | None = None
    """Agent types allowed to use this tool. None means available to all types."""

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

    _tools: dict[str, Tool] | None = None

    @classmethod
    def _ensure_init(cls) -> None:
        if cls._tools is None:
            cls._tools = {}

    @classmethod
    def register(cls, tool: Tool) -> None:
        cls._ensure_init()
        cls._tools[tool.name] = tool

    @classmethod
    def get(cls, name: str) -> Tool | None:
        cls._ensure_init()
        return cls._tools.get(name)

    @classmethod
    def all_schemas(cls) -> list[dict[str, Any]]:
        cls._ensure_init()
        return [t.to_api_schema() for t in cls._tools.values()]

    @classmethod
    def validate_execution(
        cls, agent_type: str, tool_name: str, arguments: dict[str, Any]
    ) -> list[str]:
        """Validate tool execution against agent-type-specific constraints.

        Returns a list of warning/error strings.  An empty list means the
        invocation is fully allowed.  If any entry starts with
        ``"Permission denied"`` the call should be blocked; all other
        entries are advisory warnings.
        """
        warnings: list[str] = []
        tool = cls.get(tool_name)
        if not tool:
            return [f"Unknown tool: {tool_name}"]

        # Coarse check: agent type allowed at all?
        if tool.allowed_agent_types and agent_type not in tool.allowed_agent_types:
            return [f"Permission denied: {agent_type} cannot use {tool_name}"]

        # --- Fine-grained parameter checks ---

        if tool_name == "edit" and agent_type == "review":
            # Reviewer: max ~10 lines changed (roughly 200 chars diff)
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            if abs(len(new_text) - len(old_text)) > 200:
                warnings.append(
                    "Reviewer edit limited to ~10 lines per change"
                )

        if tool_name == "write" and agent_type == "plan":
            # Planner: only planning-related file types
            path = arguments.get("path", "")
            valid_exts = (".md", ".txt", ".json", ".markdown")
            if path and not any(path.endswith(ext) for ext in valid_exts):
                warnings.append(
                    f"Planner can only write planning files (.md/.txt/.json), not: {path}"
                )

        if tool_name == "write" and agent_type == "shell":
            # Shell: no code files
            path = arguments.get("path", "")
            code_exts = (
                ".py", ".ts", ".js", ".tsx", ".jsx",
                ".go", ".rs", ".java", ".c", ".cpp",
            )
            if path and any(path.endswith(ext) for ext in code_exts):
                warnings.append(
                    f"Shell cannot write code files, use edit tool or delegate to Coder: {path}"
                )

        if tool_name == "edit" and agent_type == "shell":
            # Shell: only config files
            path = arguments.get("path", "")
            config_exts = (
                ".yml", ".yaml", ".json", ".toml",
                ".ini", ".cfg", ".env", ".xml",
            )
            if path and not any(path.endswith(ext) for ext in config_exts):
                warnings.append(
                    f"Shell edit limited to config files, not: {path}"
                )

        return warnings

    @classmethod
    def for_agent_type(cls, agent_type: str) -> list[dict[str, Any]]:
        """Return API schemas for tools available to this agent type.

        Tools with ``allowed_agent_types=None`` are available to every agent
        type.  Tools with an explicit list are only returned when the
        *agent_type* appears in that list.
        """
        if agent_type == "human":
            return []

        cls._ensure_init()
        schemas: list[dict[str, Any]] = []
        for tool in cls._tools.values():
            if tool.allowed_agent_types is None or agent_type in tool.allowed_agent_types:
                schemas.append(tool.to_api_schema())
        return schemas

    @classmethod
    def reset(cls) -> None:
        """Clear all registered tools (useful for tests)."""
        cls._tools = None


# Import and register built-in tools
from mas_agent.tools.glob_tool import GlobTool  # noqa: E402
from mas_agent.tools.grep_tool import GrepTool  # noqa: E402
from mas_agent.tools.read_tool import ReadTool  # noqa: E402
from mas_agent.tools.write_tool import WriteTool  # noqa: E402
from mas_agent.tools.shell_tool import ShellTool  # noqa: E402
from mas_agent.tools.edit_tool import EditTool  # noqa: E402

# glob, grep, and read are available to all agent types (allowed_agent_types=None)
ToolRegistry.register(GlobTool())
ToolRegistry.register(GrepTool())
ToolRegistry.register(ReadTool())

# write — plan, coder, shell only
_write = WriteTool()
_write.allowed_agent_types = ["plan", "coder", "shell"]
ToolRegistry.register(_write)

# shell — plan, coder, shell only
_shell = ShellTool()
_shell.allowed_agent_types = ["plan", "coder", "shell"]
ToolRegistry.register(_shell)

# edit — available to all agent types that write
_edit = EditTool()
_edit.allowed_agent_types = ["plan", "coder", "review", "shell"]
ToolRegistry.register(_edit)

from mas_agent.tools.apply_patch_tool import ApplyPatchTool  # noqa: E402

# apply_patch — plan, coder only (diff-based editing for GPT models)
_apply_patch = ApplyPatchTool()
_apply_patch.allowed_agent_types = ["plan", "coder"]
ToolRegistry.register(_apply_patch)
