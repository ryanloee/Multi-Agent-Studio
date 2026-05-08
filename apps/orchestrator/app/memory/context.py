"""Memory and context management module.

Handles:
- Workspace-based context sharing between nodes (.workflow/ directory)
- Context compression when approaching token limits
- Shared KV store via MCP
"""


class ContextManager:
    """Manages inter-node context through workspace file sharing."""

    async def write_node_output(
        self,
        sandbox_id: str,
        node_id: str,
        output: str,
        output_type: str = "markdown",
    ) -> str:
        """Write node output to .workflow/ directory for downstream nodes to read."""
        filename = f".workflow/{node_id}-output.{output_type}"
        # TODO: Use SandboxManager to write file
        return filename

    async def read_node_output(
        self,
        sandbox_id: str,
        node_id: str,
        output_type: str = "markdown",
    ) -> str:
        """Read upstream node output from .workflow/ directory."""
        filename = f".workflow/{node_id}-output.{output_type}"
        # TODO: Use SandboxManager to read file
        return ""
