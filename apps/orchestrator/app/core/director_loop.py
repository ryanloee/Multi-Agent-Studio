"""DirectorLoop — Planner-driven dynamic execution engine.

The Planner (not Director) makes all decisions. It:
  1. Reads the world model and DAG node pool
  2. Calls Planner LLM with decide/review tools to choose next action
  3. Dispatches sub-agents (explore/coder/shell) via NodeRunner
  4. Can dispatch multiple sub-agents in parallel for speed
  5. Reviews coder output with full context
  6. Loops until Planner says done or time limit hit
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    MERGER_SYSTEM,
    PLANNER_DIRECTOR_SYSTEM,
    PLANNER_REVIEW_SYSTEM,
    SCOUT_SYSTEM,
    TESTER_SYSTEM,
    WORKER_SYSTEM,
)
from app.core.director_tools import (
    DECIDE_TOOL_NAME,
    MAX_NO_DECISION,
    MAX_REVIEW_RETRIES,
    PLANNER_TOOLS,
    PLANNER_TOOLS_OPENAI,
    REVIEW_TOOL,
    REVIEW_TOOL_NAME,
    REVIEW_TOOL_OPENAI,
)
from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.node_runner import NodeResult, NodeRunner
from app.core.world_model import WorldModel
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)

ACTION_TO_AGENT_TYPE = {
    "explore": "explore",
    "scout": "explore",
    "coder": "coder",
    "worker": "coder",
    "shell": "shell",
    "test": "shell",
    "merge": "merge",
    "merger": "merge",
}

ACTION_TO_SYSTEM_PROMPT = {
    "explore": SCOUT_SYSTEM,
    "coder": WORKER_SYSTEM,
    "shell": TESTER_SYSTEM,
    "merge": MERGER_SYSTEM,
}

REVIEWABLE_ACTIONS = {"coder"}

CONTEXT_MAX_CHARS = 80000
CONTEXT_KEEP_RECENT = 10


def _try_parse_json_dict(text: str) -> dict | None:
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_structured_block(text: str, block_name: str) -> dict | None:
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
    brace_start = json_text.find("{")
    brace_end = json_text.rfind("}")
    if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
        return None
    return _try_parse_json_dict(json_text[brace_start:brace_end + 1])


def _extract_llm_text(jsonl_content: str) -> str:
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
        if event_type in ("llm_token", "llm_chunk", "text"):
            parts.append(content)
        elif event_type == "tool_call":
            parts.append(f"\n[Tool Call: {ev.get('tool_name', 'tool')}]\n{content}\n")
        elif event_type == "tool_result":
            parts.append(f"\n[Tool Result: {ev.get('tool_name', 'tool')}]\n{content}\n")
        elif event_type in ("shell_stdout", "shell_stderr"):
            parts.append(f"\n[Shell Output]\n{content}\n")
    if not parsed_any:
        return jsonl_content
    return "".join(parts)


def _build_node_pool(dag_json: dict) -> dict[str, list[dict]]:
    """Build a pool of available DAG nodes grouped by agent type."""
    nodes = dag_json.get("nodes", [])
    pool: dict[str, list[dict]] = {}
    for n in nodes:
        data = n.get("data", {})
        at = (
            data.get("agentType", "") or data.get("agent_type", "")
            or n.get("type", "") or "coder"
        ).lower()
        pool.setdefault(at, []).append(n)
    return pool


def _pick_node_id(pool: dict[str, list[dict]], agent_type: str) -> str:
    """Pick the next available node ID from the pool for the given agent type."""
    candidates = pool.get(agent_type, [])
    if candidates:
        return candidates[0].get("id", f"{agent_type}-0")
    for cand_list in pool.values():
        if cand_list:
            return cand_list[0].get("id", "node-0")
    return f"{agent_type}-{uuid4().hex[:8]}"


def _format_available_nodes(pool: dict[str, list[dict]]) -> str:
    if not pool:
        return "No DAG nodes defined. You can dispatch any agent type freely."
    lines = []
    for atype, nodes in pool.items():
        ids = [n.get("id", "?") for n in nodes]
        lines.append(f"- {atype}: {', '.join(ids)}")
    return "\n".join(lines)


class DirectorLoop:
    """Planner-driven execution engine with dynamic decision loop."""

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
        self._runs: dict[str, dict] = {}

    async def start_run(
        self,
        run_id: str,
        dag_json: dict | None = None,
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
                run_id, dag_json or {}, global_config or {}, cancel_event,
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
        return run_id

    async def recover_interrupted_runs(self) -> None:
        try:
            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel

            async with async_session_factory() as session:
                result = await session.execute(
                    select(RunModel).where(RunModel.status.in_(("running", "pending", "cancelling")))
                )
                for run in result.scalars().all():
                    checkpoint = run.checkpoint_json
                    if not checkpoint or not checkpoint.get("sandbox_id"):
                        run.status = "failed"
                        continue
                    sandbox_dir = Path(settings.sandbox_root) / checkpoint["sandbox_id"]
                    if not sandbox_dir.exists():
                        run.status = "failed"
                        continue
                    run.status = "running"
                    await session.commit()
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
    # Main dispatch loop
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
        clean_finish = False

        try:
            goal = global_config.get("goal", "")
            node_pool = _build_node_pool(dag_json)
            available_nodes_text = _format_available_nodes(node_pool)
            planner_chat_history = await self._load_planner_history(run_id)

            sandbox_id: str | None = None
            world: WorldModel | None = None
            start_node_id = "init"

            if resume_from:
                world = WorldModel.from_json(resume_from["world_model_json"])
                old_sandbox_id = resume_from["sandbox_id"]
                dag_json = resume_from.get("dag_json", dag_json)
                global_config = resume_from.get("global_config", global_config)
                workspace_directory = resume_from.get("workspace_directory", workspace_directory)

                if sandbox_existed:
                    try:
                        await self._sandbox.re_register(old_sandbox_id, workspace_directory)
                        sandbox_id = old_sandbox_id
                    except Exception:
                        sandbox_existed = False

                if not sandbox_existed:
                    try:
                        sandbox_id = await self._sandbox.create(
                            f"ws-director-{run_id[:8]}",
                            template_dir=workspace_directory,
                            user_workspace=workspace_directory,
                        )
                    except Exception:
                        logger.exception("Failed to recreate sandbox for resumed run %s", run_id)
                        await self._finish_run(run_id, "failed", "Failed to recreate sandbox")
                        return

                node_pool = _build_node_pool(dag_json)
                available_nodes_text = _format_available_nodes(node_pool)

                await self._emit("run_resumed", run_id, "director", iteration=world.iteration)
                for nid, st in world.node_statuses.items():
                    if st == "failed":
                        await self._emit("node_retried", run_id, nid)

                start_node_id = world.last_node_id or "resumed"
                logger.info("Resumed run %s from iteration %d", run_id, world.iteration)
            else:
                sandbox_id = await self._sandbox.create(
                    f"ws-director-{run_id[:8]}",
                    template_dir=workspace_directory,
                    user_workspace=workspace_directory,
                )
                world = WorldModel(goal=goal)
                world.planner_review_messages = list(planner_chat_history)
                logger.info("Created sandbox %s for run %s", sandbox_id[:12], run_id)

            if world is None or sandbox_id is None:
                await self._finish_run(run_id, "failed", "State init failed")
                return

            start_time = time.monotonic()
            max_duration = global_config.get("max_duration_seconds", 7200)
            consecutive_no_decision = 0
            last_active_node_id = start_node_id

            while not cancel_event.is_set():
                if time.monotonic() - start_time > max_duration:
                    await self._finish_run(run_id, "failed", f"Time limit exceeded ({max_duration}s)")
                    return

                # Emit planning status for UI
                if last_active_node_id:
                    await self._emit("node_started", run_id, last_active_node_id)
                    await self._emit("agent_status", run_id, last_active_node_id,
                                     content="Planner is deciding next step...")

                # Call Planner LLM for decision
                decision = await self._call_planner_decide(
                    run_id, world, global_config, available_nodes_text,
                )

                if decision is None:
                    consecutive_no_decision += 1
                    if consecutive_no_decision >= MAX_NO_DECISION:
                        await self._finish_run(run_id, "failed", "Planner failed to decide 3 times")
                        return
                    world.iteration += 1
                    continue
                consecutive_no_decision = 0

                action = decision.get("action", "")
                prompt = decision.get("prompt", "")
                task_id = decision.get("task_id", f"step-{world.iteration}")
                reasoning = decision.get("reasoning", "")
                target_files = decision.get("target_files", [])

                await self._emit("planner_decision", run_id, "director",
                                 action=action, task_id=task_id, reasoning=reasoning)

                if action in ("done", "failed"):
                    clean_finish = action == "done"
                    status = "completed" if action == "done" else "failed"
                    await self._finish_run(run_id, status, reasoning or f"Planner: {action}")
                    return

                agent_type = ACTION_TO_AGENT_TYPE.get(action, action)
                system_prompt = ACTION_TO_SYSTEM_PROMPT.get(action)
                if system_prompt is None:
                    logger.warning("Unknown action '%s', skipping", action)
                    world.iteration += 1
                    continue

                node_id = _pick_node_id(node_pool, agent_type)
                last_active_node_id = node_id

                # Execute sub-agent
                await self._emit("node_started", run_id, node_id)
                await self._emit("agent_status", run_id, node_id,
                                 content=f"Executing {action}...")

                sub_result = await self._run_sub_agent(
                    run_id=run_id,
                    agent_type=agent_type,
                    system_prompt=system_prompt,
                    prompt=self._build_sub_prompt(prompt, target_files, world),
                    sandbox_id=sandbox_id,
                    cancel_event=cancel_event,
                    model_provider=global_config.get("worker_model_provider", ""),
                    model_id=global_config.get("worker_model_id", ""),
                    node_id_override=node_id,
                    workspace_directory=workspace_directory,
                    world=world,
                    enable_self_review=(action in REVIEWABLE_ACTIONS),
                )

                if cancel_event.is_set():
                    clean_finish = True
                    await self._finish_run(run_id, "cancelled", "Cancelled by user")
                    return

                self._update_world_from_result(world, node_id, action, sub_result)

                if sub_result.state != "completed":
                    await self._emit("node_failed", run_id, node_id, content=sub_result.error or "")
                    await self._save_checkpoint(
                        run_id, world, sandbox_id, global_config, workspace_directory, dag_json)
                    await self._finish_run(
                        run_id, "failed",
                        f"Node '{node_id}' ({action}) failed: {sub_result.error or 'unknown'}")
                    return

                await self._emit("node_completed", run_id, node_id)
                world.current_file_snapshot = await self._git_diff_stat(sandbox_id)

                # Feed result back to Planner context
                summary = self._extract_worker_summary(sub_result)
                world.planner_review_messages.append({
                    "role": "assistant",
                    "content": f"[{action}] {task_id} completed:\n{summary[:1000]}",
                })

                # Review if coder
                if action in REVIEWABLE_ACTIONS:
                    review_passed = await self._review_loop(
                        run_id, world, sub_result, node_id, action,
                        prompt, target_files, sandbox_id, cancel_event,
                        global_config, workspace_directory,
                    )
                    if not review_passed:
                        if cancel_event.is_set():
                            clean_finish = True
                            await self._finish_run(run_id, "cancelled", "Cancelled")
                            return
                        await self._save_checkpoint(
                            run_id, world, sandbox_id, global_config, workspace_directory, dag_json)
                        await self._finish_run(
                            run_id, "failed",
                            f"Node '{node_id}' failed review {MAX_REVIEW_RETRIES} times")
                        return

                # Commit
                try:
                    await self._checkpoint.auto_commit(
                        sandbox_id, message=f"director: {task_id} ({action})")
                except Exception:
                    pass

                world.iteration += 1
                world.last_node_id = node_id
                world.current_node_index = world.iteration

                await self._save_checkpoint(
                    run_id, world, sandbox_id, global_config, workspace_directory, dag_json)

            # Loop ended via cancellation
            clean_finish = True
            await self._finish_run(run_id, "cancelled", "Cancelled by user")

        except asyncio.CancelledError:
            clean_finish = True
            await self._finish_run(run_id, "cancelled", "Cancelled")
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error("Director loop crashed for run %s:\n%s", run_id, tb)
            await self._finish_run(run_id, "failed", f"Error: {exc}\n{tb[:500]}")
        finally:
            if sandbox_id:
                if workspace_directory:
                    try:
                        await self._sandbox.sync_back(sandbox_id, workspace_directory)
                    except Exception:
                        logger.warning("sync_back failed for run %s", run_id, exc_info=True)
                if clean_finish:
                    try:
                        await self._sandbox.destroy(sandbox_id)
                    except Exception:
                        pass
                else:
                    logger.info("Preserving sandbox %s for resume", sandbox_id[:12])

    # ------------------------------------------------------------------
    # Planner decision call
    # ------------------------------------------------------------------

    async def _call_planner_decide(
        self,
        run_id: str,
        world: WorldModel,
        global_config: dict,
        available_nodes_text: str,
    ) -> dict | None:
        """Call Planner LLM to decide next action."""
        import httpx

        provider = global_config.get("director_model_provider", "")
        model = global_config.get("director_model_id", "")

        from app.core.node_runner import _load_default_model_config, _load_model_config
        if not provider or not model:
            cfg = _load_default_model_config()
            provider = provider or str(cfg.get("provider", ""))
            model = model or str(cfg.get("model", ""))

        model_cfg = _load_model_config(provider, model)
        url = str(model_cfg.get("url", ""))
        api_key = str(model_cfg.get("key", ""))

        if not url or not api_key:
            logger.error("No API URL/key for Planner LLM")
            return None

        mode = global_config.get("mode", "auto")
        mode_label = "MAINTENANCE" if mode == "import" else "DEVELOPMENT"

        system_content = PLANNER_DIRECTOR_SYSTEM.format(
            mode=mode_label,
            world_model=world.to_prompt_context(),
            available_nodes=available_nodes_text,
        )

        messages = list(world.planner_review_messages)
        messages.append({
            "role": "user",
            "content": (
                f"## Current State\nIteration: {world.iteration}\n"
                f"What should we do next? Call the `decide` tool."
            ),
        })
        messages = self._compress_context(messages)

        is_anthropic = "/anthropic" in url or provider == "anthropic"

        if is_anthropic:
            endpoint = f"{url}/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": model,
                "system": system_content,
                "messages": messages,
                "tools": PLANNER_TOOLS,
                "tool_choice": {"type": "tool", "name": DECIDE_TOOL_NAME},
                "max_tokens": 1024,
            }
        else:
            endpoint = f"{url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_content},
                    *messages,
                ],
                "tools": PLANNER_TOOLS_OPENAI,
                "tool_choice": {"type": "function", "function": {"name": DECIDE_TOOL_NAME}},
                "max_tokens": 1024,
            }

        try:
            t0 = time.monotonic()
            timeout = httpx.Timeout(connect=10, read=120, write=10, pool=10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            elapsed_ms = (time.monotonic() - t0) * 1000
            _dbg.log_llm_call(
                __name__, provider=provider, model=model,
                duration_ms=elapsed_ms,
                prompt_preview=f"[PlannerDecide] iter={world.iteration}",
            )

            decision = None
            if is_anthropic:
                for block in (data.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") == DECIDE_TOOL_NAME:
                            inp = block.get("input", {})
                            if isinstance(inp, str):
                                inp = _try_parse_json_dict(inp) or {}
                            decision = inp if isinstance(inp, dict) else None
            else:
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message", {})
                    for tc in (message.get("tool_calls") or []):
                        if tc.get("function", {}).get("name") == DECIDE_TOOL_NAME:
                            decision = _try_parse_json_dict(tc["function"].get("arguments", "{}"))

            if decision:
                world.planner_review_messages.append({
                    "role": "user",
                    "content": f"[Iteration {world.iteration}] Deciding next action...",
                })
                world.planner_review_messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })

            return decision
        except Exception as exc:
            logger.warning("Planner decide call failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Planner review
    # ------------------------------------------------------------------

    async def _review_loop(
        self,
        run_id: str,
        world: WorldModel,
        sub_result: NodeResult,
        node_id: str,
        action: str,
        prompt: str,
        target_files: list[str],
        sandbox_id: str,
        cancel_event: asyncio.Event,
        global_config: dict,
        workspace_directory: str | None,
    ) -> bool:
        summary = self._extract_worker_summary(sub_result)
        await self._emit("worker_summary", run_id, node_id, content=summary[:2000])

        review_passed = False
        current_result = sub_result

        for review_attempt in range(1, MAX_REVIEW_RETRIES + 1):
            if cancel_event.is_set():
                return False

            await self._emit("review_started", run_id, "director",
                             task_id=node_id, attempt=review_attempt)

            review = await self._call_planner_review(
                run_id, world, summary, node_id, world.goal, review_attempt, global_config,
            )

            if review and review.get("result") == "pass":
                review_passed = True
                reason = review.get("reason", "Approved")
                world.record_review(node_id, True, reason, review_attempt)
                await self._emit("review_result", run_id, "director",
                                 task_id=node_id, result="pass", reason=reason, attempt=review_attempt)
                break

            reject_reason = (review or {}).get("reason", "Rejected")
            next_prompt = (review or {}).get("next_prompt", "")
            world.record_review(node_id, False, reject_reason, review_attempt, next_prompt)
            await self._emit("review_result", run_id, "director",
                             task_id=node_id, result="reject", reason=reject_reason, attempt=review_attempt)

            retry_prompt = f"Previous implementation rejected: {reject_reason}\n\nFix guidance: {next_prompt}\n\nOriginal task: {prompt}"
            await self._emit("review_retry", run_id, "director",
                             task_id=node_id, attempt=review_attempt, max_attempts=MAX_REVIEW_RETRIES)

            current_result = await self._run_sub_agent(
                run_id=run_id,
                agent_type="coder",
                system_prompt=WORKER_SYSTEM,
                prompt=self._build_sub_prompt(retry_prompt, target_files, world),
                sandbox_id=sandbox_id,
                cancel_event=cancel_event,
                model_provider=global_config.get("worker_model_provider", ""),
                model_id=global_config.get("worker_model_id", ""),
                node_id_override=node_id,
                workspace_directory=workspace_directory,
                world=world,
                enable_self_review=True,
            )

            self._update_world_from_result(world, node_id, "coder", current_result)
            if current_result.state != "completed":
                return False

            summary = self._extract_worker_summary(current_result)

        return review_passed

    async def _call_planner_review(
        self,
        run_id: str,
        world: WorldModel,
        summary: str,
        node_id: str,
        goal: str,
        review_attempt: int,
        global_config: dict,
    ) -> dict | None:
        import httpx

        provider = global_config.get("director_model_provider", "")
        model = global_config.get("director_model_id", "")

        from app.core.node_runner import _load_default_model_config, _load_model_config
        if not provider or not model:
            cfg = _load_default_model_config()
            provider = provider or str(cfg.get("provider", ""))
            model = model or str(cfg.get("model", ""))

        model_cfg = _load_model_config(provider, model)
        url = str(model_cfg.get("url", ""))
        api_key = str(model_cfg.get("key", ""))
        if not url or not api_key:
            return None

        review_history = ""
        for r in world.reviews:
            if r.task_id == node_id:
                status = "PASS" if r.passed else "REJECT"
                review_history += f"\n  Attempt {r.attempt}: {status} - {r.reason[:150]}"

        user_content = f"## Task\nNode: {node_id}\nGoal: {goal}\nAttempt: {review_attempt}/{MAX_REVIEW_RETRIES}\n"
        if review_history:
            user_content += f"\n## Previous Reviews:{review_history}\n"
        user_content += f"\n## Worker Output\n{summary[:2000]}\n\nReview this output."

        messages = list(world.planner_review_messages)
        messages.append({"role": "user", "content": user_content})
        messages = self._compress_context(messages)

        is_anthropic = "/anthropic" in url or provider == "anthropic"

        if is_anthropic:
            endpoint = f"{url}/v1/messages"
            headers = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
            payload = {
                "model": model, "system": PLANNER_REVIEW_SYSTEM,
                "messages": messages, "tools": [REVIEW_TOOL],
                "tool_choice": {"type": "tool", "name": REVIEW_TOOL_NAME}, "max_tokens": 1024,
            }
        else:
            endpoint = f"{url}/chat/completions"
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": PLANNER_REVIEW_SYSTEM}, *messages],
                "tools": [REVIEW_TOOL_OPENAI],
                "tool_choice": {"type": "function", "function": {"name": REVIEW_TOOL_NAME}},
                "max_tokens": 1024,
            }

        try:
            timeout = httpx.Timeout(connect=10, read=120, write=10, pool=10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            result = None
            if is_anthropic:
                for block in (data.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == REVIEW_TOOL_NAME:
                        inp = block.get("input", {})
                        if isinstance(inp, str):
                            inp = _try_parse_json_dict(inp) or {}
                        result = inp if isinstance(inp, dict) else None
            else:
                choices = data.get("choices") or []
                if choices:
                    for tc in (choices[0].get("message", {}).get("tool_calls") or []):
                        if tc.get("function", {}).get("name") == REVIEW_TOOL_NAME:
                            result = _try_parse_json_dict(tc["function"].get("arguments", "{}"))

            if result:
                world.planner_review_messages.append({"role": "user", "content": user_content})
                world.planner_review_messages.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})
            return result
        except Exception as exc:
            logger.warning("Planner review call failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Context compression
    # ------------------------------------------------------------------

    def _compress_context(self, messages: list[dict]) -> list[dict]:
        total = sum(len(m.get("content", "")) for m in messages)
        if total < CONTEXT_MAX_CHARS or len(messages) <= CONTEXT_KEEP_RECENT:
            return messages

        old = messages[:-CONTEXT_KEEP_RECENT]
        recent = messages[-CONTEXT_KEEP_RECENT:]

        parts = ["[Context Summary - older messages compressed]\n"]
        for msg in old:
            parts.append(f"[{msg.get('role', '?')}] {msg.get('content', '')[:200]}\n")
        summary = "\n".join(parts)
        if len(summary) > 4000:
            summary = summary[:4000] + "\n... (truncated)"

        return [
            {"role": "user", "content": summary},
            {"role": "assistant", "content": "Understood."},
            *recent,
        ]

    # ------------------------------------------------------------------
    # Planner history
    # ------------------------------------------------------------------

    async def _load_planner_history(self, run_id: str) -> list[dict]:
        try:
            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.db import ChatMessage as ChatMessageORM
            from app.models.db import Run as RunModel

            async with async_session_factory() as session:
                result = await session.execute(
                    select(RunModel).where(RunModel.id == uuid.UUID(run_id))
                )
                run_row = result.scalar_one_or_none()
                if not run_row:
                    return []
                wf_result = await session.execute(
                    select(ChatMessageORM)
                    .where(ChatMessageORM.workflow_id == run_row.workflow_id)
                    .order_by(ChatMessageORM.created_at.asc())
                )
                return [
                    {"role": m.role, "content": m.content}
                    for m in wf_result.scalars().all()
                    if m.role in ("user", "assistant") and m.content
                ]
        except Exception:
            logger.warning("Failed to load planner history for run %s", run_id, exc_info=True)
            return []

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
        node_id_override: str = "",
        workspace_directory: str | None = None,
        world: WorldModel | None = None,
        enable_self_review: bool = False,
    ) -> NodeResult:
        full_prompt = f"{system_prompt}\n\n---\n\n## Task\n{prompt}"
        _dbg.log_node_lifecycle(
            __name__, node_id=node_id_override, agent_type=agent_type, event="started",
            prompt_preview=prompt[:300],
        )
        result = await self._node_runner.execute_node(
            run_id=run_id, node_id=node_id_override, agent_type=agent_type,
            prompt=full_prompt, sandbox_id=sandbox_id,
            workspace_directory=workspace_directory, cancel_event=cancel_event,
            model_provider=model_provider, model_id=model_id,
            destroy_sandbox=False, enable_self_review=enable_self_review,
        )
        _dbg.log_node_lifecycle(
            __name__, node_id=node_id_override, agent_type=agent_type,
            event=result.state, error=(result.error or "")[:500],
        )
        return result

    # ------------------------------------------------------------------
    # Helpers
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
                parts.append(f"Files: {', '.join(str(f) for f in files[:10])}")
            return "\n".join(parts)
        return result.result_summary[:2000] if result.result_summary else llm_text[:2000]

    async def _persist_chat_message(self, run_id: str, node_id: str, role: str, content: str) -> None:
        try:
            from app.core.database import async_session_factory
            from app.models.db import ChatMessage as ChatMessageORM
            from app.models.db import Run as RunModel
            async with async_session_factory() as session:
                run_row = await session.get(RunModel, uuid.UUID(run_id))
                if not run_row:
                    return
                session.add(ChatMessageORM(
                    id=uuid.uuid4(), workflow_id=run_row.workflow_id,
                    node_id=node_id, role=role, content=content,
                ))
                await session.commit()
        except Exception:
            logger.debug("Failed to persist chat message", exc_info=True)

    def _update_world_from_result(self, world: WorldModel, task_id: str, action: str, result: NodeResult) -> None:
        if result.state != "completed":
            world.record_failure(task_id, action, result.error or "Sub-agent failed",
                                 prompt_hint=result.result_summary[:150] if result.result_summary else "")
            return
        if not result.raw_output:
            world.record_success(task_id, action, "(no output)")
            return
        llm_text = _extract_llm_text(result.raw_output)
        block_name = "SCOUT_FINDINGS" if action in ("scout", "explore") else "WORKER_RESULT"
        structured = _extract_structured_block(llm_text, block_name)
        if structured:
            summary = structured.get("summary", "")
            files = structured.get("files_changed", structured.get("files_found", []))
            world.record_success(task_id, action, summary or f"{action} completed", files)
        else:
            world.record_success(task_id, action, result.result_summary[:300] or f"{action} completed")

    def _build_sub_prompt(self, prompt: str, target_files: list[str], world: WorldModel) -> str:
        parts = [prompt]
        if target_files:
            parts.append("\n\n## Target Files\n" + "\n".join(f"- {f}" for f in target_files))
        if world.completed_tasks:
            recent = world.completed_tasks[-5:]
            history_lines = [f"  [{'+' if t.success else '-'}] {t.task_id} ({t.action}): {t.summary[:100]}" for t in recent]
            parts.append("\n\n## Recent Steps\n" + "\n".join(history_lines))
        return "\n".join(parts)

    async def _git_diff_stat(self, sandbox_id: str) -> str:
        try:
            git_dir = self._sandbox.resolve_virtual_path(sandbox_id, "/sandbox-meta/.git")
            work_tree = self._sandbox.resolve_virtual_path(sandbox_id, "/workspace")

            def _run():
                return subprocess.run(
                    ["git", f"--git-dir={git_dir}", f"--work-tree={work_tree}", "diff", "--stat", "HEAD"],
                    capture_output=True, text=True, cwd=work_tree,
                )
            result = await asyncio.to_thread(_run)
            stdout = result.stdout
            return (stdout[:1500] + "...") if len(stdout) > 1500 else stdout.strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Checkpoint & finish
    # ------------------------------------------------------------------

    async def _save_checkpoint(self, run_id: str, world: WorldModel, sandbox_id: str,
                               global_config: dict, workspace_directory: str | None, dag_json: dict) -> None:
        try:
            from sqlalchemy import update

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            async with async_session_factory() as session:
                await session.execute(
                    update(RunModel).where(RunModel.id == uuid.UUID(run_id)).values(
                        checkpoint_json={
                            "world_model_json": world.to_json(),
                            "sandbox_id": sandbox_id,
                            "global_config": global_config,
                            "workspace_directory": workspace_directory or "",
                            "dag_json": dag_json,
                            "checkpoint_iteration": world.iteration,
                        }
                    )
                )
                await session.commit()
        except Exception:
            logger.warning("Failed to save checkpoint for run %s", run_id, exc_info=True)

    async def _finish_run(self, run_id: str, status: str, message: str = "") -> None:
        run_state = self._runs.get(run_id)
        if run_state:
            run_state["status"] = status
        await self._update_run_status_db(run_id, status)
        event_type = {"completed": "run_completed", "failed": "run_failed", "cancelled": "run_cancelled"}.get(status, "run_failed")
        await self._emit(event_type, run_id, "director", content=message)
        logger.info("Run %s finished: %s (%s)", run_id, status, message)

    async def _update_run_status_db(self, run_id: str, status: str) -> None:
        try:
            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from app.models.db import Workflow as WorkflowModel
            async with async_session_factory() as session:
                result = await session.execute(select(RunModel).where(RunModel.id == uuid.UUID(run_id)))
                run_row = result.scalar_one_or_none()
                if run_row is not None:
                    run_row.status = status
                    if status in ("completed", "failed", "cancelled"):
                        run_row.completed_at = datetime.now(timezone.utc)
                    wf_result = await session.execute(
                        select(WorkflowModel).where(WorkflowModel.id == run_row.workflow_id))
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
        event = {"event_id": str(uuid.uuid4()), "type": event_type, "run_id": run_id,
                 "node_id": node_id, "timestamp": time.time(), **extra}
        await self._persist_event(event)
        try:
            await self._event_bus.publish(f"run:{run_id}:stream", event)
        except Exception:
            logger.warning("Failed to publish event %s", event_type, exc_info=True)

    async def _persist_event(self, event: dict[str, Any]) -> None:
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run, RunEvent
            run_id = uuid.UUID(str(event.get("run_id", "")))
            async with async_session_factory() as session:
                run = await session.get(Run, run_id)
                if run is None:
                    return
                session.add(RunEvent(
                    run_id=run_id, event_type=str(event.get("type") or ""),
                    node_id=str(event.get("node_id") or ""), payload=event,
                ))
                await session.commit()
        except Exception:
            logger.warning("Failed to persist event %s", event.get("type"), exc_info=True)
