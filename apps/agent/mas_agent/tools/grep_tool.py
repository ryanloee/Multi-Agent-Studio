"""Grep tool — search file contents by regex pattern."""
from __future__ import annotations

import asyncio
import shutil
from typing import Any

from mas_agent.tools import Tool
from mas_agent.tools.output_utils import truncate_output

_MAX_MATCHES = 50
_MAX_CONTEXT = 3
_TIMEOUT = 5


def _has_rg() -> bool:
    """Return True if ripgrep is available on PATH."""
    return shutil.which("rg") is not None


class GrepTool(Tool):
    name = "grep"
    description = "Search file contents by regex pattern within the workspace."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (relative to workspace root). Defaults to workspace root.",
            },
            "glob": {
                "type": "string",
                "description": "File filter pattern (e.g. '*.py', '*.{ts,tsx}').",
            },
            "context": {
                "type": "integer",
                "description": f"Number of context lines around each match (0-{_MAX_CONTEXT}). Defaults to 2.",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        pattern = arguments.get("pattern", "")
        if not pattern:
            return "Error: pattern is required"

        search_path = arguments.get("path", "")
        glob_filter = arguments.get("glob", "")
        context = min(int(arguments.get("context", 2)), _MAX_CONTEXT)

        # Resolve search directory
        if search_path:
            import os
            full_path = os.path.join(workspace, search_path)
        else:
            full_path = workspace

        if _has_rg():
            cmd = self._build_rg_cmd(pattern, full_path, glob_filter, context)
        else:
            cmd = self._build_grep_cmd(pattern, full_path, glob_filter, context)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            return "Error: search timed out after 5 seconds"

        output = stdout.decode(errors="replace").strip()
        if not output:
            return f"No matches found for pattern '{pattern}'"

        lines = output.splitlines()
        if len(lines) > _MAX_MATCHES:
            lines = lines[:_MAX_MATCHES]
            lines.append(f"... (more than {_MAX_MATCHES} matches, showing first {_MAX_MATCHES})")

        return truncate_output("\n".join(lines), workspace=workspace, label="grep")

    @staticmethod
    def _build_rg_cmd(
        pattern: str, path: str, glob_filter: str, context: int
    ) -> list[str]:
        cmd: list[str] = [
            "rg",
            "--no-heading",
            "--line-number",
            "--color=never",
        ]
        if context > 0:
            cmd.append(f"--context={context}")
        if glob_filter:
            cmd.extend(["--glob", glob_filter])
        cmd.extend(["--max-count", str(_MAX_MATCHES)])
        cmd.extend([pattern, path])
        return cmd

    @staticmethod
    def _build_grep_cmd(
        pattern: str, path: str, glob_filter: str, context: int
    ) -> list[str]:
        cmd: list[str] = ["grep", "-rn", "-E"]
        if context > 0:
            cmd.append(f"-C{context}")
        if glob_filter:
            cmd.extend(["--include", glob_filter])
        cmd.extend(["--", pattern, path])
        return cmd
