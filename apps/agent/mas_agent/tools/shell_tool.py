"""Shell tool — execute shell commands."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from mas_agent.tools import Tool
from mas_agent.tools.output_utils import truncate_output


class ShellTool(Tool):
    name = "shell"
    description = "Execute a shell command in the workspace directory. Use for build, test, install, git, etc."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 60)",
                "default": 60,
            },
        },
        "required": ["command"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        command = arguments.get("command", "")
        timeout = arguments.get("timeout", 60)

        if not command:
            return "Error: command is required"

        # Use cmd on Windows, sh on Unix
        if sys.platform == "win32":
            shell_cmd = ["cmd", "/c", command]
        else:
            shell_cmd = ["bash", "-c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                *shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: command timed out after {timeout}s"
        except Exception as e:
            return f"Error executing command: {e}"

        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        combined = ""
        if stdout_str:
            combined += stdout_str
        if stderr_str:
            if combined:
                combined += "\n"
            combined += stderr_str

        if not combined:
            return "(no output)"

        truncated = truncate_output(combined, workspace=workspace, label="shell")

        parts = []
        if proc.returncode != 0:
            parts.append(f"Exit code: {proc.returncode}")
        parts.append(truncated)

        return "\n\n".join(parts)
