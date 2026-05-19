"""DirectorLoop — the agentic dispatch loop engine.

Replaces the static DAG executor with a Director agent that:
  1. Reads a compressed world model
  2. Calls a strong LLM with tool-use to decide the next action
  3. Dispatches sub-agents (scout / worker / tester) via NodeRunner
  4. Parses structured results, updates the world model
  5. Commits changes, repeats until done or max iterations reached

All sub-agents work on the same sandbox (trunk-based development).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.director_prompts import (
    DIRECTOR_SYSTEM,
    SCOUT_SYSTEM,
    TESTER_SYSTEM,
    WORKER_SYSTEM,
)
from app.core.director_tools import DIRECTOR_TOOL_CHOICE, DIRECTOR_TOOLS
from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.node_runner import NodeResult, NodeRunner
from app.core.world_model import WorldModel
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)

# Debug logger — detailed runtime tracing
import app.core.debug_logger as _dbg


def _extract_structured_block(text: str, block_name: str) -> dict | None:
    """Extract a JSON block delimited by ===BLOCK_NAME=== ... ===END_BLOCK_NAME===."""
    start_marker = f"==={block_name}==="
    end_marker = f"===END_{block_name}==="
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return None

    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        # Try to grab everything after start marker to end of text
        json_text = text[start_idx + len(start_marker):]
    else:
        json_text = text[start_idx + len(start_marker):end_idx]
    json_text = json_text.strip()
    if not json_text:
        return None
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        # Try to find the first { ... } block
        brace_start = json_text.find("{")
        if brace_start == -1:
            return None
        brace_end = json_text.rfind("}")
        if brace_end == -1:
            return None
        try:
            return json.loads(json_text[brace_start:brace_end + 1])
        except json.JSONDecodeError:
            return None


def _parse_free_text_decision(text: str) -> dict | None:
    """Try to extract a decision from free-text when model doesn't use tools."""
    if not text or not text.strip():
        return None

    # Try to find JSON with action field
    for match in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, dict) and data.get("action"):
                return data
        except json.JSONDecodeError:
            pass

    # Try raw JSON extraction
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        try:
            data = json.loads(text[first:last + 1])
            if isinstance(data, dict) and data.get("action"):
                return data
        except json.JSONDecodeError:
            pass

    # Last resort: infer action from text keywords
    text_lower = text.lower()
    if any(kw in text_lower for kw in ("任务完成", "目标达成", "all done", "goal achieved", "finished")):
        return {"action": "done", "reasoning": text[:200], "prompt": "", "task_id": "auto-done"}
    if any(kw in text_lower for kw in ("失败", "无法", "blocked", "stuck", "cannot proceed")):
        return {"action": "failed", "reasoning": text[:200], "prompt": "", "task_id": "auto-failed"}

    # Default: dispatch a worker with the full text as prompt
    return {"action": "worker", "reasoning": "Director free-text fallback", "prompt": text[:1000], "task_id": "fallback"}


def _extract_llm_text(jsonl_content: str) -> str:
    """Extract LLM text and tool call results from stream.jsonl event lines.

    If *jsonl_content* is not valid JSONL (i.e. no lines parsed successfully),
    the raw text is returned as-is — it is the agent's final LLM output.
    """
    parts: list[str] = []
    parsed_any = False
    for line in jsonl_content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_any = True
        event_type = ev.get("type", "")
        content = ev.get("content", "")

        # Extract LLM text chunks
        if event_type in ("llm_token", "llm_chunk", "text"):
            parts.append(content)
        # Extract tool call results for context
        elif event_type == "tool_call":
            tool_name = ev.get("tool_name", "tool")
            parts.append(f"\n[Tool Call: {tool_name}]\n{content}\n")
        elif event_type == "tool_result":
            tool_name = ev.get("tool_name", "tool")
            parts.append(f"\n[Tool Result: {tool_name}]\n{content}\n")
        elif event_type in ("shell_stdout", "shell_stderr"):
            parts.append(f"\n[Shell Output]\n{content}\n")

    # If no JSONL lines were parsed, input is plain text (agent's final output)
    if not parsed_any:
        return jsonl_content

    return "".join(parts)


