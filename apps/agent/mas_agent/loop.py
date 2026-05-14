"""AgentLoop — core agentic loop: LLM call → tool execution → repeat."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
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
        self.messages: list[dict[str, Any]] = []
        self.stream = StreamWriter(config.stream_dir, config.run_id, config.node_id)
        self._permission = PermissionChecker(self.stream, config.workspace)
        # Doom-loop detection state
        self._tool_call_history: list[tuple[str, str]] = []
        self._doom_warned: bool = False
        self._executed_tools: list[str] = []
        self._initial_workspace_snapshot = self._workspace_snapshot()

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

    def _latest_assistant_text(self) -> str:
        """Return the most recent assistant prose, excluding tool_use blocks."""
        for message in reversed(self.messages):
            if message.get("role") != "assistant":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                try:
                    blocks = json.loads(content)
                except json.JSONDecodeError:
                    return content.strip()
            else:
                blocks = content

            if isinstance(blocks, list):
                text = "".join(
                    str(block.get("text") or "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _is_internal_path(path: str) -> bool:
        normalized = path.replace(os.sep, "/").lstrip("/")
        return (
            normalized.startswith(".agent/")
            or normalized == ".agent"
            or normalized.startswith(".workflow/")
            or normalized == ".workflow"
            or normalized.startswith(".git/")
            or normalized == ".git"
        )

    def _workspace_snapshot(self) -> dict[str, tuple[int, int]]:
        """Return a cheap file snapshot for node-local change detection."""
        snapshot: dict[str, tuple[int, int]] = {}
        for root, dirs, files in os.walk(self.config.workspace):
            rel_root = os.path.relpath(root, self.config.workspace)
            rel_root = "" if rel_root == "." else rel_root
            dirs[:] = [
                d for d in dirs
                if not self._is_internal_path(os.path.join(rel_root, d))
            ]
            for filename in files:
                rel_path = os.path.join(rel_root, filename) if rel_root else filename
                if self._is_internal_path(rel_path):
                    continue
                full_path = os.path.join(self.config.workspace, rel_path)
                try:
                    stat = os.stat(full_path)
                except OSError:
                    continue
                snapshot[rel_path.replace(os.sep, "/")] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _workspace_has_node_changes(self) -> bool:
        """Return whether this loop changed visible workspace files."""
        return self._workspace_snapshot() != self._initial_workspace_snapshot

    def _completion_blocker(self, assistant_text: str) -> str | None:
        """Return a concrete reason the node cannot be considered complete."""
        agent_type = self.config.agent_type
        if not assistant_text.strip() and not self._executed_tools:
            return "模型没有产生可见文本，也没有调用任何工具。"

        if agent_type in {"design", "coder", "merge"}:
            if not self._workspace_has_node_changes():
                return (
                    f"{agent_type} 节点没有产生任何非 .agent/.workflow 的文件改动；"
                    "不能把空结果判定为完成。"
                )

        if agent_type == "shell" and "shell" not in self._executed_tools:
            return "shell 节点没有执行 shell 工具，不能判定为完成。"

        return None

    def _completion_retry_prompt(self, blocker: str) -> str:
        agent_type = self.config.agent_type
        if agent_type in {"design", "plan"}:
            action = "请立刻使用 write 工具把本节点的 Markdown 方案文档写入工作区。"
        elif agent_type in {"coder", "merge"}:
            action = "请立刻使用 read/glob/grep 检查工作区，并用 write/edit/apply_patch 产生真实代码或合并产物。"
        elif agent_type == "shell":
            action = "请立刻使用 shell 工具执行本节点要求的验证命令。"
        else:
            action = "请继续完成节点任务并输出明确结果。"

        return (
            "当前节点不能完成："
            f"{blocker}\n"
            f"{action}\n"
            "不要只解释计划；必须通过可用工具完成本节点。"
        )

    def _repair_plan_write_args(self, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Best-effort recovery for weak models that call write with `{}` in plan/design nodes."""
        if self.config.agent_type not in {"plan", "design"}:
            return args, []

        repaired = dict(args)
        repairs: list[str] = []

        if not repaired.get("path"):
            safe_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.config.node_id).strip("-")
            repaired["path"] = f"{safe_node_id or 'plan'}.md"
            repairs.append("plan_write[path]: defaulted to node markdown file")

        if not repaired.get("content"):
            assistant_text = self._latest_assistant_text()
            if assistant_text:
                repaired["content"] = assistant_text
                repairs.append("plan_write[content]: used latest assistant text")

        return repaired, repairs

    async def _execute_tool(self, tool_call: dict) -> str:
        """Execute a single tool call and return the result string."""
        # Repair common LLM formatting mistakes before lookup
        name, args, repairs = repair_tool_call(tool_call["name"], tool_call["input"])
        if name == "write":
            args, plan_write_repairs = self._repair_plan_write_args(args)
            repairs.extend(plan_write_repairs)
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

        self._executed_tools.append(name)
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
                    self.messages.append({"role": "assistant", "content": content})
                else:
                    self.messages.append({"role": "assistant", "content": assistant_text})

                if not tool_calls:
                    blocker = self._completion_blocker(assistant_text)
                    if blocker:
                        if turn < self.config.max_turns - 1:
                            self.stream.emit_status("completion_retry")
                            self.messages.append({
                                "role": "user",
                                "content": self._completion_retry_prompt(blocker),
                            })
                            continue
                        self.stream.emit_error(blocker)
                        self.stream.emit_status("failed")
                        return 1
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
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result,
                        }],
                    }
                    self.messages.append(tool_result_msg)

                # Compact message history if approaching the model context window.
                # max_tokens is output budget; context_window is the total input+output window.
                if should_compact(self.messages, self.config.context_window):
                    self.messages = compact_messages(self.messages, self.config.context_window)
                    self.stream.emit_status("context_compacted")

            final_text = self._latest_assistant_text()
            blocker = self._completion_blocker(final_text)
            if blocker:
                self.stream.emit_error(blocker)
                self.stream.emit_status("failed")
                return 1

            self.stream.emit_status("completed")
            return 0

        except Exception as e:
            logger.exception("Agent loop failed")
            self.stream.emit_error(str(e))
            self.stream.emit_status("failed")
            return 1

        finally:
            self.stream.close()
