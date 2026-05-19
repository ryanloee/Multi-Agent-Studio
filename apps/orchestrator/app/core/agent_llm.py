"""LLM API client — supports Anthropic and OpenAI-compatible endpoints.

Used by AgentRunner to call LLM APIs directly via httpx, replacing the
opencode TypeScript CLI subprocess.

Supports two formats:
  - "anthropic": POST {base_url}/v1/messages
  - "openai":    POST {base_url}/chat/completions
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# Default timeouts (seconds)
_CONNECT_TIMEOUT = 15.0
_READ_TIMEOUT = 300.0  # LLM calls can take a while
_WRITE_TIMEOUT = 30.0


@dataclass
class LLMResponse:
    """Parsed response from an LLM API call."""
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str = ""  # MiMo/GLM thinking mode content
    raw: dict[str, Any] = field(default_factory=dict)


def _normalize_reasoning(rc: Any) -> str:
    """Normalize reasoning_content to a plain string.

    MiMo/GLM APIs may return reasoning_content as str, list, dict, or None.
    This ensures we always store and pass back a string.
    """
    if rc is None:
        return ""
    if isinstance(rc, str):
        return rc
    if isinstance(rc, list):
        # List of content blocks — extract text portions
        parts = []
        for item in rc:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", "") or item.get("content", "") or str(item))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(rc, dict):
        return rc.get("text", "") or rc.get("content", "") or json.dumps(rc, ensure_ascii=False)
    return str(rc)


class LLMClient:
    """Direct HTTP client for Anthropic and OpenAI-compatible LLM APIs."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT,
                read=_READ_TIMEOUT,
                write=_WRITE_TIMEOUT,
                pool=5.0,
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        model_config: dict[str, Any],
    ) -> LLMResponse:
        """Send a chat request to the configured LLM API.

        Args:
            messages: Conversation messages in Anthropic/OpenAI format.
            tools: Tool definitions (Anthropic/OpenAI tool_use format).
            system: System prompt text.
            model_config: Dict with keys: provider, model, url, key,
                         context_window, max_output_tokens.

        Returns:
            LLMResponse with text, tool_calls, finish_reason, usage.
        """
        fmt = str(model_config.get("provider", "openai")).lower()
        if "anthropic" in fmt:
            return await self._call_anthropic(messages, tools, system, model_config)
        return await self._call_openai(messages, tools, system, model_config)

    # -----------------------------------------------------------------------
    # Anthropic Messages API
    # -----------------------------------------------------------------------

    async def _call_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        model_config: dict[str, Any],
    ) -> LLMResponse:
        base_url = str(model_config.get("url", "")).rstrip("/")
        api_key = str(model_config.get("key", ""))
        model = str(model_config.get("model", "claude-sonnet-4-20250514"))
        max_tokens = int(model_config.get("max_output_tokens", 4096))

        url = f"{base_url}/v1/messages"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [_format_anthropic_tool(t) for t in tools]

        logger.debug("Anthropic request: %s %s tools=%d msgs=%d", url, model, len(tools), len(messages))

        try:
            resp = await self._client.post(url, headers=headers, json=body)
        except httpx.ConnectError as exc:
            raise LLMConnectionError(f"Cannot connect to {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"Request timed out for {url}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMAPIError(
                f"Anthropic API error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )

        data = resp.json()
        return _parse_anthropic_response(data)

    # -----------------------------------------------------------------------
    # OpenAI-compatible Chat Completions API
    # -----------------------------------------------------------------------

    async def _call_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        model_config: dict[str, Any],
    ) -> LLMResponse:
        base_url = str(model_config.get("url", "")).rstrip("/")
        api_key = str(model_config.get("key", ""))
        model = str(model_config.get("model", "gpt-4o"))
        max_tokens = int(model_config.get("max_output_tokens", 4096))

        url = f"{base_url}/chat/completions"

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Build messages with system prompt
        oai_messages: list[dict[str, Any]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})

        # Convert Anthropic-style messages to OpenAI format
        for msg in messages:
            oai_messages.extend(_convert_to_openai_message(msg))

        body: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [_format_openai_tool(t) for t in tools]

        logger.debug("OpenAI request: %s %s tools=%d msgs=%d", url, model, len(tools), len(oai_messages))

        try:
            resp = await self._client.post(url, headers=headers, json=body)
        except httpx.ConnectError as exc:
            raise LLMConnectionError(f"Cannot connect to {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"Request timed out for {url}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMAPIError(
                f"OpenAI API error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )

        data = resp.json()
        return _parse_openai_response(data)

    # -----------------------------------------------------------------------
    # Streaming (optional, for future use)
    # -----------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        model_config: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat response. Yields SSE-style event dicts.

        Each yield: {"type": "text_delta", "text": "..."} or
                    {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
                    {"type": "done", "finish_reason": "...", "usage": {...}}
        """
        fmt = str(model_config.get("provider", "openai")).lower()
        if "anthropic" in fmt:
            async for event in self._stream_anthropic(messages, tools, system, model_config):
                yield event
        else:
            async for event in self._stream_openai(messages, tools, system, model_config):
                yield event

    async def _stream_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        model_config: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        base_url = str(model_config.get("url", "")).rstrip("/")
        api_key = str(model_config.get("key", ""))
        model = str(model_config.get("model", "claude-sonnet-4-20250514"))
        max_tokens = int(model_config.get("max_output_tokens", 4096))

        url = f"{base_url}/v1/messages"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [_format_anthropic_tool(t) for t in tools]

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                error_body = ""
                async for chunk in resp.aiter_text():
                    error_body += chunk
                raise LLMAPIError(
                    f"Anthropic streaming error {resp.status_code}: {error_body[:500]}",
                    status_code=resp.status_code,
                )

            current_tool: dict[str, Any] | None = None
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        current_tool = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input_json": "",
                        }
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield {"type": "text_delta", "text": delta.get("text", "")}
                    elif delta.get("type") == "input_json_delta":
                        if current_tool is not None:
                            current_tool["input_json"] += delta.get("partial_json", "")
                elif event_type == "content_block_stop":
                    if current_tool is not None:
                        try:
                            tool_input = json.loads(current_tool["input_json"])
                        except json.JSONDecodeError:
                            tool_input = {}
                        yield {
                            "type": "tool_use",
                            "id": current_tool["id"],
                            "name": current_tool["name"],
                            "input": tool_input,
                        }
                        current_tool = None
                elif event_type == "message_delta":
                    delta = event.get("delta", {})
                    usage = event.get("usage", {})
                    yield {
                        "type": "done",
                        "finish_reason": delta.get("stop_reason", ""),
                        "usage": usage,
                    }

    async def _stream_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        model_config: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        base_url = str(model_config.get("url", "")).rstrip("/")
        api_key = str(model_config.get("key", ""))
        model = str(model_config.get("model", "gpt-4o"))
        max_tokens = int(model_config.get("max_output_tokens", 4096))

        url = f"{base_url}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        oai_messages: list[dict[str, Any]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        for msg in messages:
            oai_messages.extend(_convert_to_openai_message(msg))

        body: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = [_format_openai_tool(t) for t in tools]

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                error_body = ""
                async for chunk in resp.aiter_text():
                    error_body += chunk
                raise LLMAPIError(
                    f"OpenAI streaming error {resp.status_code}: {error_body[:500]}",
                    status_code=resp.status_code,
                )

            # Track tool calls being built
            tool_buffers: dict[int, dict[str, Any]] = {}

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                finish = choices[0].get("finish_reason")

                # Text content
                if "content" in delta and delta["content"]:
                    yield {"type": "text_delta", "text": delta["content"]}

                # Reasoning content (MiMo/GLM thinking mode)
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    yield {"type": "reasoning_delta", "text": delta["reasoning_content"]}

                # Tool calls
                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_buffers:
                            tool_buffers[idx] = {
                                "id": tc.get("id", ""),
                                "name": "",
                                "arguments": "",
                            }
                        buf = tool_buffers[idx]
                        if "id" in tc and tc["id"]:
                            buf["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            buf["name"] = fn["name"]
                        if fn.get("arguments"):
                            buf["arguments"] += fn["arguments"]

                if finish:
                    # Flush tool calls
                    for idx in sorted(tool_buffers.keys()):
                        buf = tool_buffers[idx]
                        try:
                            tool_input = json.loads(buf["arguments"])
                        except json.JSONDecodeError:
                            tool_input = {}
                        yield {
                            "type": "tool_use",
                            "id": buf["id"],
                            "name": buf["name"],
                            "input": tool_input,
                        }
                    usage = chunk.get("usage", {})
                    yield {
                        "type": "done",
                        "finish_reason": finish,
                        "usage": usage,
                    }


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def _parse_anthropic_response(data: dict[str, Any]) -> LLMResponse:
    """Parse Anthropic Messages API response."""
    resp = LLMResponse(raw=data)

    for block in (data.get("content") or []):
        block_type = block.get("type", "")
        if block_type == "text":
            resp.text += block.get("text", "")
        elif block_type == "tool_use":
            resp.tool_calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })

    resp.finish_reason = data.get("stop_reason", "")
    resp.usage = data.get("usage", {})
    return resp


def _parse_openai_response(data: dict[str, Any]) -> LLMResponse:
    """Parse OpenAI Chat Completions API response."""
    resp = LLMResponse(raw=data)

    choices = data.get("choices") or []
    if not choices:
        return resp

    message = choices[0].get("message", {})
    resp.text = message.get("content", "") or ""
    resp.finish_reason = choices[0].get("finish_reason", "")

    # Capture reasoning_content (MiMo/GLM thinking mode)
    resp.reasoning_content = _normalize_reasoning(message.get("reasoning_content"))

    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function", {})
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            arguments = {}
        resp.tool_calls.append({
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": arguments,
        })

    resp.usage = data.get("usage", {})
    return resp


# ---------------------------------------------------------------------------
# Tool format converters
# ---------------------------------------------------------------------------


def _format_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert tool definition to Anthropic format."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
    }


def _format_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert tool definition to OpenAI format."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _convert_to_openai_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an Anthropic-style message to OpenAI format.

    Anthropic assistant messages can have content blocks (text + tool_use).
    OpenAI uses separate fields: content (text), tool_calls (structured).

    Returns a list because one Anthropic message may produce an assistant
    message + multiple tool result messages.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")

    if role == "user":
        # User messages: content can be string or list of content blocks
        if isinstance(content, str):
            return [{"role": "user", "content": content}]
        elif isinstance(content, list):
            # May contain tool_result blocks — convert those
            parts = []
            tool_results = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": _extract_text(block.get("content", "")),
                        })
                    elif block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                elif isinstance(block, str):
                    parts.append(block)

            result = []
            if parts:
                result.append({"role": "user", "content": "\n".join(parts)})
            result.extend(tool_results)
            return result if result else [{"role": "user", "content": ""}]

    elif role == "assistant":
        # Preserve reasoning_content if present (MiMo/GLM thinking mode)
        rc = _normalize_reasoning(msg.get("reasoning_content"))

        if isinstance(content, str):
            out: dict[str, Any] = {"role": "assistant", "content": content}
            if rc:
                out["reasoning_content"] = rc
            return [out]
        elif isinstance(content, list):
            text_parts = []
            tool_calls = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })

            msg_out: dict[str, Any] = {"role": "assistant"}
            combined_text = "\n".join(text_parts)
            if combined_text:
                msg_out["content"] = combined_text
            if tool_calls:
                msg_out["tool_calls"] = tool_calls
            if rc:
                msg_out["reasoning_content"] = rc
            return [msg_out]

    # Fallback
    if isinstance(content, str):
        return [{"role": role or "user", "content": content}]
    return [{"role": "user", "content": str(content)}]


def _extract_text(content: Any) -> str:
    """Extract plain text from content that may be a string or list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base exception for LLM client errors."""


class LLMConnectionError(LLMError):
    """Cannot connect to the LLM API endpoint."""


class LLMTimeoutError(LLMError):
    """LLM API request timed out."""


class LLMAPIError(LLMError):
    """LLM API returned an error response."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code