class DirectorLoop:
    """Agentic dispatch loop that replaces the static DAG executor."""

    def __init__(
        self,
        sandbox: LocalSandbox,
        event_bus: InProcessEventBus,
        checkpoint: GitCheckpointManager,
        provisioner: SandboxProvisioner,
        node_runner: NodeRunner,
    ):
        self._sandbox = sandbox
        self._event_bus = event_bus
        self._checkpoint = checkpoint
        self._provisioner = provisioner
        self._node_runner = node_runner

        # run_id -> {"status": str, "task": asyncio.Task, "cancel_event": asyncio.Event, ...}
        self._runs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API (same interface as old LocalDAGExecutor)
    # ------------------------------------------------------------------

    async def start_run(
        self,
        run_id: str,
        dag_json: dict,
        global_config: dict | None = None,
        workspace_directory: str | None = None,
    ) -> str:
        cancel_event = asyncio.Event()
        self._runs[run_id] = {
            "status": "running",
            "task": None,
            "cancel_event": cancel_event,
            "global_config": global_config or {},
            "workspace_directory": workspace_directory,
            "kind": "director",
        }

        task = asyncio.create_task(
            self._dispatch_loop(
                run_id, dag_json, global_config or {}, cancel_event,
                workspace_directory=workspace_directory,
            ),
            name=f"director-loop-{run_id}",
        )

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.exception("Director loop failed for run %s", run_id)

        task.add_done_callback(_on_done)
        self._runs[run_id]["task"] = task
        logger.info("Director loop started for run %s", run_id)
        return run_id

    async def recover_interrupted_runs(self) -> None:
        """Mark stale running runs as failed — we cannot resume a Director loop."""
        try:
            from sqlalchemy import select
            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel

            async with async_session_factory() as session:
                result = await session.execute(
                    select(RunModel).where(
                        RunModel.status.in_(("running", "pending", "cancelling"))
                    )
                )
                for run in result.scalars().all():
                    run.status = "failed"
                await session.commit()
        except Exception:
            logger.warning("Failed to recover interrupted runs", exc_info=True)

    async def get_status(self, run_id: str) -> dict:
        run = self._runs.get(run_id)
        if not run:
            return {"status": "unknown"}
        return {"status": run["status"]}

    async def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run and run["status"] in ("running",):
            run["cancel_event"].set()
            run["status"] = "cancelling"

    # ------------------------------------------------------------------
    # Core dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(
        self,
        run_id: str,
        dag_json: dict,
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
    ) -> None:
        """Main Director loop: call LLM → dispatch sub-agent → update world model → repeat."""
        goal = global_config.get("_goal", "") or dag_json.get("metadata", {}).get("goal", "")
        if not goal:
            goal = "Complete the workflow tasks."

        _dbg.info(__name__, "Director dispatch loop starting", run_id=run_id, goal=goal[:200])

        # Build agent_type -> [node_id, ...] mapping from DAG so events use
        # the same IDs as the canvas nodes (enables run-status animations).
        dag_node_map: dict[str, list[str]] = {}
        for n in (dag_json.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            at = str(
                n.get("agent_type")
                or (n.get("data") or {}).get("agentType")
                or n.get("type")
                or "coder"
            ).lower()
            dag_node_map.setdefault(at, []).append(n.get("id", ""))

        _dbg.debug(__name__, "DAG node map built", dag_node_map={k: list(v) for k, v in dag_node_map.items()})

        # Create shared sandbox for trunk-based development
        sandbox_id: str | None = None
        try:
            sandbox_id = await self._sandbox.create(
                f"ws-director-{run_id[:8]}",
                template_dir=workspace_directory,
                user_workspace=workspace_directory,
            )
            logger.info("Created shared sandbox %s for director run %s", sandbox_id[:12], run_id)
        except Exception:
            logger.exception("Failed to create sandbox for director run %s", run_id)
            await self._finish_run(run_id, "failed", "Failed to create sandbox")
            return

        world = WorldModel(goal=goal)

        # Initial scout to understand project structure
        world.iteration = 0
        world.max_iterations = global_config.get("max_iterations", 30)

        try:
            # Step 1: Initial scout
            await self._emit("director_decision", run_id, "director",
                             action="scout", reasoning="Initial project reconnaissance",
                             task_id="init-scout", iteration=0)

            scout_result = await self._run_sub_agent(
                run_id=run_id,
                agent_type="explore",
                system_prompt=SCOUT_SYSTEM,
                prompt=f"Investigate the project structure and report findings.\n\nGoal: {goal}",
                sandbox_id=sandbox_id,
                cancel_event=cancel_event,
                model_provider=global_config.get("director_model_provider", ""),
                model_id=global_config.get("director_model_id", ""),
                dag_node_map=dag_node_map,
                workspace_directory=workspace_directory,
                world=world,
            )

            logger.info(
                "Scout result for run %s: state=%s error=%s raw_output_len=%d",
                run_id, scout_result.state, (scout_result.error or "")[:100],
                len(scout_result.raw_output or ""),
            )
            _dbg.log_node_lifecycle(
                __name__, node_id="init-scout", agent_type="explore",
                event="completed" if scout_result.state == "completed" else "failed",
                error=(scout_result.error or "")[:500],
                raw_output_len=len(scout_result.raw_output or ""),
            )
            if scout_result.state == "completed" and scout_result.raw_output:
                llm_text = _extract_llm_text(scout_result.raw_output)
                findings = _extract_structured_block(llm_text, "SCOUT_FINDINGS")
                if findings:
                    world.project_structure = findings.get("summary", llm_text[:2000])
                else:
                    world.project_structure = llm_text[:2000]
                world.record_success("init-scout", "scout", "Initial project reconnaissance completed")
            else:
                world.record_failure("init-scout", "scout", scout_result.error or "Scout failed")
                world.project_structure = "(scout failed — project structure unknown)"

            # Update file snapshot
            world.current_file_snapshot = await self._git_diff_stat(sandbox_id)
            world.iteration = 1

            # Step 2: Main dispatch loop
            consecutive_no_decision = 0
            last_decision_error = ""
            while world.iteration <= world.max_iterations and not cancel_event.is_set():
                # Call Director LLM
                decision, decision_error = await self._call_director_llm(
                    run_id=run_id,
                    world_model=world,
                    global_config=global_config,
                )

                if decision is None:
                    consecutive_no_decision += 1
                    last_decision_error = decision_error
                    logger.warning("Director LLM returned no decision for run %s (consecutive=%d, error=%s)", run_id, consecutive_no_decision, decision_error[:200])
                    if consecutive_no_decision >= 3:
                        await self._finish_run(run_id, "failed", f"Director LLM 连续 3 次未返回有效决策\n{last_decision_error}")
                        return
                    world.iteration += 1
                    continue
                consecutive_no_decision = 0

                action = decision.get("action", "")
                prompt = decision.get("prompt", "")
                task_id = decision.get("task_id", f"step-{world.iteration}")
                reasoning = decision.get("reasoning", "")
                target_files = decision.get("target_files", [])

                await self._emit(
                    "director_decision", run_id, "director",
                    action=action,
                    reasoning=reasoning,
                    task_id=task_id,
                    target_files=target_files,
                    iteration=world.iteration,
                )

                if action == "done":
                    world.record_success(task_id, "done", reasoning)
                    await self._finish_run(run_id, "completed", reasoning)
                    return

                if action == "failed":
                    world.record_failure(task_id, "failed", reasoning)
                    await self._finish_run(run_id, "failed", reasoning)
                    return

                # Dispatch sub-agent
                if action == "scout":
                    agent_type = "explore"
                    system_prompt = SCOUT_SYSTEM
                elif action == "worker":
                    agent_type = "coder"
                    system_prompt = WORKER_SYSTEM
                elif action == "test":
                    agent_type = "shell"
                    system_prompt = TESTER_SYSTEM
                else:
                    logger.warning("Unknown action '%s' from Director, skipping", action)
                    world.iteration += 1
                    continue

                sub_result = await self._run_sub_agent(
                    run_id=run_id,
                    agent_type=agent_type,
                    system_prompt=system_prompt,
                    prompt=self._build_sub_prompt(prompt, target_files, world, action),
                    sandbox_id=sandbox_id,
                    cancel_event=cancel_event,
                    model_provider=global_config.get("worker_model_provider", ""),
                    model_id=global_config.get("worker_model_id", ""),
                    dag_node_map=dag_node_map,
                    workspace_directory=workspace_directory,
                    world=world,
                )

                # Parse result
                self._update_world_from_result(world, task_id, action, sub_result)

                # Git commit after worker success
                if action == "worker" and sub_result.state == "completed":
                    try:
                        await self._checkpoint.auto_commit(
                            sandbox_id,
                            message=f"director: {task_id} — {reasoning[:80]}",
                        )
                    except Exception:
                        pass

                # Update file snapshot
                world.current_file_snapshot = await self._git_diff_stat(sandbox_id)
                world.iteration += 1

            # Loop ended without done/failed
            if cancel_event.is_set():
                await self._finish_run(run_id, "cancelled", "Cancelled by user")
            else:
                await self._finish_run(run_id, "failed", f"Max iterations ({world.max_iterations}) reached")

        except asyncio.CancelledError:
            await self._finish_run(run_id, "cancelled", "Cancelled")
        except Exception as exc:
            logger.exception("Director loop crashed for run %s", run_id)
            await self._finish_run(run_id, "failed", f"Director loop error: {exc}")
        finally:
            if sandbox_id:
                if workspace_directory:
                    try:
                        await self._sandbox.sync_back(sandbox_id, workspace_directory)
                    except Exception:
                        logger.warning("Final sync_back failed for director run %s", run_id, exc_info=True)
                try:
                    await self._sandbox.destroy(sandbox_id)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Sub-agent execution
    # ------------------------------------------------------------------

    async def _run_sub_agent(
        self,
        run_id: str,
        agent_type: str,
        system_prompt: str,
        prompt: str,
        sandbox_id: str,
        cancel_event: asyncio.Event,
        model_provider: str = "",
        model_id: str = "",
        dag_node_map: dict[str, list[str]] | None = None,
        workspace_directory: str | None = None,
        world: WorldModel | None = None,
    ) -> NodeResult:
        """Dispatch a sub-agent via NodeRunner on the shared sandbox."""
        # Prefer DAG node IDs so canvas status animations work
        node_id = ""
        if dag_node_map:
            candidates = dag_node_map.get(agent_type, [])
            if candidates:
                node_id = candidates.pop(0)

        # Reuse failed node ID when Director retries the same action type
        if not node_id and world:
            for attempt in reversed(world.failed_attempts):
                if attempt.action == agent_type:
                    node_id = attempt.task_id
                    break

        if not node_id:
            node_id = f"{agent_type}-{uuid4().hex[:8]}"

        # Emit retry event so frontend resets node status from "failed" to "running"
        is_retry = world and any(a.task_id == node_id for a in world.failed_attempts)
        if is_retry:
            await self._emit("node_retried", run_id, node_id)

        full_prompt = f"{system_prompt}\n\n---\n\n## Task\n{prompt}"

        _dbg.log_node_lifecycle(
            __name__, node_id=node_id, agent_type=agent_type, event="started",
            model_provider=model_provider, model_id=model_id,
            prompt_preview=prompt[:300],
        )

        result = await self._node_runner.execute_node(
            run_id=run_id,
            node_id=node_id,
            agent_type=agent_type,
            prompt=full_prompt,
            sandbox_id=sandbox_id,
            workspace_directory=workspace_directory,
            cancel_event=cancel_event,
            model_provider=model_provider,
            model_id=model_id,
            destroy_sandbox=False,  # trunk-based: never destroy shared sandbox
        )

        _dbg.log_node_lifecycle(
            __name__, node_id=node_id, agent_type=agent_type,
            event=result.state, exit_code=result.exit_code,
            error=(result.error or "")[:500],
            raw_output_len=len(result.raw_output or ""),
        )
        return result

    # ------------------------------------------------------------------
    # Director LLM call
    # ------------------------------------------------------------------

    async def _call_director_llm(
        self,
        run_id: str,
        world_model: WorldModel,
        global_config: dict,
    ) -> tuple[dict | None, str]:
        """Call the Director (strong model) with world model context + tool-use."""
        import httpx

        # Load model config
        director_provider = global_config.get("director_model_provider", "")
        director_model = global_config.get("director_model_id", "")

        from app.core.node_runner import _load_default_model_config, _load_model_config
        if not director_provider or not director_model:
            cfg = _load_default_model_config()
            director_provider = director_provider or str(cfg.get("provider", ""))
            director_model = director_model or str(cfg.get("model", ""))

        model_cfg = _load_model_config(director_provider, director_model)
        url = str(model_cfg.get("url", ""))
        api_key = str(model_cfg.get("key", ""))

        if not url or not api_key:
            logger.error("No API URL/key for Director LLM (provider=%s, model=%s)", director_provider, director_model)
            return None, f"未配置 API URL 或密钥 (provider={director_provider}, model={director_model})"

        system_content = DIRECTOR_SYSTEM.format(
            max_iterations=world_model.max_iterations,
            world_model=world_model.to_prompt_context(),
        )

        # Convert tools to the correct format based on API style
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for tool in DIRECTOR_TOOLS
        ]

        # Determine if this is an Anthropic-style or OpenAI-style API
        is_anthropic = "/anthropic" in url or director_provider == "anthropic"

        if is_anthropic:
            # Anthropic format
            endpoint = f"{url}/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": director_model,
                "system": system_content,
                "messages": [
                    {"role": "user", "content": "Decide the next action based on the current world model."},
                ],
                "tools": DIRECTOR_TOOLS,
                "tool_choice": {"type": "tool", "name": "decide"},
                "max_tokens": 2048,
            }
        else:
            # OpenAI format
            endpoint = f"{url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            payload = {
                "model": director_model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": "Decide the next action based on the current world model."},
                ],
                "tools": openai_tools,
                "tool_choice": DIRECTOR_TOOL_CHOICE,
                "max_tokens": 2048,
            }

        try:
            _dbg.log_llm_call(
                __name__, provider=director_provider, model=director_model,
                prompt_preview=f"[Director] iteration={world_model.iteration}",
            )
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            elapsed_ms = (time.monotonic() - t0) * 1000
            _dbg.log_llm_call(
                __name__, provider=director_provider, model=director_model,
                duration_ms=elapsed_ms,
                response_preview=str(data)[:500],
            )

            # Extract tool call — handle both API formats
            if is_anthropic:
                result = self._parse_anthropic_tool_response(data)
            else:
                result = self._parse_openai_tool_response(data)
            if result is None:
                return None, f"LLM 未返回有效决策 (model={director_model}, response={str(data)[:300]})"
            return result, ""

        except Exception as exc:
            logger.exception("Director LLM call failed for run %s", run_id)
            return None, f"Director LLM 调用失败: {str(exc)[:300]}"

    def _parse_anthropic_tool_response(self, data: dict) -> dict | None:
        """Parse tool call from Anthropic-format response."""
        content_blocks = data.get("content") or []
        for block in content_blocks:
            if block.get("type") == "tool_use":
                try:
                    return block.get("input", {})
                except Exception:
                    pass
        # No tool_use block — try free-text from text blocks
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        full_text = "".join(text_parts)
        decision = _parse_free_text_decision(full_text)
        if decision:
            logger.info("Director free-text decision parsed (anthropic): action=%s", decision.get("action"))
            return decision
        logger.warning("Director returned no tool_use block: %s", full_text[:200])
        return None

    def _parse_openai_tool_response(self, data: dict) -> dict | None:
        """Parse tool call from OpenAI-format response."""
        choices = data.get("choices") or []
        if not choices:
            return None

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = message.get("content", "") or ""
            decision = _parse_free_text_decision(content)
            if decision:
                logger.info("Director free-text decision parsed (openai): action=%s", decision.get("action"))
                return decision
            logger.warning("Director returned free text instead of tool call: %s", content[:200])
            return None

        tool_call = tool_calls[0]
        function_args = tool_call.get("function", {}).get("arguments", "{}")
        try:
            return json.loads(function_args)
        except json.JSONDecodeError:
            logger.warning("Failed to parse Director tool call arguments: %s", function_args[:200])
            return None

    # ------------------------------------------------------------------
    # World model updates
    # ------------------------------------------------------------------

    def _update_world_from_result(
        self, world: WorldModel, task_id: str, action: str, result: NodeResult,
    ) -> None:
        """Parse sub-agent result and update the world model."""
        if result.state != "completed":
            world.record_failure(task_id, action, result.error or "Sub-agent failed",
                                 prompt_hint=result.result_summary[:150] if result.result_summary else "")
            return

        if not result.raw_output:
            world.record_success(task_id, action, "(no output)")
            return

        llm_text = _extract_llm_text(result.raw_output)
        files_changed: list[str] = []

        # Try structured block first
        block_name = "SCOUT_FINDINGS" if action == "scout" else "WORKER_RESULT"
        structured = _extract_structured_block(llm_text, block_name)

        if structured:
            summary = structured.get("summary", "")
            files_changed = structured.get("files_changed", structured.get("files_found", []))
            if not summary:
                summary = f"{action} completed"
            world.record_success(task_id, action, summary, files_changed)
        else:
            # Fallback: use result_summary
            summary = result.result_summary[:300] if result.result_summary else f"{action} completed (no structured output)"
            world.record_success(task_id, action, summary)

    def _build_sub_prompt(
        self, prompt: str, target_files: list[str], world: WorldModel, action: str,
    ) -> str:
        """Build the prompt for a sub-agent, including context."""
        parts = [prompt]

        if target_files:
            parts.append(f"\n\n## Target Files\n" + "\n".join(f"- {f}" for f in target_files))

        # Add recent history for context
        if world.completed_tasks:
            recent = world.completed_tasks[-5:]
            history_lines = []
            for t in recent:
                icon = "+" if t.success else "-"
                history_lines.append(f"  [{icon}] {t.task_id} ({t.action}): {t.summary[:100]}")
            parts.append("\n\n## Recent Steps\n" + "\n".join(history_lines))

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _git_diff_stat(self, sandbox_id: str) -> str:
        """Get a compact git diff --stat for the world model.

        Uses subprocess directly — no shell invocation.
        """
        try:
            git_dir = self._sandbox.resolve_virtual_path(sandbox_id, "/sandbox-meta/.git")
            work_tree = self._sandbox.resolve_virtual_path(sandbox_id, "/workspace")

            def _run():
                return subprocess.run(
                    ["git", f"--git-dir={git_dir}", f"--work-tree={work_tree}",
                     "diff", "--stat", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=work_tree,
                )

            result = await asyncio.to_thread(_run)
            stdout = result.stdout
            if len(stdout) > 1500:
                stdout = stdout[:1500] + "..."
            return stdout.strip()
        except Exception:
            return ""

    async def _finish_run(self, run_id: str, status: str, message: str = "") -> None:
        """Update run status in memory and DB."""
        run_state = self._runs.get(run_id)
        if run_state:
            run_state["status"] = status

        await self._update_run_status_db(run_id, status)

        event_type = "run_completed" if status == "completed" else "run_failed"
        await self._emit(event_type, run_id, "director", content=message)
        logger.info("Director run %s finished: %s (%s)", run_id, status, message)

    async def _update_run_status_db(self, run_id: str, status: str) -> None:
        try:
            from sqlalchemy import select
            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel, Workflow as WorkflowModel

            async with async_session_factory() as session:
                result = await session.execute(
                    select(RunModel).where(RunModel.id == uuid.UUID(run_id))
                )
                run_row = result.scalar_one_or_none()
                if run_row is not None:
                    run_row.status = status
                    if status in ("completed", "failed", "cancelled"):
                        run_row.completed_at = datetime.now(timezone.utc)

                    wf_result = await session.execute(
                        select(WorkflowModel).where(WorkflowModel.id == run_row.workflow_id)
                    )
                    wf = wf_result.scalar_one_or_none()
                    if wf is not None:
                        if status == "running":
                            wf.lifecycle_phase = "running"
                            wf.blockers_json = []
                        elif status in ("completed", "failed", "cancelled"):
                            wf.lifecycle_phase = "review"
                    await session.commit()
        except Exception:
            logger.warning("Failed to update run status in DB for %s", run_id, exc_info=True)

    async def _emit(self, event_type: str, run_id: str, node_id: str, **extra: Any) -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "run_id": run_id,
            "node_id": node_id,
            "timestamp": time.time(),
            **extra,
        }
        await self._persist_event(event)
        channel = f"run:{run_id}:stream"
        try:
            await self._event_bus.publish(channel, event)
        except Exception:
            logger.warning("Failed to publish event %s", event_type, exc_info=True)

    async def _persist_event(self, event: dict[str, Any]) -> None:
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run, RunEvent

            run_id = uuid.UUID(str(event.get("run_id", "")))
            node_id = str(event.get("node_id") or "")
            async with async_session_factory() as session:
                run = await session.get(Run, run_id)
                if run is None:
                    return
                session.add(RunEvent(
                    run_id=run_id,
                    event_type=str(event.get("type") or ""),
                    node_id=node_id,
                    payload=event,
                ))
                await session.commit()
        except Exception:
            logger.warning("Failed to persist event %s", event.get("type"), exc_info=True)
