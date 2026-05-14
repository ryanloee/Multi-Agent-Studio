"""Context compaction — compress message history when it nears the context window.

Uses simple heuristic estimation (chars / 4) and string extraction rather than
LLM-generated summaries.  The goal is to keep the original prompt and the most
recent turns intact while collapsing everything in between into a compact
structured summary block.
"""
from __future__ import annotations

import json
from typing import Any

from mas_agent.tools.output_utils import truncate_output


def estimate_tokens(messages: list[dict]) -> int:
    """Return a rough token estimate: total characters / 4."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Content blocks (tool_use, tool_result, etc.)
            total_chars += len(json.dumps(content, ensure_ascii=False))
    return total_chars // 4


def should_compact(messages: list[dict], max_tokens: int) -> bool:
    """Return True when estimated tokens exceed 70% of *max_tokens*."""
    return estimate_tokens(messages) > max_tokens * 0.7


def _extract_text(msg: dict) -> str:
    """Extract plain text from a message content field."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Concatenate all text-type blocks
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append(block.get("content", ""))
        return "\n".join(parts)
    return str(content)


def _extract_tool_names(messages: list[dict]) -> list[str]:
    """Collect unique tool names from tool_use blocks across all messages."""
    names: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            try:
                blocks = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            blocks = content
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name and name not in seen:
                        names.append(name)
                        seen.add(name)
    return names


def _truncate_tool_results(messages: list[dict], max_chars: int = 2000) -> list[dict]:
    """Return a copy of *messages* with oversized tool_result content truncated."""
    result: list[dict] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            try:
                blocks = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                result.append(msg)
                continue
            if not isinstance(blocks, list):
                result.append(msg)
                continue
            stringify = True
        else:
            blocks = content
            stringify = False
            if not isinstance(blocks, list):
                result.append(msg)
                continue

        modified = False
        next_blocks: list[Any] = []
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
                and len(block["content"]) > max_chars
            ):
                block = {
                    **block,
                    "content": truncate_output(block["content"], max_chars=max_chars),
                }
                modified = True
            next_blocks.append(block)

        if not modified:
            result.append(msg)
        elif stringify:
            result.append({"role": msg["role"], "content": json.dumps(next_blocks, ensure_ascii=False)})
        else:
            result.append({"role": msg["role"], "content": next_blocks})
    return result


def compact_messages(
    messages: list[dict],
    max_tokens: int,
    keep_recent: int = 2,
) -> list[dict]:
    """Compact *messages* by summarising the middle section.

    Layout of the returned list::

        [0]  original user prompt  (always kept verbatim)
        [1]  user message with structured context summary
        [2:] last *keep_recent* complete turns
    """
    if len(messages) < 3:
        # Not enough to compact — return as-is (but still truncate tool results)
        return _truncate_tool_results(messages)

    # --- Identify the boundary for recent turns ---
    # A "complete turn" = assistant message + the following user/tool_result message.
    # Walk backwards from the end to find `keep_recent` complete turns.
    recent_start = len(messages)
    turns_found = 0
    i = len(messages) - 1
    while i >= 1 and turns_found < keep_recent:
        # The user/tool_result message that follows an assistant message
        if messages[i].get("role") == "user" and i - 1 >= 0 and messages[i - 1].get("role") == "assistant":
            turns_found += 1
            recent_start = i - 1
            i -= 2
        else:
            i -= 1

    first_msg = messages[0]
    middle = messages[1:recent_start]
    recent = messages[recent_start:]

    # --- Build the summary from the middle section ---
    original_task = _extract_text(first_msg)[:500]
    assistant_texts: list[str] = []
    for msg in middle:
        if msg.get("role") == "assistant":
            text = _extract_text(msg)
            if text:
                assistant_texts.append(text)

    previous_work = "\n".join(assistant_texts)[:2000]
    tool_names = _extract_tool_names(middle)

    summary_lines = [
        "## Context Summary",
        f"- Original task: {original_task}",
        f"- Previous work summary: {previous_work}",
        f"- Tools used: {', '.join(tool_names) if tool_names else 'none'}",
    ]
    summary_content = "\n".join(summary_lines)

    summary_msg: dict[str, Any] = {"role": "user", "content": summary_content}

    compacted = [first_msg, summary_msg] + recent
    return _truncate_tool_results(compacted)
