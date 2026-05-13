"""AgentLoop — core agentic loop: LLM call → tool execution → repeat."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from mas_agent.compaction import should_compact, compact_messages
from mas_agent.events import StreamWriter
from mas_agent.permission import PermissionAction, PermissionChecker
from mas_agent.providers.base import StreamChunk
from mas_agent.providers import create_provider
from mas_agent.prompts import load_prompt
from mas_agent.tool_repair import repair_tool_call
from mas_agent.tools import ToolRegistry
from mas_agent.types import LoopConfig, Message

logger = logging.getLogger(__name__)

# Doom-loop detection thresholds
_DOOM_WARNING_THRESHOLD = 3   # consecutive identical calls before warning
_DOOM_FATAL_THRESHOLD = 5     # consecutive identical calls before termination
_DOOM_HISTORY_SIZE = 10       # max entries kept in tool-call history


class AgentLoop:
    """Runs the LLM ↔ tool execution cycle for a single agent node."""

    def __init__(self, config: LoopConfig) -> None:
        self.config = config
        self.messages: list[dict[str, str]] = []
        self.stream = StreamWriter(config.stream_dir, config.run_id, config.node_id)
        self._permission = PermissionChecker(self.stream, config.workspace)
        # Doom-loop detection state
        self._tool_call_history: list[tuple[str, str]] = []
        self._doom_warned: bool = False

    def _build_system_prompt(self) -> str:
        return load_prompt(
            self.config.agent_type,
            workspace=self.config.workspace,
        )

    def _get_tools(self) -> list[dict]:
        return ToolRegistry.for_agent_type(self.config.agent_type)

    async def _call_llm(self, system: str, tools: list[dict]) -> tuple[str, list[dict]]:
        """Call the LLM and return (assistant_text, tool_calls_to_execute).

        Streams tokens to StreamWriter and accumulates tool calls.
        """
        provider = create_provider(
            self.config.provider,
            self.config.model,
            self.config.provider_url,
            self.config.provider_key,
        )

        assistant_text = ""
        tool_calls: list[dict] = []

        async for chunk in provider.stream_chat(
            messages=self.messages,
            system=system,
            tools=tools if tools else None,
            max_tokens=self.config.max_tokens,
            thinking_level=self.config.thinking_level,
        ):
            if chunk.type == "text":
                assistant_text += chunk.text
                self.stream.emit_llm_token(chunk.text)

            elif chunk.type == "thinking":
                self.stream.emit_thinking(chunk.text)

            elif chunk.type == "tool_use":
                tool_calls.append({
                    "id": chunk.tool_call_id,
                    "name": chunk.tool_name,
                    "input": chunk.tool_input or {},
                })

            elif chunk.type == "error":
                self.stream.emit_error(chunk.text)
                raise RuntimeError(f"LLM error: {chunk.text}")

        return assistant_text, tool_calls

    @staticmethod
    def _hash_args(args: dict) -> str:
        """Return a stable MD5 hex-digest for tool arguments."""
        return hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()

    def _check_doom_loop(self, tool_name: str, args: dict) -> str | None:
        """Check for repeated identical tool calls.

        Returns ``"warning"`` if a doom-loop warning was injected,
        ``"fatal"`` if the loop must terminate, or ``None`` if everything
        looks normal.
        """
        args_hash = self._hash_args(args)
        self._tool_call_history.append((tool_name, args_hash))
        # Trim to bounded size
        if len(self._tool_call_history) > _DOOM_HISTORY_SIZE:
            self._tool_call_history = self._tool_call_history[-_DOOM_HISTORY_SIZE:]

        # Count consecutive identical entries at the tail
        consecutive = 0
        for i in range(len(self._tool_call_history) - 1, -1, -1):
            if self._tool_call_history[i] == (tool_name, args_hash):
                consecutive += 1
            else:
                break

        if consecutive >= _DOOM_FATAL_THRESHOLD:
            self.stream.emit_status("doom_loop_fatal")
            return "fatal"

        if consecutive >= _DOOM_WARNING_THRESHOLD and not self._doom_warned:
            self.stream.emit_status("doom_loop_detected")
            self.messages.append({
                "role": "user",
                "content": (
                    "Warning: you have called the same tool with the same "
                    "arguments 3 times in a row. Please try a different approach."
                ),
            })
            self._doom_warned = True
            return "warning"

        return None

    async def _execute_tool(self, tool_call: dict) -> str:
        """Execute a single tool call and return the result string."""
        # Repair common LLM formatting mistakes before lookup
        name, args, repairs = repair_tool_call(tool_call["name"], tool_call["input"])
        if repairs:
            self.stream.emit_tool_call(name, json.dumps({"repairs": repairs})[:2000])

        tool = ToolRegistry.get(name)

        if not tool:
            return f"Error: unknown tool '{name}'"

        # Agent-type-level parameter validation
        warnings = ToolRegistry.validate_execution(
            self.config.agent_type, name, args
        )
        if warnings:
            if "Permission denied" in warnings[0]:
                logger.warning("Tool blocked: %s", warnings[0])
                self.stream.emit_tool_call(name, json.dumps(args, ensure_ascii=False)[:2000])
                msg = f"BLOCKED: {warnings[0]}"
                self.stream.emit_tool_result(name, msg)
                return msg
            # Soft constraints — log but allow execution
            for w in warnings:
                logger.warning("Tool warning: %s", w)

        # Permission check — gate execution on security-sensitive operations
        action = await self._permission.check(name, args)
        if action == PermissionAction.DENY:
            msg = f"Permission denied: {name} on target"
            self.stream.emit_tool_call(name, json.dumps(args, ensure_ascii=False)[:2000])
            self.stream.emit_tool_result(name, msg)
            return msg
        if action == PermissionAction.ASK:
            self.stream.emit_tool_call(name, json.dumps(args, ensure_ascii=False)[:2000])
            approved = await self._permission.wait_for_approval(name, args)
            if not approved:
                msg = f"Permission denied (not approved): {name} on target"
                self.stream.emit_tool_result(name, msg)
                return msg

        self.stream.emit_tool_call(name, json.dumps(args, ensure_ascii=False)[:2000])

        try:
            result = await tool.execute(args, self.config.workspace)
        except Exception as e:
            result = f"Error executing {name}: {e}"

        self.stream.emit_tool_result(name, result[:5000])
        return result

    async def run(self) -> int:
        """Run the agent loop. Returns exit code (0=success, 1=failure)."""
        system = self._build_system_prompt()
        tools = self._get_tools()
        prompt = self.config.prompt

        self.messages.append({"role": "user", "content": prompt})
        self.stream.emit_status("running")

        try:
            for turn in range(self.config.max_turns):
                assistant_text, tool_calls = await self._call_llm(system, tools)

                # Build the assistant message content blocks
                if tool_calls:
                    content: list[dict] = []
                    if assistant_text:
                        content.append({"type": "text", "text": assistant_text})
                    for tc in tool_calls:
                        content.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        })
                    self.messages.append({"role": "assistant", "content": json.dumps(content)})
                else:
                    self.messages.append({"role": "assistant", "content": assistant_text})

                # No tool calls → we're done
                if not tool_calls:
                    break

                # Execute all tool calls and add results
                for tc in tool_calls:
                    # Doom-loop check before execution
                    doom_status = self._check_doom_loop(tc["name"], tc["input"])
                    if doom_status == "fatal":
                        self.stream.emit_status("failed")
                        return 1

                    result = await self._execute_tool(tc)
                    tool_result_msg = {
                        "role": "user",
                        "content": json.dumps([{
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result,
                        }]),
                    }
                    self.messages.append(tool_result_msg)

                # Compact message history if approaching the model context window.
                # max_tokens is output budget; context_window is the total input+output window.
                if should_compact(self.messages, self.config.context_window):
                    self.messages = compact_messages(self.messages, self.config.context_window)
                    self.stream.emit_status("context_compacted")

            self.stream.emit_status("completed")
            return 0

        except Exception as e:
            logger.exception("Agent loop failed")
            self.stream.emit_error(str(e))
            self.stream.emit_status("failed")
            return 1

        finally:
            self.stream.close()
