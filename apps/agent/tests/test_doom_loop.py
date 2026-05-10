"""Tests for doom-loop detection in AgentLoop."""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mas_agent.loop import AgentLoop, _DOOM_FATAL_THRESHOLD, _DOOM_WARNING_THRESHOLD
from mas_agent.types import LoopConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path) -> LoopConfig:
    """Return a minimal LoopConfig pointing at *tmp_path*."""
    return LoopConfig(
        run_id="test-run",
        node_id="test-node",
        agent_type="coder",
        provider="mock",
        model="mock-model",
        prompt="Do something useful",
        max_turns=20,
        workspace=str(tmp_path),
        stream_dir=str(tmp_path / ".agent"),
    )


def _tool_call(name: str, args: dict | None = None, call_id: str = "") -> dict:
    """Build a tool_call dict matching the shape produced by _call_llm."""
    return {
        "id": call_id or f"call_{name}",
        "name": name,
        "input": args or {},
    }


class _FakeStreamWriter:
    """Lightweight stand-in for StreamWriter that captures emitted events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._closed = False

    def _record(self, **kw: Any) -> None:
        self.events.append(kw)

    def emit_status(self, content: str) -> None:
        self._record(type="status", content=content)

    def emit_error(self, content: str, **_: Any) -> None:
        self._record(type="error", content=content)

    def emit_llm_token(self, content: str) -> None:
        self._record(type="llm_token", content=content)

    def emit_thinking(self, content: str) -> None:
        self._record(type="thinking", content=content)

    def emit_tool_call(self, name: str, content: str, **_: Any) -> None:
        self._record(type="tool_call", name=name, content=content)

    def emit_tool_result(self, name: str, content: str, **_: Any) -> None:
        self._record(type="tool_result", name=name, content=content)

    def emit_shell_stdout(self, content: str) -> None:
        self._record(type="shell_stdout", content=content)

    def emit_shell_stderr(self, content: str) -> None:
        self._record(type="shell_stderr", content=content)

    def close(self) -> None:
        self._closed = True

    def status_events(self) -> list[str]:
        return [e["content"] for e in self.events if e.get("type") == "status"]


def _patched_loop(config: LoopConfig):
    """Return an AgentLoop whose _call_llm and _execute_tool are mocks.

    Also replaces StreamWriter with _FakeStreamWriter so we can inspect
    emitted events without touching the filesystem (beyond what the
    StreamWriter constructor tries to do — we patch the instance *after*
    construction).
    """
    loop = AgentLoop(config)
    fake_stream = _FakeStreamWriter()
    loop.stream = fake_stream  # type: ignore[assignment]
    return loop, fake_stream


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHashArgs:
    """Verify _hash_args produces deterministic, stable hashes."""

    def test_same_args_same_hash(self) -> None:
        a = AgentLoop._hash_args({"pattern": "foo", "path": "/src"})
        b = AgentLoop._hash_args({"path": "/src", "pattern": "foo"})
        assert a == b  # sort_keys makes order irrelevant

    def test_different_args_different_hash(self) -> None:
        a = AgentLoop._hash_args({"pattern": "foo"})
        b = AgentLoop._hash_args({"pattern": "bar"})
        assert a != b


class TestCheckDoomLoop:
    """Unit tests for _check_doom_loop without running the full loop."""

    def test_no_trigger_on_varied_calls(self, tmp_path) -> None:
        loop, stream = _patched_loop(_make_config(tmp_path))
        assert loop._check_doom_loop("grep", {"pattern": "a"}) is None
        assert loop._check_doom_loop("grep", {"pattern": "b"}) is None
        assert loop._check_doom_loop("cat", {"path": "f"}) is None
        assert "doom_loop_detected" not in stream.status_events()

    def test_warning_at_threshold(self, tmp_path) -> None:
        loop, stream = _patched_loop(_make_config(tmp_path))
        args = {"pattern": "stuck"}
        assert loop._check_doom_loop("grep", args) is None   # 1
        assert loop._check_doom_loop("grep", args) is None   # 2
        result = loop._check_doom_loop("grep", args)          # 3 — warning
        assert result == "warning"
        assert "doom_loop_detected" in stream.status_events()
        # Warning message should be appended to messages
        assert any("Warning" in m.get("content", "") for m in loop.messages)

    def test_fatal_at_threshold(self, tmp_path) -> None:
        loop, stream = _patched_loop(_make_config(tmp_path))
        args = {"pattern": "stuck"}
        for _ in range(_DOOM_WARNING_THRESHOLD):
            loop._check_doom_loop("grep", args)  # warnings
        # 4th call — still just a warning already given
        loop._check_doom_loop("grep", args)
        # 5th call — fatal
        result = loop._check_doom_loop("grep", args)
        assert result == "fatal"
        assert "doom_loop_fatal" in stream.status_events()

    def test_different_args_no_trigger(self, tmp_path) -> None:
        loop, stream = _patched_loop(_make_config(tmp_path))
        for pat in ("aaa", "bbb", "ccc"):
            assert loop._check_doom_loop("grep", {"pattern": pat}) is None
        assert "doom_loop_detected" not in stream.status_events()


class TestDoomLoopIntegration:
    """End-to-end tests patching _call_llm / _execute_tool."""

    @pytest.mark.asyncio
    async def test_normal_run_no_doom_loop(self, tmp_path) -> None:
        """Three different tool calls — no doom-loop events."""
        config = _make_config(tmp_path)
        loop, stream = _patched_loop(config)

        # Simulate 3 turns with different tools, then a final text-only turn
        llm_responses: list[tuple[str, list[dict]]] = [
            ("thinking", [_tool_call("grep", {"pattern": "a"})]),
            ("thinking", [_tool_call("grep", {"pattern": "b"})]),
            ("thinking", [_tool_call("cat", {"path": "f"})]),
            ("done", []),  # final turn, no tools
        ]

        with (
            patch.object(loop, "_call_llm", new_callable=AsyncMock, side_effect=llm_responses),
            patch.object(loop, "_execute_tool", new_callable=AsyncMock, return_value="ok"),
        ):
            exit_code = await loop.run()

        assert exit_code == 0
        assert "doom_loop_detected" not in stream.status_events()
        assert "doom_loop_fatal" not in stream.status_events()

    @pytest.mark.asyncio
    async def test_warning_injected_on_3_identical(self, tmp_path) -> None:
        """After 3 identical tool calls, a warning message is injected."""
        config = _make_config(tmp_path)
        loop, stream = _patched_loop(config)

        args = {"pattern": "stuck"}
        llm_responses: list[tuple[str, list[dict]]] = [
            ("", [_tool_call("grep", args, "c1")]),
            ("", [_tool_call("grep", args, "c2")]),
            ("", [_tool_call("grep", args, "c3")]),
            # After the warning the LLM tries something different
            ("ok done", []),
        ]

        with (
            patch.object(loop, "_call_llm", new_callable=AsyncMock, side_effect=llm_responses),
            patch.object(loop, "_execute_tool", new_callable=AsyncMock, return_value="no match"),
        ):
            exit_code = await loop.run()

        assert exit_code == 0
        assert "doom_loop_detected" in stream.status_events()
        # Verify the warning message exists in messages
        warning_msgs = [
            m for m in loop.messages
            if m["role"] == "user" and "Warning" in m.get("content", "")
        ]
        assert len(warning_msgs) >= 1

    @pytest.mark.asyncio
    async def test_fatal_on_5_identical(self, tmp_path) -> None:
        """After 5 identical tool calls, the loop terminates with exit code 1."""
        config = _make_config(tmp_path)
        loop, stream = _patched_loop(config)

        args = {"pattern": "stuck"}
        # 5 identical tool call turns
        llm_responses: list[tuple[str, list[dict]]] = [
            ("", [_tool_call("grep", args, f"c{i}")])
            for i in range(_DOOM_FATAL_THRESHOLD)
        ]

        with (
            patch.object(loop, "_call_llm", new_callable=AsyncMock, side_effect=llm_responses),
            patch.object(loop, "_execute_tool", new_callable=AsyncMock, return_value="no match"),
        ):
            exit_code = await loop.run()

        assert exit_code == 1
        assert "doom_loop_fatal" in stream.status_events()

    @pytest.mark.asyncio
    async def test_similar_but_different_args_no_trigger(self, tmp_path) -> None:
        """Three grep calls with different patterns should NOT trigger."""
        config = _make_config(tmp_path)
        loop, stream = _patched_loop(config)

        llm_responses: list[tuple[str, list[dict]]] = [
            ("", [_tool_call("grep", {"pattern": "alpha"})]),
            ("", [_tool_call("grep", {"pattern": "beta"})]),
            ("", [_tool_call("grep", {"pattern": "gamma"})]),
            ("done", []),
        ]

        with (
            patch.object(loop, "_call_llm", new_callable=AsyncMock, side_effect=llm_responses),
            patch.object(loop, "_execute_tool", new_callable=AsyncMock, return_value="result"),
        ):
            exit_code = await loop.run()

        assert exit_code == 0
        assert "doom_loop_detected" not in stream.status_events()
        assert "doom_loop_fatal" not in stream.status_events()

    @pytest.mark.asyncio
    async def test_history_trimmed(self, tmp_path) -> None:
        """History stays bounded at _DOOM_HISTORY_SIZE entries."""
        from mas_agent.loop import _DOOM_HISTORY_SIZE

        config = _make_config(tmp_path)
        loop, _ = _patched_loop(config)

        for i in range(_DOOM_HISTORY_SIZE + 5):
            loop._check_doom_loop("tool", {"i": i})

        assert len(loop._tool_call_history) <= _DOOM_HISTORY_SIZE
