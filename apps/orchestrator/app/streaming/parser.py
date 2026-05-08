import json
import re
import logging
from typing import Optional

from app.agents.base import StreamEvent

logger = logging.getLogger(__name__)

# Compiled regex for ANSI escape code stripping.
# Matches ESC [ ... letter patterns (CSI sequences) as well as
# other common escape sequences (OSC, etc.).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[^[\]()]")

# Maximum line length: skip lines > 1 MB to avoid memory issues.
_MAX_LINE_BYTES = 1 * 1024 * 1024  # 1 MB


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text.

    Handles:
    - CSI sequences: ESC [ <params> <letter>  (e.g. colors, cursor movement)
    - OSC sequences: ESC ] ... BEL            (e.g. window title)
    - Other 2-byte escape sequences

    Args:
        text: Input string that may contain ANSI escape codes.

    Returns:
        Cleaned string with all ANSI sequences removed.
    """
    return _ANSI_RE.sub("", text)


class OpenCodeOutputParser:
    """Parses OpenCode JSONL output lines into standardized StreamEvents.

    Handles gracefully:
    - Non-JSON lines (stdout pollution from npm/pip/gcc) -> skipped
    - Malformed JSON -> skipped with warning
    - Lines exceeding 1 MB -> skipped (memory defense)
    - ANSI escape codes -> stripped before processing
    """

    def parse(self, line: str, run_id: str, node_id: str) -> Optional[StreamEvent]:
        # Strip ANSI escape codes (present when TERM is not "dumb")
        line = strip_ansi(line).strip()
        if not line:
            return None

        # Memory defense: skip lines exceeding 1 MB
        if len(line.encode("utf-8", errors="replace")) > _MAX_LINE_BYTES:
            logger.warning(
                "Skipping oversized line (%d bytes) in run=%s node=%s",
                len(line.encode("utf-8", errors="replace")),
                run_id,
                node_id,
            )
            return None

        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON line (stdout pollution) -> treat as plain text
            return StreamEvent(
                event_type="shell_stdout",
                content=line,
                node_id=node_id,
                run_id=run_id,
            )

        if not isinstance(raw, dict):
            return None

        raw_type = raw.get("type", "")
        event_type = self._map_type(raw_type)
        if not event_type:
            return None

        # Extract content from OpenCode v1.14.41 nested structure.
        # Actual output wraps payload in a "part" dict:
        #   {"type":"text", "part":{"text":"Hello!", ...}}
        #   {"type":"step_start", "part":{"type":"step-start", ...}}
        part = raw.get("part", {})
        content = self._extract_content(event_type, raw, part)

        return StreamEvent(
            event_type=event_type,
            content=content,
            node_id=node_id,
            run_id=run_id,
            tool_name=raw.get("tool") or raw.get("tool_name")
            or part.get("tool") or part.get("tool_name"),
            metadata=raw,
            timestamp=raw.get("timestamp"),
        )

    def _extract_content(
        self, event_type: str, raw: dict, part: dict
    ) -> str:
        """Extract text content from an OpenCode event, handling the
        v1.14.41 nested ``part`` structure as well as flat legacy format."""
        if event_type == "llm_token":
            return part.get("text", raw.get("content", ""))
        if event_type in ("node_started", "node_completed"):
            return json.dumps(part) if part else ""
        if event_type in ("tool_call", "tool_result"):
            return part.get("text", raw.get("content", ""))
        # Fallback: prefer part.text, then raw.content
        return raw.get("content", part.get("text", ""))

    def _map_type(self, raw_type: str) -> str:
        """Map OpenCode event type to internal StreamEvent type.

        Covers OpenCode v1.14.41 actual output types (--format json):
        - text:            LLM text output
        - step_start / step-start:  Agent step begins
        - step_finish / step-finish: Agent step ends
        Plus legacy/alternative types for compatibility.
        """
        mapping = {
            # OpenCode v1.14.41 actual types (--format json)
            "text": "llm_token",
            "step_start": "node_started",
            "step_finish": "node_completed",
            "step-start": "node_started",
            "step-finish": "node_completed",
            # Legacy / alternative types
            "token": "llm_token",
            "thinking": "llm_token",
            "message": "llm_token",
            "assistant": "llm_token",
            "tool_use": "tool_call",
            "tool-call": "tool_call",
            "tool_result": "tool_result",
            "tool-result": "tool_result",
            "shell_stdout": "shell_stdout",
            "shell_stderr": "shell_stdout",
            "shell-stdout": "shell_stdout",
            "shell-stderr": "shell_stdout",
            "status": "status",
            "error": "error",
            "summary": "status",
            "progress": "status",
            "user": "status",
            "system": "status",
        }
        return mapping.get(raw_type, "")
