"""Tests for AnthropicProvider: incremental JSON parsing, retry, timeout."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mas_agent.providers.anthropic_provider import AnthropicProvider


# ---------------------------------------------------------------------------
# Helpers: build SSE lines and mock response objects
# ---------------------------------------------------------------------------


def _sse_line(data: dict[str, Any] | str) -> str:
    """Build a single SSE ``data:`` line."""
    if isinstance(data, str):
        return f"data: {data}"
    return f"data: {json.dumps(data)}"


def _make_event_lines(events: list[dict[str, Any]]) -> list[str]:
    """Convert a list of event dicts into SSE-formatted lines."""
    lines: list[str] = []
    for ev in events:
        lines.append(_sse_line(ev))
    return lines


class _FakeAiterLines:
    """Helper mixin that provides ``aiter_lines`` from a list of strings."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):  # noqa: D401 — async generator
        for line in self._lines:
            yield line


class _FakeStreamResponse(_FakeAiterLines):
    """Fake httpx streaming response with configurable status/body."""

    def __init__(
        self,
        status_code: int = 200,
        lines: list[str] | None = None,
        body: bytes = b"",
    ) -> None:
        super().__init__(lines or [])
        self.status_code = status_code
        self._body = body

    async def aread(self) -> bytes:
        return self._body


class _FakeStreamContextManager:
    """Context manager that returns a _FakeStreamResponse."""

    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


class _FakeAsyncClient:
    """Minimal fake httpx.AsyncClient for testing retry/streaming."""

    def __init__(self, responses: list[_FakeStreamResponse]) -> None:
        # responses can be a list; each call to .stream() pops the first one.
        self._responses = list(responses)
        self.timeout = None

    def stream(self, method, url, **kwargs):
        resp = self._responses.pop(0)
        return _FakeStreamContextManager(resp)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider() -> AnthropicProvider:
    return AnthropicProvider(model="test-model", api_key="test-key", base_url="http://localhost:11434")


# ---------------------------------------------------------------------------
# Test: Incremental JSON — simple tool arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_json_simple(provider: AnthropicProvider):
    """Multiple partial_json deltas should accumulate and parse correctly."""
    events = [
        {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "call_1", "name": "read_file"},
        },
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"fi'}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": 'le": '}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '"main.py"}'}},
        {"type": "content_block_stop"},
        {"type": "message_stop"},
    ]

    fake_resp = _FakeStreamResponse(status_code=200, lines=_make_event_lines(events))
    fake_client = _FakeAsyncClient([fake_resp])

    chunks: list = []
    with patch("mas_agent.providers.anthropic_provider.httpx.AsyncClient", return_value=fake_client):
        async for chunk in provider.stream_chat(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    tool_chunks = [c for c in chunks if c.type == "tool_use"]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_name == "read_file"
    assert tool_chunks[0].tool_input == {"file": "main.py"}
    assert tool_chunks[0].tool_call_id == "call_1"


# ---------------------------------------------------------------------------
# Test: Nested JSON in tool arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_json_nested(provider: AnthropicProvider):
    """Tool arguments containing nested objects should parse correctly."""
    events = [
        {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "call_2", "name": "write_file"},
        },
        {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.txt", "content": {"li'},
        },
        {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": 'nes": ["one", "two"], "num": 42}}'},
        },
        {"type": "content_block_stop"},
        {"type": "message_stop"},
    ]

    fake_resp = _FakeStreamResponse(status_code=200, lines=_make_event_lines(events))
    fake_client = _FakeAsyncClient([fake_resp])

    chunks: list = []
    with patch("mas_agent.providers.anthropic_provider.httpx.AsyncClient", return_value=fake_client):
        async for chunk in provider.stream_chat(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    tool_chunks = [c for c in chunks if c.type == "tool_use"]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_name == "write_file"
    assert tool_chunks[0].tool_input == {
        "path": "a.txt",
        "content": {"lines": ["one", "two"], "num": 42},
    }


# ---------------------------------------------------------------------------
# Test: Network retry on ConnectError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_connect_error(provider: AnthropicProvider):
    """Should retry up to 3 times on httpx.ConnectError."""
    call_count = 0

    class _FailingClient:
        timeout = None

        def stream(self, method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("connection refused")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    with (
        patch("mas_agent.providers.anthropic_provider.httpx.AsyncClient", return_value=_FailingClient()),
        patch("mas_agent.providers.anthropic_provider.asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(httpx.ConnectError):
            async for _ in provider.stream_chat(messages=[{"role": "user", "content": "hi"}]):
                pass

    assert call_count == 3


# ---------------------------------------------------------------------------
# Test: Retry on 5xx response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_5xx(provider: AnthropicProvider):
    """Should retry on HTTP 503 responses."""
    error_resp = _FakeStreamResponse(status_code=503, body=b"Service Unavailable")
    success_events = [
        {"type": "content_block_start", "content_block": {"type": "text"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_stop"},
        {"type": "message_stop"},
    ]
    success_resp = _FakeStreamResponse(status_code=200, lines=_make_event_lines(success_events))

    fake_client = _FakeAsyncClient([error_resp, success_resp])

    chunks: list = []
    with (
        patch("mas_agent.providers.anthropic_provider.httpx.AsyncClient", return_value=fake_client),
        patch("mas_agent.providers.anthropic_provider.asyncio.sleep", new_callable=AsyncMock),
    ):
        async for chunk in provider.stream_chat(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    text_chunks = [c for c in chunks if c.type == "text"]
    assert len(text_chunks) == 1
    assert text_chunks[0].text == "Hello"


# ---------------------------------------------------------------------------
# Test: No retry on 4xx response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_on_4xx(provider: AnthropicProvider):
    """Should NOT retry on HTTP 400 — raise immediately."""
    error_resp = _FakeStreamResponse(status_code=400, body=b"Bad Request")
    fake_client = _FakeAsyncClient([error_resp])

    with patch("mas_agent.providers.anthropic_provider.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(RuntimeError, match="LLM API error 400"):
            async for _ in provider.stream_chat(messages=[{"role": "user", "content": "hi"}]):
                pass


# ---------------------------------------------------------------------------
# Test: Timeout configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_configuration(provider: AnthropicProvider):
    """Verify the httpx.AsyncClient is created with the correct timeout."""
    captured_timeout = None
    success_events = [
        {"type": "message_stop"},
    ]
    success_resp = _FakeStreamResponse(status_code=200, lines=_make_event_lines(success_events))

    class _CapturingClient:
        def __init__(self, timeout=None):
            nonlocal captured_timeout
            captured_timeout = timeout

        def stream(self, method, url, **kwargs):
            return _FakeStreamContextManager(success_resp)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    with patch("mas_agent.providers.anthropic_provider.httpx.AsyncClient", side_effect=_CapturingClient):
        async for _ in provider.stream_chat(messages=[{"role": "user", "content": "hi"}]):
            pass

    assert captured_timeout is not None
    assert captured_timeout.connect == 15
    assert captured_timeout.read == 120
    assert captured_timeout.write == 30
    assert captured_timeout.pool == 15
