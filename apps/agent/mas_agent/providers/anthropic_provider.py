"""Anthropic-compatible LLM provider (works with MiMo, GLM, etc.)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from mas_agent.providers.base import BaseLLMProvider, StreamChunk

logger = logging.getLogger(__name__)

# Retry configuration
_MAX_RETRIES = 3
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
_BACKOFF_BASE = 1  # seconds


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

        timeout = httpx.Timeout(connect=15, read=120, write=30, pool=15)

        last_exception: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            should_retry = False

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", url, json=body, headers=headers) as resp:
                        if resp.status_code >= 400:
                            error_body = await resp.aread()
                            error_text = error_body.decode()

                            # 4xx errors should not be retried
                            if 400 <= resp.status_code < 500:
                                raise RuntimeError(
                                    f"LLM API error {resp.status_code}: {error_text}"
                                )

                            # 5xx errors — retryable
                            last_exception = RuntimeError(
                                f"LLM API error {resp.status_code}: {error_text}"
                            )
                            logger.warning(
                                "LLM API returned %d (attempt %d/%d)",
                                resp.status_code,
                                attempt + 1,
                                _MAX_RETRIES,
                            )
                            should_retry = True

                        else:
                            # Success — process the streaming response
                            async for chunk in self._process_sse_stream(resp):
                                yield chunk
                            return

            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exception = exc
                logger.warning(
                    "LLM request failed with %s (attempt %d/%d): %s",
                    type(exc).__name__,
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                )
                should_retry = True

            if should_retry and attempt < _MAX_RETRIES - 1:
                backoff = _BACKOFF_BASE * (2 ** attempt)
                logger.info("Retrying in %ds ...", backoff)
                await asyncio.sleep(backoff)

        # All retries exhausted
        if last_exception is not None:
            raise last_exception
        raise RuntimeError("LLM request failed after all retries")

    @staticmethod
    async def _process_sse_stream(
        resp: httpx.Response,
    ) -> AsyncIterator[StreamChunk]:
        """Parse an SSE response stream and yield StreamChunks."""
        current_tool_id = ""
        current_tool_name = ""
        partial_json_buffers: dict[str, str] = {}

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
                    partial_json_buffers[current_tool_id] = ""
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
                    if current_tool_id:
                        partial_json_buffers[current_tool_id] += delta.get(
                            "partial_json", ""
                        )

            # Content block stop — emit completed tool call
            elif etype == "content_block_stop":
                if current_tool_id and current_tool_name:
                    raw_json = partial_json_buffers.get(current_tool_id, "")
                    try:
                        parsed_input = json.loads(raw_json) if raw_json else {}
                    except (json.JSONDecodeError, ValueError):
                        logger.warning(
                            "Failed to parse tool input JSON for %s: %s",
                            current_tool_name,
                            raw_json[:200],
                        )
                        parsed_input = {}

                    yield StreamChunk(
                        type="tool_use",
                        tool_name=current_tool_name,
                        tool_input=parsed_input,
                        tool_call_id=current_tool_id,
                    )
                    # Clean up buffer
                    partial_json_buffers.pop(current_tool_id, None)
                    current_tool_id = ""
                    current_tool_name = ""

            # Message-level stop
            elif etype == "message_stop":
                break

            # Error
            elif etype == "error":
                error_msg = event.get("error", {}).get("message", "Unknown error")
                yield StreamChunk(type="error", text=error_msg)
                return
