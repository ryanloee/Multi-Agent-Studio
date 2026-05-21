"""DirectorLoop — rule-based serial DAG executor with Planner review.

The Director is a pure scheduler (no LLM). It:
  1. Takes the Planner-generated DAG and topologically sorts it into a serial queue
  2. Executes nodes one by one via NodeRunner (sub-agents)
  3. After coder/merge nodes, calls Planner LLM to review the output
  4. Saves checkpoints for resume capability

Planner review uses full context (user chat history + review records).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.core import debug_logger as _dbg
from app.core.director_prompts import (
    MERGER_SYSTEM,
    PLANNER_REVIEW_SYSTEM,
    SCOUT_SYSTEM,
    TESTER_SYSTEM,
    WORKER_SYSTEM,
)
from app.core.director_tools import (
    MAX_REVIEW_RETRIES,
    REVIEW_TOOL,
    REVIEW_TOOL_CHOICE_ANTHROPIC,
    REVIEW_TOOL_CHOICE_OPENAI,
    REVIEW_TOOL_OPENAI,
)
from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.node_runner import NodeResult, NodeRunner
from app.core.world_model import WorldModel
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)

AGENT_TYPE_TO_PROMPT = {
    "explore": SCOUT_SYSTEM,
    "scout": SCOUT_SYSTEM,
    "coder": WORKER_SYSTEM,
    "worker": WORKER_SYSTEM,
    "shell": TESTER_SYSTEM,
    "tester": TESTER_SYSTEM,
    "merge": MERGER_SYSTEM,
    "merger": MERGER_SYSTEM,
    "review": None,
    "reviewer": None,
    "human": None,
    "plan": None,
    "planner": None,
    "design": None,
}

REVIEWABLE_TYPES = {"coder", "worker", "merge", "merger"}

CONTEXT_KEEP_RECENT = 10
CONTEXT_MAX_CHARS = 80000


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
            tool_name = ev.get("tool_name", "tool")
            parts.append(f"\n[Tool Call: {tool_name}]\n{content}\n")
        elif event_type == "tool_result":
            tool_name = ev.get("tool_name", "tool")
            parts.append(f"\n[Tool Result: {tool_name}]\n{content}\n")
        elif event_type in ("shell_stdout", "shell_stderr"):
            parts.append(f"\n[Shell Output]\n{content}\n")
    if not parsed_any:
        return jsonl_content
    return "".join(parts)


def _topological_sort(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Topological sort of DAG nodes into serial execution order.

    Parallel branches are serialized in stable node-ID order.
    """
    node_map = {n["id"]: n for n in nodes}
    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    children: dict[str, list[str]] = {nid: [] for nid in node_map}

    for edge in edges:
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src in node_map and tgt in node_map:
            in_degree[tgt] = in_degree.get(tgt, 0) + 1
            children[src].append(tgt)

    queue = deque(sorted(nid for nid, deg in in_degree.items() if deg == 0))
    result: list[dict] = []

    while queue:
        nid = queue.popleft()
        result.append(node_map[nid])
        for child in sorted(children[nid]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(result) != len(nodes):
        missing = set(node_map.keys()) - {n["id"] for n in result}
        logger.warning("DAG has cycle or disconnected nodes: %s", missing)
        for nid in sorted(missing):
            result.append(node_map[nid])

    return result


def _estimate_chars(messages: list[dict]) -> int:
    return sum(len(msg.get("content", "")) for msg in messages)


def _get_agent_type(node: dict) -> str:
    data = node.get("data", {})
    return (
        data.get("agentType", "")
        or data.get("agent_type", "")
        or node.get("type", "")
        or "coder"
    ).lower()


class DirectorLoop:
    """Rule-based serial DAG executor with Planner review."""

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

                    world_json = checkpoint.get("world_model_json")
                    resumed_index = checkpoint.get("checkpoint_iteration", 0)
                    if isinstance(world_json, dict):
                        resumed_index = int(
                            world_json.get("current_node_index", resumed_index) or 0
                        )

                    logger.info(
                        "Resuming interrupted run %s from node index %d",
                        run.id,
                        resumed_index,
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
            nodes = dag_json.get("nodes", [])
            edges = dag_json.get("edges", [])

            if not nodes:
                await self._finish_run(run_id, "failed", "No nodes in workflow")
                return

            node_queue = _topological_sort(nodes, edges)
            planner_chat_history = await self._load_planner_history(run_id)

            sandbox_id: str | None = None
            world: WorldModel | None = None

            if resume_from:
                world = WorldModel.from_json(resume_from["world_model_json"])
                old_sandbox_id = resume_from["sandbox_id"]
                dag_json = resume_from.get("dag_json", dag_json)
                global_config = resume_from.get("global_config", global_config)
                workspace_directory = resume_from.get("workspace_directory", workspace_directory)

                node_queue = world.node_queue or node_queue

                if sandbox_existed:
                    try:
                        await self._sandbox.re_register(old_sandbox_id, workspace_directory)
                        sandbox_id = old_sandbox_id
                        logger.info(
                            "Re-registered sandbox %s for resumed run %s at node %d",
                            sandbox_id[:12], run_id, world.current_node_index,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to re-register sandbox %s, will recreate",
                            old_sandbox_id[:12],
                        )
                        sandbox_existed = False

                if not sandbox_existed:
                    try:
                        sandbox_id = await self._sandbox.create(
                            f"ws-director-{run_id[:8]}",
                            template_dir=workspace_directory,
                            user_workspace=workspace_directory,
                        )
                        logger.info(
                            "Recreated sandbox %s for resumed run %s",
                            sandbox_id[:12], run_id,
                        )
                    except Exception:
                        logger.exception("Failed to recreate sandbox for resumed run %s", run_id)
                        await self._finish_run(run_id, "failed", "Failed to recreate sandbox")
                        return

                await self._emit(
                    "run_resumed", run_id, "director",
                    current_node_index=world.current_node_index,
                    total_nodes=len(node_queue),
                )

                if world.node_statuses:
                    for nid, st in world.node_statuses.items():
                        if st == "failed":
                            await self._emit("node_retried", run_id, nid)

                logger.info(
                    "Resumed run %s from node %d/%d",
                    run_id, world.current_node_index, len(node_queue),
                )
            else:
                sandbox_id = await self._sandbox.create(
                    f"ws-director-{run_id[:8]}",
                    template_dir=workspace_directory,
                    user_workspace=workspace_directory,
                )
                logger.info("Created sandbox %s for run %s", sandbox_id[:12], run_id)

                world = WorldModel(goal=goal)
                world.node_queue = node_queue
                world.current_node_index = 0
                world.planner_review_messages = list(planner_chat_history)

            if world is None or sandbox_id is None:
                await self._finish_run(run_id, "failed", "Internal error: state init failed")
                return

            start_time = time.monotonic()
            max_duration = global_config.get("max_duration_seconds", 7200)

            start_idx = world.current_node_index
            for idx in range(start_idx, len(node_queue)):
                if cancel_event.is_set():
                    clean_finish = True
                    await self._finish_run(run_id, "cancelled", "Cancelled by user")
                    return

                elapsed = time.monotonic() - start_time
                if elapsed > max_duration:
                    await self._finish_run(
                        run_id, "failed",
                        f"Workflow time limit exceeded ({max_duration}s).",
                    )
                    return

                node = node_queue[idx]
                node_id = node.get("id", f"node-{idx}")
                agent_type = _get_agent_type(node)
                world.current_node_index = idx

                system_prompt = AGENT_TYPE_TO_PROMPT.get(agent_type)
                if system_prompt is None:
                    logger.info(
                        "Skipping non-executable node %s (type=%s)",
                        node_id, agent_type,
                    )
                    await self._emit("node_started", run_id, node_id)
                    await self._emit("node_completed", run_id, node_id)
                    world.node_statuses[node_id] = "completed"
                    continue

                node_data = node.get("data", {})
                prompt = (
                    node_data.get("prompt", "")
                    or node_data.get("description", "")
                    or f"Execute task: {node_data.get('label', node_id)}"
                )
                target_files = node_data.get("target_files", [])

                model_provider = (
                    node_data.get("modelProvider", "")
                    or global_config.get("worker_model_provider", "")
                )
                model_id = (
                    node_data.get("modelId", "")
                    or global_config.get("worker_model_id", "")
                )

                await self._emit("node_started", run_id, node_id)
                await self._emit(
                    "agent_status", run_id, node_id,
                    content=f"Executing {agent_type} node...",
                )

                _dbg.log_node_lifecycle(
                    __name__, node_id=node_id, agent_type=agent_type, event="started",
                    model_provider=model_provider, model_id=model_id,
                )

                sub_result = await self._run_sub_agent(
                    run_id=run_id,
                    agent_type=agent_type,
                    system_prompt=system_prompt,
                    prompt=self._build_sub_prompt(prompt, target_files, world, agent_type),
                    sandbox_id=sandbox_id,
                    cancel_event=cancel_event,
                    model_provider=model_provider,
                    model_id=model_id,
                    node_id_override=node_id,
                    workspace_directory=workspace_directory,
                    world=world,
                    enable_self_review=(agent_type in ("coder", "worker")),
                )

                _dbg.log_node_lifecycle(
                    __name__, node_id=node_id, agent_type=agent_type,
                    event="completed" if sub_result.state == "completed" else "failed",
                    error=(sub_result.error or "")[:500],
                )

                if cancel_event.is_set():
                    clean_finish = True
                    await self._finish_run(run_id, "cancelled", "Cancelled by user")
                    return

                self._update_world_from_result(world, node_id, agent_type, sub_result)

                if sub_result.state != "completed":
                    await self._emit("node_failed", run_id, node_id, content=sub_result.error or "")
                    clean_finish = False
                    await self._save_checkpoint(
                        run_id, world, sandbox_id, global_config, workspace_directory, dag_json,
                    )
                    await self._finish_run(
                        run_id, "failed",
                        (
                            f"Node '{node_id}' ({agent_type}) failed: "
                            f"{sub_result.error or 'unknown error'}"
                        ),
                    )
                    return

                await self._emit("node_completed", run_id, node_id)
                world.current_file_snapshot = await self._git_diff_stat(sandbox_id)

                if agent_type in REVIEWABLE_TYPES:
                    review_passed = await self._review_loop(
                        run_id=run_id,
                        world=world,
                        sub_result=sub_result,
                        node_id=node_id,
                        agent_type=agent_type,
                        prompt=prompt,
                        target_files=target_files,
                        sandbox_id=sandbox_id,
                        cancel_event=cancel_event,
                        global_config=global_config,
                        workspace_directory=workspace_directory,
                    )

                    if not review_passed:
                        if cancel_event.is_set():
                            clean_finish = True
                            await self._finish_run(run_id, "cancelled", "Cancelled by user")
                            return
                        await self._save_checkpoint(
                            run_id, world, sandbox_id, global_config, workspace_directory, dag_json,
                        )
                        await self._finish_run(
                            run_id, "failed",
                            f"Node '{node_id}' failed review {MAX_REVIEW_RETRIES} times",
                        )
                        return

                try:
                    await self._checkpoint.auto_commit(
                        sandbox_id,
                        message=f"director: {node_id} ({agent_type})",
                    )
                except Exception:
                    pass

                await self._save_checkpoint(
                    run_id, world, sandbox_id, global_config, workspace_directory, dag_json,
                )

            clean_finish = True
            await self._finish_run(run_id, "completed", "All nodes executed successfully")

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
                        logger.warning("sync_back failed for run %s", run_id, exc_info=True)

                if clean_finish:
                    try:
                        await self._sandbox.destroy(sandbox_id)
                        logger.info("Destroyed sandbox %s (clean finish)", sandbox_id[:12])
                    except Exception:
                        pass
                else:
                    logger.info("Preserving sandbox %s for potential resume", sandbox_id[:12])

    async def _review_loop(
        self,
        run_id: str,
        world: WorldModel,
        sub_result: NodeResult,
        node_id: str,
        agent_type: str,
        prompt: str,
        target_files: list[str],
        sandbox_id: str,
        cancel_event: asyncio.Event,
        global_config: dict,
        workspace_directory: str | None,
    ) -> bool:
        summary = self._extract_worker_summary(sub_result)
        await self._emit("worker_summary", run_id, node_id, content=summary[:2000])
        await self._persist_chat_message(
            run_id, node_id, "assistant",
            f"Worker '{node_id}' completed:\n{summary[:1500]}",
        )

        review_passed = False
        reject_reason = ""
        current_result = sub_result

        for review_attempt in range(1, MAX_REVIEW_RETRIES + 1):
            if cancel_event.is_set():
                return False

            await self._emit(
                "review_started", run_id, "director",
                task_id=node_id, attempt=review_attempt,
            )

            review = await self._call_planner_review(
                run_id=run_id,
                world=world,
                summary=summary,
                node_id=node_id,
                goal=world.goal,
                review_attempt=review_attempt,
                global_config=global_config,
            )

            if review and review.get("result") == "pass":
                review_passed = True
                review_reason = review.get("reason", "Approved")
                world.record_review(node_id, True, review_reason, review_attempt)
                await self._emit(
                    "review_result", run_id, "director",
                    task_id=node_id, result="pass",
                    reason=review_reason, attempt=review_attempt,
                )
                await self._persist_chat_message(
                    run_id, node_id, "assistant",
                    f"Review PASSED (attempt {review_attempt}): {review_reason}",
                )
                break
            else:
                reject_reason = (review or {}).get("reason", "Review rejected")
                next_prompt = (review or {}).get("next_prompt", "")
                world.record_review(node_id, False, reject_reason, review_attempt, next_prompt)
                await self._emit(
                    "review_result", run_id, "director",
                    task_id=node_id, result="reject",
                    reason=reject_reason, attempt=review_attempt,
                )
                await self._persist_chat_message(
                    run_id, node_id, "assistant",
                    f"Review REJECTED (attempt {review_attempt}): {reject_reason}\n"
                    f"Guidance: {next_prompt}",
                )

                retry_prompt = (
                    f"Previous implementation was rejected: {reject_reason}\n"
                    f"Please fix the code based on the feedback.\n"
                )
                if next_prompt:
                    retry_prompt += f"\nFix guidance: {next_prompt}\n"
                retry_prompt += f"\nOriginal task: {prompt}"

                await self._emit(
                    "review_retry", run_id, "director",
                    task_id=node_id, attempt=review_attempt,
                    max_attempts=MAX_REVIEW_RETRIES,
                )

                current_result = await self._run_sub_agent(
                    run_id=run_id,
                    agent_type="coder",
                    system_prompt=WORKER_SYSTEM,
                    prompt=self._build_sub_prompt(retry_prompt, target_files, world, "worker"),
                    sandbox_id=sandbox_id,
                    cancel_event=cancel_event,
                    model_provider=global_config.get("worker_model_provider", ""),
                    model_id=global_config.get("worker_model_id", ""),
                    node_id_override=node_id,
                    workspace_directory=workspace_directory,
                    world=world,
                    enable_self_review=True,
                )

                self._update_world_from_result(world, node_id, "worker", current_result)

                if current_result.state != "completed":
                    return False

                summary = self._extract_worker_summary(current_result)
                await self._emit("worker_summary", run_id, node_id, content=summary[:2000])
                await self._persist_chat_message(
                    run_id, node_id, "assistant",
                    f"Worker '{node_id}' revised:\n{summary[:1500]}",
                )

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
        """Call Planner LLM to review worker output with full context."""
        import httpx

        planner_provider = global_config.get("director_model_provider", "")
        planner_model = global_config.get("director_model_id", "")

        from app.core.node_runner import _load_default_model_config, _load_model_config
        if not planner_provider or not planner_model:
            cfg = _load_default_model_config()
            planner_provider = planner_provider or str(cfg.get("provider") or "")
            planner_model = planner_model or str(cfg.get("model") or "")

        model_cfg = _load_model_config(planner_provider, planner_model)
        url = str(model_cfg.get("url", ""))
        api_key = str(model_cfg.get("key", ""))

        if not url or not api_key:
            logger.error("No API URL/key for Planner review LLM")
            return None

        review_history = ""
        for r in world.reviews:
            if r.task_id == node_id:
                status = "PASS" if r.passed else "REJECT"
                review_history += f"\n  Attempt {r.attempt}: {status} - {r.reason[:150]}"

        user_content = (
            f"## Task\nNode ID: {node_id}\nGoal: {goal}\n"
            f"Review attempt: {review_attempt}/{MAX_REVIEW_RETRIES}\n"
        )
        if review_history:
            user_content += f"\n## Previous Reviews for this task:{review_history}\n"
        user_content += f"\n## Worker Output Summary\n{summary[:2000]}\n\nReview this output."

        messages = list(world.planner_review_messages)
        messages.append({"role": "user", "content": user_content})

        messages = self._compress_planner_context(messages)

        is_anthropic = "/anthropic" in url or planner_provider == "anthropic"

        if is_anthropic:
            endpoint = f"{url}/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": planner_model,
                "system": PLANNER_REVIEW_SYSTEM,
                "messages": messages,
                "tools": [REVIEW_TOOL],
                "tool_choice": REVIEW_TOOL_CHOICE_ANTHROPIC,
                "max_tokens": 1024,
            }
        else:
            endpoint = f"{url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            payload = {
                "model": planner_model,
                "messages": [
                    {"role": "system", "content": PLANNER_REVIEW_SYSTEM},
                    *messages,
                ],
                "tools": [REVIEW_TOOL_OPENAI],
                "tool_choice": REVIEW_TOOL_CHOICE_OPENAI,
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
                __name__, provider=planner_provider, model=planner_model,
                duration_ms=elapsed_ms,
                prompt_preview=f"[PlannerReview] node={node_id} attempt={review_attempt}",
            )

            review_result = None
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
                        review_result = inp if isinstance(inp, dict) else None
            else:
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message", {})
                    tool_calls = message.get("tool_calls") or []
                    for tc in tool_calls:
                        if tc.get("function", {}).get("name") == "review":
                            args = tc["function"].get("arguments", "{}")
                            review_result = _try_parse_json_dict(args)

            if review_result:
                world.planner_review_messages.append({"role": "user", "content": user_content})
                assistant_content = json.dumps(review_result, ensure_ascii=False)
                world.planner_review_messages.append(
                    {"role": "assistant", "content": assistant_content}
                )

            return review_result
        except Exception as exc:
            logger.warning("Planner review LLM call failed: %s", exc)
            return None

    def _compress_planner_context(self, messages: list[dict]) -> list[dict]:
        """Compress planner context if it exceeds the character threshold."""
        total_chars = _estimate_chars(messages)
        if total_chars < CONTEXT_MAX_CHARS:
            return messages

        if len(messages) <= CONTEXT_KEEP_RECENT:
            return messages

        old_messages = messages[:-CONTEXT_KEEP_RECENT]
        recent_messages = messages[-CONTEXT_KEEP_RECENT:]

        summary_parts = ["[Context Summary - older messages compressed]\n"]
        for msg in old_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            summary_parts.append(f"[{role}] {content[:200]}\n")

        summary = "\n".join(summary_parts)
        if len(summary) > 4000:
            summary = summary[:4000] + "\n... (further truncated)"

        return [
            {"role": "user", "content": summary},
            {"role": "assistant", "content": "Understood, I have the context summary."},
            *recent_messages,
        ]

    async def _load_planner_history(self, run_id: str) -> list[dict]:
        """Load Planner chat history from ChatMessage table."""
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
                messages = wf_result.scalars().all()

                result_msgs: list[dict] = []
                for msg in messages:
                    if msg.role in ("user", "assistant") and msg.content:
                        result_msgs.append({"role": msg.role, "content": msg.content})

                return result_msgs
        except Exception:
            logger.warning("Failed to load planner history for run %s", run_id, exc_info=True)
            return []

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
            model_provider=model_provider, model_id=model_id,
            prompt_preview=prompt[:300],
        )

        result = await self._node_runner.execute_node(
            run_id=run_id,
            node_id=node_id_override,
            agent_type=agent_type,
            prompt=full_prompt,
            sandbox_id=sandbox_id,
            workspace_directory=workspace_directory,
            cancel_event=cancel_event,
            model_provider=model_provider,
            model_id=model_id,
            destroy_sandbox=False,
            enable_self_review=enable_self_review,
        )

        _dbg.log_node_lifecycle(
            __name__, node_id=node_id_override, agent_type=agent_type,
            event=result.state, exit_code=result.exit_code,
            error=(result.error or "")[:500],
            raw_output_len=len(result.raw_output or ""),
        )

        return result

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

    async def _persist_chat_message(
        self, run_id: str, node_id: str, role: str, content: str,
    ) -> None:
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

    def _update_world_from_result(
        self, world: WorldModel, task_id: str, action: str, result: NodeResult,
    ) -> None:
        if result.state != "completed":
            prompt_hint = result.result_summary[:150] if result.result_summary else ""
            world.record_failure(
                task_id, action,
                result.error or "Sub-agent failed",
                prompt_hint=prompt_hint,
            )
            return

        if not result.raw_output:
            world.record_success(task_id, action, "(no output)")
            return

        llm_text = _extract_llm_text(result.raw_output)
        files_changed: list[str] = []

        block_name = "SCOUT_FINDINGS" if action in ("scout", "explore") else "WORKER_RESULT"
        structured = _extract_structured_block(llm_text, block_name)

        if structured:
            summary = structured.get("summary", "")
            files_changed = structured.get("files_changed", structured.get("files_found", []))
            if not summary:
                summary = f"{action} completed"
            world.record_success(task_id, action, summary, files_changed)
        else:
            if result.result_summary:
                summary = result.result_summary[:300]
            else:
                summary = f"{action} completed (no structured output)"
            world.record_success(task_id, action, summary)

    def _build_sub_prompt(
        self, prompt: str, target_files: list[str], world: WorldModel, action: str,
    ) -> str:
        parts = [prompt]
        if target_files:
            parts.append("\n\n## Target Files\n" + "\n".join(f"- {f}" for f in target_files))
        if world.completed_tasks:
            recent = world.completed_tasks[-5:]
            history_lines = []
            for t in recent:
                icon = "+" if t.success else "-"
                history_lines.append(f"  [{icon}] {t.task_id} ({t.action}): {t.summary[:100]}")
            parts.append("\n\n## Recent Steps\n" + "\n".join(history_lines))
        return "\n".join(parts)

    async def _git_diff_stat(self, sandbox_id: str) -> str:
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
                "checkpoint_iteration": world.current_node_index,
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
