"""StreamWriter — writes events to stream.jsonl for the orchestrator."""
from __future__ import annotations

import json
import os
import time
from typing import Any


class StreamWriter:
    def __init__(self, stream_dir: str, run_id: str, node_id: str) -> None:
        self.stream_dir = stream_dir
        self.stream_path = os.path.join(stream_dir, "stream.jsonl")
        self.run_id = run_id
        self.node_id = node_id
        self._child_counter = 0
        os.makedirs(stream_dir, exist_ok=True)
        self._file = open(self.stream_path, "a", encoding="utf-8")

    def _write(self, event: dict[str, Any]) -> None:
        event.setdefault("run_id", self.run_id)
        event.setdefault("node_id", self.node_id)
        event.setdefault("timestamp", time.time())
        self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._file.flush()

    def emit_llm_token(self, content: str) -> None:
        self._write({"type": "llm_token", "content": content})

    def emit_thinking(self, content: str) -> None:
        self._write({"type": "llm_chunk", "content": content, "metadata": {"thinking": True}})

    def emit_tool_call(self, tool_name: str, content: str,
                       metadata: dict[str, Any] | None = None) -> None:
        event: dict[str, Any] = {"type": "tool_call", "tool_name": tool_name, "content": content}
        if metadata:
            event["metadata"] = metadata
        self._write(event)

    def emit_tool_result(self, tool_name: str, content: str,
                         metadata: dict[str, Any] | None = None) -> None:
        event: dict[str, Any] = {"type": "tool_result", "tool_name": tool_name, "content": content}
        if metadata:
            event["metadata"] = metadata
        self._write(event)

    def emit_shell_stdout(self, content: str) -> None:
        self._write({"type": "shell_stdout", "content": content})

    def emit_shell_stderr(self, content: str) -> None:
        self._write({"type": "shell_stderr", "content": content})

    def emit_status(self, content: str) -> None:
        self._write({"type": "status", "content": content})

    def emit_error(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        event: dict[str, Any] = {"type": "error", "content": content}
        if metadata:
            event["metadata"] = metadata
        self._write(event)

    def emit_child_created(self, child_node_id: str, child_type: str,
                           child_prompt: str, child_model: str = "") -> None:
        self._write({
            "type": "child_created",
            "child_node_id": child_node_id,
            "child_type": child_type,
            "child_prompt": child_prompt,
            "child_model": child_model,
        })

    def next_child_id(self) -> str:
        child_id = f"{self.node_id}_child_{self._child_counter}"
        self._child_counter += 1
        return child_id

    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.close()
