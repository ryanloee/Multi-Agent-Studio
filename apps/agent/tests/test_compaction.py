"""Tests for the context compaction module."""
from __future__ import annotations

import json

import pytest

from mas_agent.compaction import (
    compact_messages,
    estimate_tokens,
    should_compact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _assistant_with_tool(tool_name: str, tool_id: str = "call_1") -> dict:
    """Return an assistant message that includes a tool_use block."""
    blocks = [
        {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {"path": "/tmp"}},
    ]
    return {"role": "assistant", "content": json.dumps(blocks)}


def _tool_result(tool_id: str = "call_1", content: str = "ok") -> dict:
    """Return a user message wrapping a tool_result block."""
    return {
        "role": "user",
        "content": json.dumps([
            {"type": "tool_result", "tool_use_id": tool_id, "content": content},
        ]),
    }


def _build_long_messages(n_turns: int = 5, chars_per_msg: int = 500) -> list[dict]:
    """Build a conversation with *n_turns* assistant+tool_result turns.

    Each assistant message contains *chars_per_msg* filler characters.
    """
    msgs: list[dict] = [_msg("user", "Fix all the bugs in the codebase.")]
    filler = "x" * chars_per_msg
    for i in range(n_turns):
        tool_id = f"call_{i}"
        msgs.append(_assistant_with_tool("bash", tool_id))
        msgs.append(_tool_result(tool_id, filler))
    return msgs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_basic_estimation(self):
        msgs = [_msg("user", "a" * 40)]
        assert estimate_tokens(msgs) == 10  # 40 / 4

    def test_empty_list(self):
        assert estimate_tokens([]) == 0


class TestShouldCompact:
    def test_triggers_when_over_threshold(self):
        # max_tokens = 100, threshold = 70 tokens
        # Need > 280 chars (70 * 4) to trigger
        msgs = [_msg("user", "a" * 300)]
        assert should_compact(msgs, max_tokens=100) is True

    def test_no_trigger_when_under_threshold(self):
        msgs = [_msg("user", "short message")]
        assert should_compact(msgs, max_tokens=10000) is False


class TestCompactMessages:
    """Test 2: Recent turns preserved."""

    def test_recent_turns_preserved(self):
        msgs = _build_long_messages(n_turns=5, chars_per_msg=300)
        compacted = compact_messages(msgs, max_tokens=500, keep_recent=2)

        # The last 2 complete turns (assistant + tool_result each) must appear
        # verbatim at the tail of the compacted list.
        original_tail = msgs[-4:]  # last 2 turns = 4 messages
        assert compacted[-4:] == original_tail

    """Test 3: Summary structure."""

    def test_summary_contains_context_block(self):
        msgs = _build_long_messages(n_turns=3, chars_per_msg=200)
        compacted = compact_messages(msgs, max_tokens=200, keep_recent=1)

        # Second message should be the summary
        summary_msg = compacted[1]
        assert summary_msg["role"] == "user"
        content = summary_msg["content"]
        assert "## Context Summary" in content
        assert "Original task:" in content
        assert "Fix all the bugs" in content
        assert "Tools used:" in content

    """Test 4: Tool output truncation."""

    def test_tool_output_truncation(self):
        long_output = "A" * 10000
        msgs = [
            _msg("user", "do something"),
            _assistant_with_tool("bash", "call_0"),
            _tool_result("call_0", long_output),
            _msg("assistant", "done"),
        ]
        # keep_recent=1 keeps the last turn (assistant "done"), so the
        # tool_result above goes into the middle and gets truncated.
        compacted = compact_messages(msgs, max_tokens=100, keep_recent=1)

        # Find the tool_result in the compacted output
        for msg in compacted:
            content = msg.get("content", "")
            if isinstance(content, str) and "tool_result" in content:
                parsed = json.loads(content)
                for block in parsed:
                    if block.get("type") == "tool_result":
                        assert len(block["content"]) < 10000

    """Test 5: No compaction when under limit."""

    def test_no_compaction_when_small(self):
        msgs = [
            _msg("user", "hello"),
            _msg("assistant", "world"),
        ]
        compacted = compact_messages(msgs, max_tokens=50000)
        # With only 2 messages (< 3), returned as-is
        assert compacted == msgs

    """Test 6: First message always preserved."""

    def test_first_message_preserved(self):
        original_prompt = "This is my very important original task prompt!"
        msgs = [_msg("user", original_prompt)]
        msgs.extend(_build_long_messages(n_turns=4, chars_per_msg=500)[1:])

        compacted = compact_messages(msgs, max_tokens=200, keep_recent=1)

        assert compacted[0]["role"] == "user"
        assert compacted[0]["content"] == original_prompt

    """Test 1 (full integration): Compaction trigger round-trip."""

    def test_compaction_trigger_integration(self):
        # Build messages that exceed 70% of max_tokens
        msgs = _build_long_messages(n_turns=6, chars_per_msg=500)
        max_tokens = 200  # threshold = 140 tokens = 560 chars

        assert should_compact(msgs, max_tokens) is True

        compacted = compact_messages(msgs, max_tokens, keep_recent=2)

        # Compacted should be strictly shorter than original
        assert len(compacted) < len(msgs)
        # And should now be under the threshold
        assert estimate_tokens(compacted) < estimate_tokens(msgs)

    def test_tool_names_collected_in_summary(self):
        msgs = [
            _msg("user", "task"),
            _assistant_with_tool("grep", "c0"),
            _tool_result("c0", "found"),
            _assistant_with_tool("edit", "c1"),
            _tool_result("c1", "done"),
            _msg("assistant", "all done"),
        ]
        # keep_recent=0 collapses everything into the summary (no turns kept)
        compacted = compact_messages(msgs, max_tokens=10, keep_recent=0)
        summary = compacted[1]["content"]
        assert "grep" in summary
        assert "edit" in summary

        # With keep_recent=1, only the last turn (edit) is kept verbatim,
        # so only grep appears in the summary's middle section.
        compacted_r1 = compact_messages(msgs, max_tokens=10, keep_recent=1)
        summary_r1 = compacted_r1[1]["content"]
        assert "grep" in summary_r1
