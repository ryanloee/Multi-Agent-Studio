"""Read tool — read file contents with pagination, encoding detection, and binary guard."""
from __future__ import annotations

import os
from typing import Any

from mas_agent.tools import Tool
from mas_agent.tools.output_utils import truncate_output

# Encodings tried in order when UTF-8 fails.
_FALLBACK_ENCODINGS = ["utf-8-sig", "gbk", "shift-jis", "latin-1"]

# Lines at which we emit a "large file" warning.
_LARGE_FILE_THRESHOLD = 1000


def _try_read_lines(full_path: str) -> list[str]:
    """Read *full_path* and return its lines, trying multiple encodings.

    UTF-8 (with BOM stripping via ``utf-8-sig``) is tried first.  If that
    raises ``UnicodeDecodeError`` we fall back through GBK, Shift-JIS, and
    Latin-1.
    """
    last_err: Exception | None = None
    for enc in _FALLBACK_ENCODINGS:
        try:
            with open(full_path, "r", encoding=enc) as f:
                return f.readlines()
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    # All encodings failed — should be extremely rare (Latin-1 always works).
    raise last_err  # type: ignore[misc]


def _is_binary(full_path: str) -> tuple[bool, int]:
    """Return ``(is_binary, file_size)`` by checking for NUL bytes."""
    size = os.path.getsize(full_path)
    if size == 0:
        return False, size
    with open(full_path, "rb") as f:
        chunk = f.read(8192)
    return b"\x00" in chunk, size


class ReadTool(Tool):
    name = "read"
    description = "Read the contents of a file. Returns up to 500 lines with line numbers."
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
            "start_line": {
                "type": "integer",
                "description": "1-based start line (inclusive)",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based end line (inclusive)",
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

        # ---- Binary detection ----
        is_bin, file_size = _is_binary(full_path)
        if is_bin:
            return f"Binary file, cannot display. ({file_size} bytes)"

        # ---- Read with encoding fallback ----
        try:
            lines = _try_read_lines(full_path)
        except Exception as e:
            return f"Error reading file: {e}"

        total = len(lines)

        # ---- Determine slice from parameters ----
        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line")

        if start_line is not None:
            # 1-based → 0-based, clamped to valid range.
            start_idx = max(0, start_line - 1)
            if end_line is not None:
                end_idx = min(total, end_line)  # end_line is inclusive → slice exclusive
            else:
                end_idx = min(total, start_idx + 500)
        else:
            offset = arguments.get("offset", 0)
            limit = arguments.get("limit", 500)
            start_idx = offset
            end_idx = min(total, offset + limit)

        # ---- Slice & number ----
        selected = lines[start_idx:end_idx]
        display_start = start_idx + 1  # 1-based for display
        display_end = start_idx + len(selected)

        numbered = [
            f"{display_start + i}\t{line.rstrip()}" for i, line in enumerate(selected)
        ]

        parts: list[str] = []

        # ---- Large file warning ----
        if total > _LARGE_FILE_THRESHOLD:
            parts.append(
                f"Large file ({total} lines). Showing lines {display_start}-{display_end}."
            )

        parts.append("\n".join(numbered))

        # ---- Pagination hint ----
        if display_end < total:
            parts.append(
                f"Lines {display_start}-{display_end} of {total}. "
                f"Use offset={display_end} to read more."
            )

        result = "\n".join(parts)
        return truncate_output(result, workspace=workspace, label="read")
