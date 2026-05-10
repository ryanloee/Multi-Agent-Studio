"""Read tool — read file contents."""
from __future__ import annotations

import os
from typing import Any

from mas_agent.tools import Tool


class ReadTool(Tool):
    name = "read"
    description = "Read the contents of a file. Returns up to 2000 lines with line numbers."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (0-based)",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to read",
                "default": 500,
            },
        },
        "required": ["path"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        rel_path = arguments.get("path", "")
        if not rel_path:
            return "Error: path is required"

        full_path = os.path.join(workspace, rel_path)
        if not os.path.isfile(full_path):
            return f"Error: file not found: {rel_path}"

        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 500)

        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return f"Error reading file: {e}"

        total = len(lines)
        selected = lines[offset : offset + limit]
        numbered = [f"{offset + i + 1}\t{line.rstrip()}" for i, line in enumerate(selected)]

        result = "\n".join(numbered)
        if offset + limit < total:
            result += f"\n... ({total - offset - limit} more lines, total {total})"
        return result
