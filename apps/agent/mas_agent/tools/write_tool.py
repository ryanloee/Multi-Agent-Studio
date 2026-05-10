"""Write tool — write file contents."""
from __future__ import annotations

import os
from typing import Any

from mas_agent.tools import Tool


class WriteTool(Tool):
    name = "write"
    description = "Write content to a file. Creates parent directories as needed."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
        },
        "required": ["path", "content"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        rel_path = arguments.get("path", "")
        content = arguments.get("content", "")

        if not rel_path:
            return "Error: path is required"

        full_path = os.path.join(workspace, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {rel_path}"
        except Exception as e:
            return f"Error writing file: {e}"
