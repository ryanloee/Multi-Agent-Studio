"""Python agent loop — replaces opencode CLI + run-node.ts.

The AgentRunner directly calls LLM APIs and executes tools in-process,
eliminating the need for the Bun/TypeScript subprocess chain.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.agent_llm import LLMClient, LLMConnectionError, LLMError, LLMTimeoutError
from app.core.agent_tools import TOOLS, execute_tool, get_system_prompt

logger = logging.getLogger(__name__)

# Event emitter callback: (event_type, **kwargs) -> None
EventEmitter = Callable[..., Awaitable[None]]

# Default limits
DEFAULT_MAX_TURNS = 80
DEFAULT_TIMEOUT_SECONDS = 900  # 15 minutes of inactivity
SELF_REVIEW_MAX_TURNS = 10

# LLM retry settings
LLM_MAX_RETRIES = 10
LLM_RETRY_BASE_DELAY = 2.0  # seconds, exponential backoff: 2s, 4s, 8s, 16s ...


@dataclass
class AgentResult:
    """Result from a complete agent run."""
    success: bool = True
    output: str = ""
    error: str = ""
    turns_used: int = 0
    files_changed: list[str] = field(default_factory=list)


class _InactivityTimeoutError(Exception):
    """Raised when agent produces no output for the configured timeout."""


class AgentRunner:
    """Python agent loop — direct LLM API calls + in-process tool execution.

    Replaces: Bun.spawn(run-node.ts) -> Bun.spawn(opencode CLI) -> LLM API
    With:      AgentRunner.run() -> LLM API (httpx)
    """

    def __init__(self) -> None:
        self._llm = LLMClient()

    async def close(self) -> None:
        await self._llm.close()

    async def run(
        self,
        prompt: str,
        model_config: dict[str, Any],
        agent_type: str,
        workspace: str,
        emit: EventEmitter,
        run_id: str,
        node_id: str,
        system_prompt: str = "",
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        cancel_event: asyncio.Event | None = None,
        enable_self_review: bool = False,
    ) -> AgentResult:
        """Run the agent loop until completion or limits hit.

        Args:
            prompt: User prompt/task description.
            model_config: Dict with provider, model, url, key, etc.
            agent_type: Coder, explore, shell, etc. (for system prompt).
            workspace: Working directory for tool execution.
            emit: Async callback to emit events (event_type, run_id, node_id, ...).
            run_id: Run identifier for event routing.
            node_id: Node identifier for event routing.
            system_prompt: Optional custom system prompt override.
            max_turns: Maximum LLM call rounds.
            timeout_seconds: Max seconds of inactivity before timeout.
            cancel_event: Optional event to signal cancellation.

        Returns:
            AgentResult with success, output, turns_used, etc.
        """
        await emit("node_started", run_id=run_id, node_id=node_id,
                    content=f"Agent started: {agent_type}")

        result = AgentResult()
        start_time = time.monotonic()
        last_activity = time.monotonic()

        async def _emit_with_activity(event_type: str, **kwargs: Any) -> None:
            nonlocal last_activity
            last_activity = time.monotonic()
            await emit(event_type, **kwargs)

        try:
            result = await self._run_loop(
                prompt=prompt,
                model_config=model_config,
                agent_type=agent_type,
                workspace=workspace,
                emit=_emit_with_activity,
                run_id=run_id,
                node_id=node_id,
                system_prompt=system_prompt,
                max_turns=max_turns,
                cancel_event=cancel_event,
                result=result,
                start_time=start_time,
                timeout_seconds=timeout_seconds,
                last_activity_time=lambda: last_activity,
            )

            if result.success and enable_self_review and result.files_changed:
                result = await self._run_self_review(
                    result=result,
                    model_config=model_config,
                    workspace=workspace,
                    emit=_emit_with_activity,
                    run_id=run_id,
                    node_id=node_id,
                    cancel_event=cancel_event,
                    original_prompt=prompt,
                )
        except _InactivityTimeoutError:
            result.success = False
            elapsed = time.monotonic() - start_time
            result.error = (
                f"Agent timed out: no activity for {timeout_seconds}s "
                f"(total elapsed: {elapsed:.0f}s)"
            )
            logger.warning("Agent %s/%s timed out (inactivity %ds, elapsed %.0fs)",
                           run_id, node_id[:12], timeout_seconds, elapsed)
        except Exception as exc:
            result.success = False
            result.error = f"Agent error: {exc}"
            logger.exception("Agent %s/%s failed", run_id, node_id[:12])

        # Emit completion event
        if result.success:
            await emit("node_completed", run_id=run_id, node_id=node_id,
                        content=result.output[:2000] if result.output else "")
        else:
            await emit("node_failed", run_id=run_id, node_id=node_id,
                        content=result.error)

        await emit("status", run_id=run_id, node_id=node_id,
                    content="completed" if result.success else "failed")

        elapsed = time.monotonic() - start_time
        logger.info("Agent %s/%s finished: success=%s turns=%d elapsed=%.1fs",
                     run_id, node_id[:12], result.success, result.turns_used, elapsed)

        return result

    async def _run_self_review(
        self,
        result: AgentResult,
        model_config: dict[str, Any],
        workspace: str,
        emit: EventEmitter,
        run_id: str,
        node_id: str,
        cancel_event: asyncio.Event | None,
        original_prompt: str,
    ) -> AgentResult:
        """Run a self-review pass after worker completes, optimizing code quality."""
        from app.core.director_prompts import SELF_REVIEW_SYSTEM

        await emit("agent_status", run_id=run_id, node_id=node_id,
                    content="Self-review: optimizing code quality...")

        files_list = "\n".join(f"- {f}" for f in result.files_changed)
        review_prompt = (
            f"{SELF_REVIEW_SYSTEM}\n\n"
            f"## Files you just modified:\n{files_list}\n\n"
            f"## Original task:\n{original_prompt[:1000]}\n\n"
            f"Please review and optimize your recent changes. "
            f"Only modify the files listed above."
        )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": review_prompt},
        ]

        for turn in range(SELF_REVIEW_MAX_TURNS):
            if cancel_event and cancel_event.is_set():
                break

            response = None
            last_llm_error: LLMError | None = None
            for attempt in range(1, LLM_MAX_RETRIES + 1):
                try:
                    response = await self._llm.chat(
                        messages=messages,
                        tools=TOOLS,
                        system=SELF_REVIEW_SYSTEM,
                        model_config=model_config,
                    )
                    last_llm_error = None
                    break
                except (LLMConnectionError, LLMTimeoutError) as exc:
                    last_llm_error = exc
                    if attempt < LLM_MAX_RETRIES:
                        delay = min(LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1)), 30.0)
                        await asyncio.sleep(delay)
                except LLMError as exc:
                    last_llm_error = exc
                    break

            if response is None:
                logger.warning("Self-review LLM call failed: %s", last_llm_error)
                break

            if response.text:
                await emit("llm_token", run_id=run_id, node_id=node_id,
                            content=f"\n[Self-Review] {response.text}")

            if not response.tool_calls:
                break

            assistant_content: list[dict[str, Any]] = []
            if response.text:
                assistant_content.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: list[dict[str, Any]] = []
            for tc in response.tool_calls:
                if cancel_event and cancel_event.is_set():
                    break
                tool_name = tc["name"]
                tool_input = tc["input"]
                tool_id = tc["id"]

                await emit("tool_call", run_id=run_id, node_id=node_id,
                            tool_name=tool_name,
                            content=f"[Self-Review] {_summarize_tool_call(tool_name, tool_input)}")

                tool_output = await execute_tool(tool_name, tool_input, workspace)

                if tool_name in ("write", "edit"):
                    file_path = tool_input.get("path", "")
                    if file_path and file_path not in result.files_changed:
                        result.files_changed.append(file_path)

                await emit("tool_result", run_id=run_id, node_id=node_id,
                            tool_name=tool_name,
                            content=f"[Self-Review] {tool_output[:3000]}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": tool_output,
                })
            messages.append({"role": "user", "content": tool_results})

        result.output += "\n\n[Self-Review completed]"
        return result

    async def _run_loop(
        self,
        prompt: str,
        model_config: dict[str, Any],
        agent_type: str,
        workspace: str,
        emit: EventEmitter,
        run_id: str,
        node_id: str,
        system_prompt: str,
        max_turns: int,
        cancel_event: asyncio.Event | None,
        result: AgentResult,
        start_time: float,
        timeout_seconds: int,
        last_activity_time: Callable[[], float] | None = None,
    ) -> AgentResult:
        """Inner loop: call LLM -> execute tools -> repeat."""
        # Build system prompt
        sys_prompt = get_system_prompt(agent_type, system_prompt)

        # Add environment context
        env_info = self._build_env_info(workspace)
        sys_prompt = f"{sys_prompt}\n\n## Environment\n{env_info}"

        # Initialize conversation
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]
        pass_reasoning = bool(model_config.get("thinking_mode", True))
        # Track the last reasoning_content for MiMo/GLM APIs that require it
        _last_reasoning: str = ""

        for turn in range(max_turns):
            if cancel_event and cancel_event.is_set():
                result.success = False
                result.error = "Cancelled"
                return result

            # Check inactivity timeout
            if last_activity_time and (time.monotonic() - last_activity_time()) > timeout_seconds:
                raise _InactivityTimeoutError()

            # Emit turn status
            await emit("agent_status", run_id=run_id, node_id=node_id,
                        content=f"Turn {turn + 1}/{max_turns}")

            # Call LLM (with retry for transient errors)
            response = None
            last_llm_error: LLMError | None = None
            for attempt in range(1, LLM_MAX_RETRIES + 1):
                try:
                    response = await self._llm.chat(
                        messages=messages,
                        tools=TOOLS,
                        system=sys_prompt,
                        model_config=model_config,
                    )
                    last_llm_error = None
                    break
                except (LLMConnectionError, LLMTimeoutError) as exc:
                    last_llm_error = exc
                    if attempt < LLM_MAX_RETRIES:
                        delay = min(LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1)), 30.0)
                        logger.warning(
                            "LLM transient error (attempt %d/%d), retrying in %.1fs: %s",
                            attempt, LLM_MAX_RETRIES, delay, exc,
                        )
                        await emit(
                            "agent_status", run_id=run_id, node_id=node_id,
                            content=(
                                f"LLM connection error, retrying ({attempt}/{LLM_MAX_RETRIES})..."
                            ),
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "LLM transient error persisted after %d attempts: %s",
                            LLM_MAX_RETRIES, exc,
                        )
                except LLMError as exc:
                    # Non-transient errors (e.g. 4xx API errors) — fail immediately
                    last_llm_error = exc
                    break

            if response is None:
                result.success = False
                result.error = f"LLM error: {last_llm_error}"
                return result

            result.turns_used = turn + 1

            # Emit text output
            if response.text:
                await emit("llm_token", run_id=run_id, node_id=node_id,
                            content=response.text)
                result.output = response.text

            # Emit usage info
            if response.usage:
                await emit("agent_status", run_id=run_id, node_id=node_id,
                            content=f"Tokens: in={response.usage.get('input_tokens', '?')} "
                                    f"out={response.usage.get('output_tokens', '?')}")

            # Track reasoning_content from response
            if response.reasoning_content:
                _last_reasoning = response.reasoning_content

            # No tool calls → agent is done
            if not response.tool_calls:
                # Build assistant message with just text
                assistant_msg: dict[str, Any] = {
                    "role": "assistant", "content": response.text,
                }
                if pass_reasoning and _last_reasoning:
                    assistant_msg["reasoning_content"] = _last_reasoning
                messages.append(assistant_msg)
                result.success = True
                return result

            # Build assistant message with text + tool_use blocks
            assistant_content: list[dict[str, Any]] = []
            if response.text:
                assistant_content.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })
            assistant_msg = {"role": "assistant", "content": assistant_content}
            if pass_reasoning and _last_reasoning:
                assistant_msg["reasoning_content"] = _last_reasoning
            messages.append(assistant_msg)

            # Execute tools and collect results
            tool_results: list[dict[str, Any]] = []
            for tc in response.tool_calls:
                if cancel_event and cancel_event.is_set():
                    result.success = False
                    result.error = "Cancelled"
                    return result

                tool_name = tc["name"]
                tool_input = tc["input"]
                tool_id = tc["id"]

                await emit(
                    "tool_call",
                    run_id=run_id,
                    node_id=node_id,
                    tool_name=tool_name,
                    content=_summarize_tool_call(tool_name, tool_input),
                )

                # Execute
                tool_output = await execute_tool(tool_name, tool_input, workspace)

                # Track files changed
                if tool_name in ("write", "edit"):
                    file_path = tool_input.get("path", "")
                    if file_path and file_path not in result.files_changed:
                        result.files_changed.append(file_path)

                # Emit shell output if it's a shell command
                if tool_name == "shell":
                    await emit("shell_stdout", run_id=run_id, node_id=node_id,
                                content=tool_output[:10000])

                await emit("tool_result", run_id=run_id, node_id=node_id,
                            tool_name=tool_name, content=tool_output[:5000])

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": tool_output,
                })

            # Append tool results as user message
            messages.append({"role": "user", "content": tool_results})

        # Exhausted max turns
        result.success = True  # Not a failure, just hit the limit
        result.error = f"Reached max turns ({max_turns})"
        return result

    def _build_env_info(self, workspace: str) -> str:
        """Build environment context string for the system prompt."""
        parts = [
            f"Platform: {platform.system()} {platform.release()}",
            f"Working directory: {workspace}",
        ]

        # Detect available tools
        tools_avail = []
        for cmd in ("git", "node", "python", "pip", "npm", "pnpm", "cargo", "go"):
            import shutil
            if shutil.which(cmd):
                tools_avail.append(cmd)
        if tools_avail:
            parts.append(f"Available tools: {', '.join(tools_avail)}")

        # List workspace root
        try:
            from pathlib import Path
            ws = Path(workspace)
            if ws.is_dir():
                entries = sorted(p.name for p in ws.iterdir() if not p.name.startswith("."))[:20]
                if entries:
                    parts.append(f"Workspace contents: {', '.join(entries)}")
        except Exception:
            pass

        return "\n".join(parts)


def _summarize_tool_call(name: str, args: dict[str, Any]) -> str:
    """Create a short summary of a tool call for event display."""
    if name == "shell":
        cmd = args.get("command", "")
        return f"shell: {cmd[:200]}"
    elif name == "read":
        return f"read: {args.get('path', '')}"
    elif name == "write":
        path = args.get("path", "")
        content_len = len(args.get("content", ""))
        return f"write: {path} ({content_len} chars)"
    elif name == "edit":
        path = args.get("path", "")
        old = args.get("old_string", "")[:50]
        return f"edit: {path} ({old!r}...)"
    elif name == "glob":
        return f"glob: {args.get('pattern', '')}"
    elif name == "grep":
        return f"grep: {args.get('pattern', '')}"
    return f"{name}: {str(args)[:200]}"
