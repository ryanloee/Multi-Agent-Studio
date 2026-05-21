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
import re
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.core import debug_logger as _dbg
from app.core.director_prompts import (
    DIRECTOR_REVIEW_SYSTEM,
    DIRECTOR_SYSTEM,
    SCOUT_SYSTEM,
    TESTER_SYSTEM,
    WORKER_SYSTEM,
)
from app.core.director_tools import DIRECTOR_TOOLS
from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.node_runner import NodeResult, NodeRunner
from app.core.world_model import WorldModel
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)

MAX_REVIEW_RETRIES = 10


def _try_parse_json_dict(text: str) -> dict | None:
    """Parse JSON text, returning a dict or None if the result is not a dict."""
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_structured_block(text: str, block_name: str) -> dict | None:
    """Extract a JSON block delimited by ===BLOCK_NAME=== ... ===END_BLOCK_NAME===."""
    start_marker = f"==={block_name}==="
    end_marker = f"===END_{block_name}==="
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return None

    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        json_text = text[start_idx + len(start_marker):]
    else:
        json_text = text[start_idx + len(start_marker):end_idx]
    json_text = json_text.strip()
    if not json_text:
        return None
    result = _try_parse_json_dict(json_text)
    if result is not None:
        return result
    # Fallback: try to extract the first { ... } substring
    brace_start = json_text.find("{")
    brace_end = json_text.rfind("}")
    if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
        return None
    return _try_parse_json_dict(json_text[brace_start:brace_end + 1])


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
    if any(
        kw in text_lower
        for kw in ("任务完成", "目标达成", "all done", "goal achieved", "finished")
    ):
        return {"action": "done", "reasoning": text[:200], "prompt": "", "task_id": "auto-done"}
    if any(kw in text_lower for kw in ("失败", "无法", "blocked", "stuck", "cannot proceed")):
        return {"action": "failed", "reasoning": text[:200], "prompt": "", "task_id": "auto-failed"}

    # Default: dispatch a worker with the full text as prompt
    return {
        "action": "worker",
        "reasoning": "Director free-text fallback",
        "prompt": text[:1000],
        "task_id": "fallback",
    }


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
        if not isinstance(ev, dict):
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
        resume_from: dict | None = None,
        sandbox_existed: bool = True,
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
                resume_from=resume_from,
                sandbox_existed=sandbox_existed,
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
        """Detect interrupted runs and resume from checkpoint if possible."""
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
                runs = result.scalars().all()
                if not runs:
                    return

                for run in runs:
                    checkpoint = run.checkpoint_json
                    if not checkpoint or not checkpoint.get("sandbox_id"):
                        run.status = "failed"
                        logger.info("Marking interrupted run %s as failed (no checkpoint)", run.id)
                        continue

                    sandbox_dir = Path(settings.sandbox_root) / checkpoint["sandbox_id"]
                    if not sandbox_dir.exists():
                        run.status = "failed"
                        logger.info(
                            "Marking interrupted run %s as failed (sandbox missing)",
                            run.id,
                        )
                        continue

                    run.status = "running"
                    await session.commit()
                    logger.info(
                        "Resuming interrupted run %s from iteration %d",
                        run.id, checkpoint.get("checkpoint_iteration", 0),
                    )
                    await self.start_run(
                        run_id=str(run.id),
                        dag_json=checkpoint.get("dag_json", {}),
                        global_config=checkpoint.get("global_config", {}),
                        workspace_directory=checkpoint.get("workspace_directory"),
                        resume_from=checkpoint,
                    )

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
        resume_from: dict | None = None,
        sandbox_existed: bool = True,
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

        _dbg.debug(
            __name__,
            "DAG node map built",
            dag_node_map={k: list(v) for k, v in dag_node_map.items()},
        )

        # Create shared sandbox for trunk-based development
        sandbox_id: str | None = None
        world: WorldModel | None = None
        last_active_node_id: str | None = None

        if resume_from:
            world = WorldModel.from_json(resume_from["world_model_json"])
            old_sandbox_id = resume_from["sandbox_id"]
            dag_json = resume_from.get("dag_json", dag_json)
            global_config = resume_from.get("global_config", global_config)
            workspace_directory = resume_from.get("workspace_directory", workspace_directory)

            if sandbox_existed:
                # Try to re-register existing sandbox
                try:
                    await self._sandbox.re_register(old_sandbox_id, workspace_directory)
                    sandbox_id = old_sandbox_id
                    logger.info(
                        "Re-registered sandbox %s for resumed run %s at iteration %d",
                        sandbox_id[:12], run_id, world.iteration,
                    )
                except Exception:
                    logger.warning(
                        "Failed to re-register sandbox %s, will recreate", old_sandbox_id[:12]
                    )
                    sandbox_existed = False

            if not sandbox_existed:
                # Sandbox was destroyed, recreate from workspace
                try:
                    sandbox_id = await self._sandbox.create(
                        f"ws-director-{run_id[:8]}",
                        template_dir=workspace_directory,
                        user_workspace=workspace_directory,
                    )
                    logger.info(
                        "Recreated sandbox %s for resumed run %s (old sandbox was destroyed)",
                        sandbox_id[:12], run_id,
                    )
                except Exception:
                    logger.exception("Failed to recreate sandbox for resumed run %s", run_id)
                    await self._finish_run(run_id, "failed", "Failed to recreate sandbox")
                    return

            await self._emit(
                "run_resumed", run_id, "director",
                iteration=world.iteration,
                checkpoint_iteration=resume_from.get("checkpoint_iteration", 0),
                sandbox_recreated=not sandbox_existed,
            )

            # Emit node_retried for the last failed node so frontend resets its status
            failed_attempts = world.failed_attempts or []
            if failed_attempts:
                last_failed = failed_attempts[-1]
                action_map = {"scout": "explore", "worker": "coder", "test": "shell"}
                agent_type = action_map.get(last_failed.action, "coder")
                candidates = dag_node_map.get(agent_type, [])
                retry_node_id = candidates[0] if candidates else last_failed.task_id
                await self._emit("node_retried", run_id, retry_node_id)
                last_active_node_id = retry_node_id
                logger.info(
                    "Resuming from failed node %s (was %s)",
                    retry_node_id,
                    last_failed.task_id,
                )

            logger.info("Resumed run %s from iteration %d", run_id, world.iteration)
        else:
            try:
                sandbox_id = await self._sandbox.create(
                    f"ws-director-{run_id[:8]}",
                    template_dir=workspace_directory,
                    user_workspace=workspace_directory,
                )
                logger.info(
                    "Created shared sandbox %s for director run %s", sandbox_id[:12], run_id
                )
            except Exception:
                logger.exception("Failed to create sandbox for director run %s", run_id)
                await self._finish_run(run_id, "failed", "Failed to create sandbox")
                return

            world = WorldModel(goal=goal)
            world.iteration = 0
            # Removed fixed iteration limit; now relies on time-based limits

        if world is None or sandbox_id is None:
            logger.error("Failed to initialize world/sandbox for run %s", run_id)
            await self._finish_run(run_id, "failed", "Internal error: state initialization failed")
            return

        clean_finish = False

        try:
            if not resume_from:
                await self._emit("director_decision", run_id, "director",
                                 action="scout", reasoning="Initial project reconnaissance",
                                 task_id="init-scout", iteration=0)

                scout_result = await self._run_sub_agent(
                    run_id=run_id,
                    agent_type="explore",
                    system_prompt=SCOUT_SYSTEM,
                    prompt=(
                        "Investigate the project structure and report findings.\n\n"
                        f"Goal: {goal}"
                    ),
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
                    world.record_success(
                        "init-scout",
                        "scout",
                        "Initial project reconnaissance completed",
                    )
                else:
                    world.record_failure(
                        "init-scout", "scout", scout_result.error or "Scout failed"
                    )
                    # Check if this was due to user cancellation
                    if cancel_event.is_set():
                        clean_finish = True
                        await self._finish_run(run_id, "cancelled", "Cancelled by user")
                        return
                    # Initial scout failed (likely LLM connection error) — stop the run
                    error_msg = scout_result.error or "Scout failed"
                    logger.error(
                        "Initial scout failed for run %s: %s — stopping run",
                        run_id, error_msg[:200],
                    )
                    # Do NOT set clean_finish — preserve sandbox for resume
                    await self._finish_run(
                        run_id, "failed",
                        f"Initial scout failed: {error_msg}",
                    )
                    return

                # Update file snapshot
                world.current_file_snapshot = await self._git_diff_stat(sandbox_id)
                world.iteration = 1

                # Save checkpoint after initial scout
                await self._save_checkpoint(run_id, world, sandbox_id,
                                            global_config, workspace_directory, dag_json)

            # Step 2: Main dispatch loop
            consecutive_no_decision = 0
            last_decision_error = ""

            # Keep track of the last used node_id so the UI stays active while the Director plans.
            if not resume_from:
                last_active_node_id = "init-scout"

            # Time-based limit instead of iteration count
            start_time = time.monotonic()
            max_duration = global_config.get("max_duration_seconds", 7200)
            deadline = start_time + max_duration

            ui_fallback_node_id: str | None = None
            if dag_node_map:
                for cand_list in dag_node_map.values():
                    if cand_list:
                        ui_fallback_node_id = cand_list[0]
                        break

            while not cancel_event.is_set():
                # Check time limit
                if time.monotonic() > deadline:
                    logger.warning(
                        "Run %s exceeded max duration (%.1fs), stopping",
                        run_id,
                        max_duration,
                    )
                    await self._finish_run(
                        run_id, "failed",
                        (
                            f"Workflow time limit exceeded ({max_duration}s). "
                            "Please simplify the task or increase the limit."
                        ),
                    )
                    return

                # Pick a stable node_id so the UI stays active while the Director plans.
                if ui_fallback_node_id and not last_active_node_id:
                    last_active_node_id = ui_fallback_node_id

                if last_active_node_id:
                    # Emit an event to set the node status to running with a planning message
                    # This bridges the gap between sub-agent completions and prevents UI flicker.
                    await self._emit("node_started", run_id, last_active_node_id)
                    await self._emit(
                        "agent_status", run_id, last_active_node_id,
                        content="Director is planning next step..."
                    )

                # Call Director LLM
                decision, decision_error = await self._call_director_llm(
                    run_id=run_id,
                    world_model=world,
                    global_config=global_config,
                )

                if decision is None:
                    consecutive_no_decision += 1
                    last_decision_error = decision_error
                    logger.warning(
                        "Director LLM returned no decision for run %s (consecutive=%d, error=%s)",
                        run_id,
                        consecutive_no_decision,
                        decision_error[:200],
                    )
                    if consecutive_no_decision >= 3:
                        await self._finish_run(
                            run_id,
                            "failed",
                            (
                                "Director LLM 连续 3 次未返回有效决策\n"
                                f"{last_decision_error}"
                            ),
                        )
                        return
                    world.iteration += 1
                    continue
                consecutive_no_decision = 0

                if not isinstance(decision, dict):
                    logger.warning(
                        "Director returned non-dict decision: %s",
                        type(decision).__name__,
                    )
                    consecutive_no_decision += 1
                    world.iteration += 1
                    continue

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
                    clean_finish = True
                    await self._finish_run(run_id, "completed", reasoning)
                    return

                if action == "failed":
                    world.record_failure(task_id, "failed", reasoning)
                    # Do NOT set clean_finish — preserve sandbox for resume
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
                    enable_self_review=(action == "worker"),
                )

                # Parse result
                self._update_world_from_result(world, task_id, action, sub_result)

                # If sub-agent failed, stop the entire run immediately.
                if sub_result.state != "completed":
                    # Check if this was due to user cancellation
                    if cancel_event.is_set():
                        clean_finish = True
                        await self._finish_run(run_id, "cancelled", "Cancelled by user")
                        return
                    error_msg = sub_result.error or "Sub-agent failed"
                    logger.error(
                        "Sub-agent failed for run %s, task %s: %s — stopping run",
                        run_id, task_id, error_msg[:200],
                    )
                    # Do NOT set clean_finish — preserve sandbox for resume
                    await self._finish_run(
                        run_id, "failed",
                        f"Node '{task_id}' ({action}) failed: {error_msg}",
                    )
                    return

                # Worker review loop: self-review already done in AgentRunner,
                # now Director reviews the output before committing.
                if action == "worker" and sub_result.state == "completed":
                    review_passed = False
                    reject_reason = ""
                    summary = self._extract_worker_summary(sub_result)

                    await self._emit("worker_summary", run_id, "director",
                                     task_id=task_id, content=summary[:2000])
                    await self._persist_chat_message(
                        run_id, task_id, "assistant",
                        f"Worker '{task_id}' completed:\n{summary[:1500]}",
                    )

                    for review_attempt in range(1, MAX_REVIEW_RETRIES + 1):
                        if cancel_event.is_set():
                            break

                        await self._emit("review_started", run_id, "director",
                                         task_id=task_id, attempt=review_attempt)

                        review = await self._review_worker_output(
                            run_id=run_id,
                            world_model=world,
                            summary=summary,
                            task_id=task_id,
                            goal=reasoning,
                            review_attempt=review_attempt,
                            global_config=global_config,
                        )

                        if review and review.get("result") == "pass":
                            review_passed = True
                            review_reason = review.get("reason", "Approved")
                            world.record_review(task_id, True, review_reason, review_attempt)
                            await self._emit("review_result", run_id, "director",
                                             task_id=task_id, result="pass",
                                             reason=review_reason, attempt=review_attempt)
                            await self._persist_chat_message(
                                run_id, task_id, "assistant",
                                f"Review PASSED (attempt {review_attempt}): {review_reason}",
                            )
                            break
                        else:
                            reject_reason = (review or {}).get("reason", "未通过审核")
                            next_prompt = (review or {}).get("next_prompt", "")
                            world.record_review(
                                task_id,
                                False,
                                reject_reason,
                                review_attempt,
                                next_prompt,
                            )
                            await self._emit("review_result", run_id, "director",
                                             task_id=task_id, result="reject",
                                             reason=reject_reason, attempt=review_attempt)
                            await self._persist_chat_message(
                                run_id, task_id, "assistant",
                                f"Review REJECTED (attempt {review_attempt}): {reject_reason}\n"
                                f"Guidance: {next_prompt}",
                            )

                            retry_prompt = (
                                f"之前的实现被审核不通过，原因：{reject_reason}\n"
                                f"请在现有代码基础上修正。\n"
                            )
                            if next_prompt:
                                retry_prompt += f"\n修改指导：{next_prompt}\n"
                            retry_prompt += f"\n原始任务：{prompt}"

                            await self._emit("review_retry", run_id, "director",
                                             task_id=task_id, attempt=review_attempt,
                                             max_attempts=MAX_REVIEW_RETRIES)
                            redispatch_msg = (
                                f"Re-dispatching worker '{task_id}' "
                                f"(attempt {review_attempt + 1}/{MAX_REVIEW_RETRIES})"
                            )
                            await self._persist_chat_message(
                                run_id,
                                task_id,
                                "system",
                                redispatch_msg,
                            )

                            sub_result = await self._run_sub_agent(
                                run_id=run_id,
                                agent_type="coder",
                                system_prompt=WORKER_SYSTEM,
                                prompt=self._build_sub_prompt(
                                    retry_prompt, target_files, world, "worker"
                                ),
                                sandbox_id=sandbox_id,
                                cancel_event=cancel_event,
                                model_provider=global_config.get("worker_model_provider", ""),
                                model_id=global_config.get("worker_model_id", ""),
                                dag_node_map=dag_node_map,
                                workspace_directory=workspace_directory,
                                world=world,
                                enable_self_review=True,
                            )

                            self._update_world_from_result(world, task_id, "worker", sub_result)

                            if sub_result.state != "completed":
                                break

                            summary = self._extract_worker_summary(sub_result)
                            await self._emit("worker_summary", run_id, "director",
                                             task_id=task_id, content=summary[:2000])
                            await self._persist_chat_message(
                                run_id, task_id, "assistant",
                                f"Worker '{task_id}' revised:\n{summary[:1500]}",
                            )

                    if not review_passed and sub_result.state == "completed":
                        # Check if this was due to user cancellation
                        if cancel_event.is_set():
                            clean_finish = True
                            await self._finish_run(run_id, "cancelled", "Cancelled by user")
                            return
                        # Do NOT set clean_finish — preserve sandbox for resume
                        await self._finish_run(
                            run_id, "failed",
                            (
                                f"Task '{task_id}' failed review {MAX_REVIEW_RETRIES} times: "
                                f"{reject_reason}"
                            ),
                        )
                        return

                    if not review_passed:
                        # Check if this was due to user cancellation
                        if cancel_event.is_set():
                            clean_finish = True
                            await self._finish_run(run_id, "cancelled", "Cancelled by user")
                            return
                        # Do NOT set clean_finish — preserve sandbox for resume
                        await self._finish_run(
                            run_id, "failed",
                            (
                                f"Task '{task_id}' worker failed: "
                                f"{sub_result.error or 'unknown error'}"
                            ),
                        )
                        return

                    try:
                        await self._checkpoint.auto_commit(
                            sandbox_id,
                            message=f"director: {task_id} — {reasoning[:80]} (reviewed)",
                        )
                    except Exception:
                        pass

                # Non-worker actions: commit immediately
                elif action in ("scout", "test") and sub_result.state == "completed":
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

                # Save checkpoint after each iteration
                await self._save_checkpoint(run_id, world, sandbox_id,
                                            global_config, workspace_directory, dag_json)

            # Loop ended (should only happen via cancellation or time limit)
            if cancel_event.is_set():
                clean_finish = True
                await self._finish_run(run_id, "cancelled", "Cancelled by user")

        except asyncio.CancelledError:
            clean_finish = True
            await self._finish_run(run_id, "cancelled", "Cancelled")
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error("Director loop crashed for run %s:\n%s", run_id, tb)
            await self._finish_run(run_id, "failed", f"Director loop error: {exc}\n{tb[:500]}")
        finally:
            if sandbox_id:
                if workspace_directory:
                    try:
                        await self._sandbox.sync_back(sandbox_id, workspace_directory)
                    except Exception:
                        logger.warning(
                            "Final sync_back failed for director run %s",
                            run_id,
                            exc_info=True,
                        )
                if clean_finish:
                    try:
                        await self._sandbox.destroy(sandbox_id)
                    except Exception:
                        pass
                else:
                    logger.info("Preserving sandbox %s for potential resume", sandbox_id[:12])

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
        enable_self_review: bool = False,
    ) -> NodeResult:
        """Dispatch a sub-agent via NodeRunner on the shared sandbox."""
        # Prefer DAG node IDs so canvas status animations work
        node_id = ""
        if dag_node_map:
            # Prefer exact agent_type match.
            candidates = dag_node_map.get(agent_type, [])
            if candidates:
                node_id = candidates.pop(0)

            # Fallback: borrow the first available node ID (any type) so something animates.
            if not node_id:
                for cand_list in dag_node_map.values():
                    if cand_list:
                        node_id = cand_list[0]  # peek; do not pop
                        break

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
            enable_self_review=enable_self_review,
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
        force_tool: str = "decide",
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
            logger.error(
                "No API URL/key for Director LLM (provider=%s, model=%s)",
                director_provider,
                director_model,
            )
            return None, (
                "未配置 API URL 或密钥 "
                f"(provider={director_provider}, model={director_model})"
            )

        system_content = DIRECTOR_SYSTEM.format(
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
                    {
                        "role": "user",
                        "content": "Decide the next action based on the current world model.",
                    },
                ],
                "tools": DIRECTOR_TOOLS,
                "tool_choice": {"type": "tool", "name": force_tool},
                "max_tokens": 2048,
            }
        else:
            endpoint = f"{url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            tool_choice_obj = {"type": "function", "function": {"name": force_tool}}
            payload = {
                "model": director_model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {
                        "role": "user",
                        "content": "Decide the next action based on the current world model.",
                    },
                ],
                "tools": openai_tools,
                "tool_choice": tool_choice_obj,
                "max_tokens": 2048,
            }

        try:
            _dbg.log_llm_call(
                __name__, provider=director_provider, model=director_model,
                prompt_preview=f"[Director] iteration={world_model.iteration}",
            )
            t0 = time.monotonic()

            # Retry transient connection/timeout errors with exponential backoff
            max_retries = 3
            last_exc: Exception | None = None
            data: dict | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    timeout = httpx.Timeout(connect=10, read=120, write=10, pool=10)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                    last_exc = None
                    break
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = 2.0 * (2 ** (attempt - 1))
                        logger.warning(
                            "Director LLM transient error (attempt %d/%d), retrying in %.1fs: %s",
                            attempt, max_retries, delay, exc,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "Director LLM transient error persisted after %d attempts: %s",
                            max_retries, exc,
                        )

            if data is None:
                raise last_exc or Exception("Director LLM call returned no data")

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
                return None, (
                    "LLM 未返回有效决策 "
                    f"(model={director_model}, response={str(data)[:300]})"
                )
            return result, ""

        except Exception as exc:
            logger.exception("Director LLM call failed for run %s", run_id)
            return None, f"Director LLM 调用失败: {str(exc)[:300]}"

    def _parse_anthropic_tool_response(self, data: dict) -> dict | None:
        """Parse tool call from Anthropic-format response."""
        content_blocks = data.get("content") or []
        if isinstance(content_blocks, str):
            decision = _parse_free_text_decision(content_blocks)
            if decision:
                logger.info(
                    "Director free-text decision parsed (anthropic string content): action=%s",
                    decision.get("action"),
                )
                return decision
            return None
        text_parts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inp = block.get("input", {})
                if isinstance(inp, str):
                    inp = _try_parse_json_dict(inp) or {}
                return inp if isinstance(inp, dict) and inp.get("action") else None
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        # No tool_use block — try free-text from text blocks
        full_text = "".join(text_parts)
        decision = _parse_free_text_decision(full_text)
        if decision:
            logger.info(
                "Director free-text decision parsed (anthropic): action=%s",
                decision.get("action"),
            )
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
                logger.info(
                    "Director free-text decision parsed (openai): action=%s",
                    decision.get("action"),
                )
                return decision
            logger.warning("Director returned free text instead of tool call: %s", content[:200])
            return None

        tool_call = tool_calls[0]
        function_args = tool_call.get("function", {}).get("arguments", "{}")
        result = _try_parse_json_dict(function_args)
        if result is None:
            logger.warning("Failed to parse Director tool call arguments: %s", function_args[:200])
        return result

    # ------------------------------------------------------------------
    # Worker review
    # ------------------------------------------------------------------

    def _extract_worker_summary(self, result: NodeResult) -> str:
        if not result.raw_output:
            return result.result_summary or "(no output)"
        llm_text = _extract_llm_text(result.raw_output)
        structured = _extract_structured_block(llm_text, "WORKER_RESULT")
        if structured:
            summary = structured.get("summary", "")
            files = structured.get("files_changed", [])
            parts = [f"Summary: {summary}"]
            if files:
                parts.append(f"Files changed: {', '.join(str(f) for f in files[:10])}")
            return "\n".join(parts)
        return result.result_summary[:2000] if result.result_summary else llm_text[:2000]

    async def _review_worker_output(
        self,
        run_id: str,
        world_model: WorldModel,
        summary: str,
        task_id: str,
        goal: str,
        review_attempt: int,
        global_config: dict,
    ) -> dict | None:
        """Call Director LLM to review worker output, using the review tool."""
        import httpx

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
            logger.error("No API URL/key for Director review LLM")
            return None

        review_history = ""
        for r in world_model.reviews:
            if r.task_id == task_id:
                status = "PASS" if r.passed else "REJECT"
                review_history += f"\n  Attempt {r.attempt}: {status} — {r.reason[:150]}"

        system_content = DIRECTOR_REVIEW_SYSTEM
        user_content = (
            f"## Task\nTask ID: {task_id}\nGoal: {goal}\n"
            f"Review attempt: {review_attempt}/{MAX_REVIEW_RETRIES}\n"
        )
        if review_history:
            user_content += f"\n## Previous Reviews for this task:{review_history}\n"
        user_content += f"\n## Worker Output Summary\n{summary[:2000]}\n\nReview this output."

        is_anthropic = "/anthropic" in url or director_provider == "anthropic"
        review_tool = [t for t in DIRECTOR_TOOLS if t["name"] == "review"][0]
        openai_tools = [{
            "type": "function",
            "function": {
                "name": review_tool["name"],
                "description": review_tool.get("description", ""),
                "parameters": review_tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }]

        if is_anthropic:
            endpoint = f"{url}/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": director_model,
                "system": system_content,
                "messages": [{"role": "user", "content": user_content}],
                "tools": [review_tool],
                "tool_choice": {"type": "tool", "name": "review"},
                "max_tokens": 1024,
            }
        else:
            endpoint = f"{url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            payload = {
                "model": director_model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                "tools": openai_tools,
                "tool_choice": {"type": "function", "function": {"name": "review"}},
                "max_tokens": 1024,
            }

        try:
            t0 = time.monotonic()
            timeout = httpx.Timeout(connect=10, read=60, write=10, pool=10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            elapsed_ms = (time.monotonic() - t0) * 1000
            _dbg.log_llm_call(
                __name__, provider=director_provider, model=director_model,
                duration_ms=elapsed_ms,
                prompt_preview=f"[Review] task={task_id} attempt={review_attempt}",
            )

            if is_anthropic:
                content_blocks = data.get("content") or []
                for block in content_blocks:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "review"
                    ):
                        inp = block.get("input", {})
                        if isinstance(inp, str):
                            inp = _try_parse_json_dict(inp) or {}
                        return inp if isinstance(inp, dict) else None
            else:
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message", {})
                    tool_calls = message.get("tool_calls") or []
                    for tc in tool_calls:
                        if tc.get("function", {}).get("name") == "review":
                            args = tc["function"].get("arguments", "{}")
                            return _try_parse_json_dict(args)
            return None
        except Exception as exc:
            logger.warning("Director review LLM call failed: %s", exc)
            return None

    async def _persist_chat_message(
        self, run_id: str, node_id: str, role: str, content: str,
    ) -> None:
        """Persist a review-related message to the ChatMessage table."""
        try:
            from app.core.database import async_session_factory
            from app.models.db import ChatMessage as ChatMessageORM
            from app.models.db import Run as RunModel

            async with async_session_factory() as session:
                run_row = await session.get(RunModel, uuid.UUID(run_id))
                if not run_row:
                    return
                workflow_id = run_row.workflow_id
                session.add(ChatMessageORM(
                    id=uuid.uuid4(),
                    workflow_id=workflow_id,
                    node_id=node_id,
                    role=role,
                    content=content,
                ))
                await session.commit()
        except Exception:
            logger.debug("Failed to persist chat message for review", exc_info=True)

    # ------------------------------------------------------------------
    # World model updates
    # ------------------------------------------------------------------

    def _update_world_from_result(
        self, world: WorldModel, task_id: str, action: str, result: NodeResult,
    ) -> None:
        """Parse sub-agent result and update the world model."""
        if result.state != "completed":
            prompt_hint = result.result_summary[:150] if result.result_summary else ""
            world.record_failure(
                task_id,
                action,
                result.error or "Sub-agent failed",
                prompt_hint=prompt_hint,
            )
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
            if result.result_summary:
                summary = result.result_summary[:300]
            else:
                summary = f"{action} completed (no structured output)"
            world.record_success(task_id, action, summary)

    def _build_sub_prompt(
        self, prompt: str, target_files: list[str], world: WorldModel, action: str,
    ) -> str:
        """Build the prompt for a sub-agent, including context."""
        parts = [prompt]

        if target_files:
            parts.append("\n\n## Target Files\n" + "\n".join(f"- {f}" for f in target_files))

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

    async def _save_checkpoint(
        self,
        run_id: str,
        world: WorldModel,
        sandbox_id: str,
        global_config: dict,
        workspace_directory: str | None,
        dag_json: dict,
    ) -> None:
        """Persist current world model state to the runs table for crash recovery."""
        try:
            from sqlalchemy import update

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel

            checkpoint_data = {
                "world_model_json": world.to_json(),
                "sandbox_id": sandbox_id,
                "global_config": global_config,
                "workspace_directory": workspace_directory or "",
                "dag_json": dag_json,
                "checkpoint_iteration": world.iteration,
            }
            async with async_session_factory() as session:
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == uuid.UUID(run_id))
                    .values(checkpoint_json=checkpoint_data)
                )
                await session.commit()
        except Exception:
            logger.warning("Failed to save checkpoint for run %s", run_id, exc_info=True)

    async def _finish_run(self, run_id: str, status: str, message: str = "") -> None:
        """Update run status in memory and DB."""
        run_state = self._runs.get(run_id)
        if run_state:
            run_state["status"] = status

        await self._update_run_status_db(run_id, status)

        event_type = {
            "completed": "run_completed",
            "failed": "run_failed",
            "cancelled": "run_cancelled",
        }.get(status, "run_failed")
        await self._emit(event_type, run_id, "director", content=message)
        logger.info("Director run %s finished: %s (%s)", run_id, status, message)

    async def _update_run_status_db(self, run_id: str, status: str) -> None:
        try:
            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from app.models.db import Workflow as WorkflowModel

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
