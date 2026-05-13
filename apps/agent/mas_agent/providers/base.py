"""Base LLM provider interface."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StreamChunk:
    type: str  # "text", "thinking", "tool_use", "tool_result", "error"
    text: str = ""
    tool_name: str = ""
    tool_input: dict | None = None
    tool_call_id: str = ""


class BaseLLMProvider:
    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        thinking_level: str = "off",
    ):
        raise NotImplementedError
