"""Anthropic-compatible LLM provider (works with MiMo, GLM, etc.)."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from mas_agent.providers.base import BaseLLMProvider, StreamChunk

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseLLMProvider):
    """Streams chat completions via Anthropic-compatible /messages endpoint."""

    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        super().__init__(
            model=model,
            base_url=base_url or "https://api.anthropic.com",
            api_key=api_key,
        )

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        url = f"{self.base_url.rstrip('/')}/v1/messages"

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    raise RuntimeError(
                        f"LLM API error {resp.status_code}: {error_body.decode()}"
                    )

                current_tool_id = ""
                current_tool_name = ""
                current_tool_input: dict[str, Any] = {}

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break

                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")

                    # Content block start — could be text or tool_use
                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_tool_id = block.get("id", "")
                            current_tool_name = block.get("name", "")
                            current_tool_input = {}
                        elif block.get("type") == "thinking":
                            pass  # thinking block started

                    # Delta — text, thinking, or partial tool input
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type", "")

                        if dtype == "text_delta":
                            yield StreamChunk(type="text", text=delta.get("text", ""))

                        elif dtype == "thinking_delta":
                            yield StreamChunk(type="thinking", text=delta.get("thinking", ""))

                        elif dtype == "input_json_delta":
                            partial = delta.get("partial_json", "")
                            # Try to accumulate into a valid JSON object
                            try:
                                current_tool_input = json.loads(
                                    (json.dumps(current_tool_input) if current_tool_input else "{")
                                    .rstrip("}")
                                    + ("," if current_tool_input else "")
                                    + partial.lstrip("{").rstrip("}")
                                    + "}"
                                )
                            except (json.JSONDecodeError, ValueError):
                                pass

                    # Content block stop — emit completed tool call
                    elif etype == "content_block_stop":
                        if current_tool_id and current_tool_name:
                            yield StreamChunk(
                                type="tool_use",
                                tool_name=current_tool_name,
                                tool_input=current_tool_input or {},
                                tool_call_id=current_tool_id,
                            )
                            current_tool_id = ""
                            current_tool_name = ""
                            current_tool_input = {}

                    # Message-level stop
                    elif etype == "message_stop":
                        break

                    # Error
                    elif etype == "error":
                        error_msg = event.get("error", {}).get("message", "Unknown error")
                        yield StreamChunk(type="error", text=error_msg)
                        return
