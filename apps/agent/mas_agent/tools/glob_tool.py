"""Glob tool — find files by pattern."""
from __future__ import annotations

import glob
import os
from typing import Any

from mas_agent.tools import Tool


class GlobTool(Tool):
    name = "glob"
    description = "Find files matching a glob pattern within the workspace."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts')",
            }
        },
        "required": ["pattern"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        pattern = arguments.get("pattern", "")
        if not pattern:
            return "Error: pattern is required"

        full_pattern = os.path.join(workspace, pattern)
        matches = sorted(glob.glob(full_pattern, recursive=True))
        if not matches:
            return f"No files found matching '{pattern}'"

        # Return relative paths
        rel = [os.path.relpath(m, workspace).replace("\\", "/") for m in matches[:200]]
        result = "\n".join(rel)
        if len(matches) > 200:
            result += f"\n... ({len(matches) - 200} more)"
        return result
