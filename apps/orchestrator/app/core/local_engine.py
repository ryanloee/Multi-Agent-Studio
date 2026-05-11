"""Local asyncio-based DAG executor.

Replaces Temporal.io workflow engine with a pure-asyncio implementation that
executes DAG layers sequentially, nodes within each layer in parallel.

Orchestration flow for each node:
  1. Create sandbox container
  2. Provision workspace directories + Git init
  3. Git checkpoint (auto-commit before agent runs)
  4. Build and launch mas_agent command
  5. Poll process status, streaming events from stream.jsonl
  6. On completion: emit node_completed/node_failed, destroy sandbox
  7. Plan nodes: parse output and execute dynamic child tasks
"""

import asyncio
import json
import logging
import os
import shlex
import uuid
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.task_scheduler import TaskScheduler, ESCALATION_MARKER, PROGRESS_MARKER
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner
from app.workflows.compiler import compile_dag
from app.workflows.plan_parser import parse_plan_output, parse_plan_to_dag, PLAN_SYSTEM_SUFFIX

logger = logging.getLogger(__name__)

# Event types that the agent writes to stream.jsonl in the correct format
_KNOWN_EVENT_TYPES = frozenset({
    "llm_token", "llm_chunk", "tool_call", "tool_result", "shell_stdout",
    "shell_stderr", "status", "error", "node_started", "node_completed",
    "node_failed", "child_created", "child_completed",
    "task_created", "task_updated", "task_message", "worker_escalation",
})

# Resolve the mas_agent package directory: apps/agent/ relative to project root
# __file__ = .../apps/orchestrator/app/core/local_engine.py
# parents[0] = .../apps/orchestrator/app/core
# parents[1] = .../apps/orchestrator/app
# parents[2] = .../apps/orchestrator
# parents[3] = .../apps
# Project root = parents[4] = .../mat
_AGENT_PKG_DIR = str(Path(__file__).resolve().parents[4] / "apps" / "agent")


def _build_subprocess_env() -> dict[str, str]:
    """Build environment for subprocess with mas_agent on PYTHONPATH."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    extra = _AGENT_PKG_DIR
    env["PYTHONPATH"] = f"{extra}{os.pathsep}{existing}" if existing else extra
    return env


def _load_default_model_config() -> dict[str, str]:
    """Load the first configured UI model for agent execution fallback."""
    settings_path = Path(__file__).resolve().parents[3] / "data" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    models = data.get("models", [])
    if not isinstance(models, list) or not models:
        return {}

    first = models[0]
    if not isinstance(first, dict):
        return {}

    return {
        "provider": str(first.get("format") or ""),
        "model": str(first.get("default_model") or first.get("name") or ""),
        "url": str(first.get("base_url") or "").rstrip("/"),
        "key": str(first.get("api_key") or ""),
    }


class LocalDAGExecutor:
    """Local asyncio-based DAG executor.  Replaces Temporal."""

    def __init__(
        self,
        sandbox: LocalSandbox,
        event_bus: InProcessEventBus,
        checkpoint: GitCheckpointManager,
        provisioner: SandboxProvisioner,
    ):
        self._sandbox = sandbox
        self._event_bus = event_bus
        self._checkpoint = checkpoint
        self._provisioner = provisioner
        # run_id -> {"status": str, "task": asyncio.Task, "cancel_event": asyncio.Event}
        self._runs: dict[str, dict[str, Any]] = {}

        # Task scheduler for dynamic task-driven execution
        self._task_scheduler = TaskScheduler(
            sandbox=sandbox,
            event_bus=event_bus,
            checkpoint=checkpoint,
            provisioner=provisioner,
            execute_node_fn=self._execute_node,
            emit_fn=self._emit,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_workflow(
        self,
        run_id: str,
        layers: list[dict],
        global_config: dict | None = None,
        workspace_directory: str | None = None,
    ) -> str:
        """Start DAG execution as a background asyncio task."""
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            self._execute_dag(
                run_id, layers, global_config or {}, cancel_event,
                workspace_directory=workspace_directory,
            ),
            name=f"dag-{run_id}",
        )

        def _log_task_exception(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.exception("DAG task failed for run %s", run_id)

        task.add_done_callback(_log_task_exception)

        self._runs[run_id] = {
            "status": "running",
            "task": task,
            "cancel_event": cancel_event,
            "global_config": global_config or {},
        }
        logger.info("DAG task created for run %s with %d layers", run_id, len(layers))
        return run_id

    async def start_task_dag(
        self,
        run_id: str,
        dag_json: dict,
        global_config: dict | None = None,
        workspace_directory: str | None = None,
    ) -> str:
        """Start an already-planned DAG and mirror each node to the task board."""
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            self._execute_task_dag(
                run_id, dag_json, global_config or {}, cancel_event,
                workspace_directory=workspace_directory,
            ),
            name=f"task-dag-{run_id}",
        )

        def _log_task_exception(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.exception("Task DAG failed for run %s", run_id)

        task.add_done_callback(_log_task_exception)

        self._runs[run_id] = {
            "status": "running",
            "task": task,
            "cancel_event": cancel_event,
            "global_config": global_config or {},
        }
        logger.info(
            "Task DAG created for run %s with %d nodes",
            run_id, len(dag_json.get("nodes", [])),
        )
        return run_id

    async def get_status(self, run_id: str) -> dict:
        """Return current execution status for a run."""
        run = self._runs.get(run_id)
        if not run:
            return {"status": "unknown"}
        return {"status": run["status"]}

    async def cancel(self, run_id: str) -> None:
        """Request cancellation of a running workflow."""
        run = self._runs.get(run_id)
        if run and run["status"] in ("running",):
            run["cancel_event"].set()
            run["status"] = "cancelling"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit(
        self, event_type: str, run_id: str, node_id: str, **extra: Any
    ) -> None:
        """Publish an event to the in-process event bus."""
        event = {
            "type": event_type,
            "run_id": run_id,
            "node_id": node_id,
            **extra,
        }
        channel = f"run:{run_id}:stream"
        try:
            await self._event_bus.publish(channel, event)
        except Exception:
            logger.warning("Failed to publish event %s", event_type, exc_info=True)

    @staticmethod
    def _extract_llm_text(jsonl_content: str) -> str:
        """Extract plain LLM text from stream.jsonl event lines.

        Concatenates content from llm_token, llm_chunk, and text events
        so the plan parser can find embedded JSON code blocks.
        Tokens are naturally contiguous fragments — joining with empty
        string preserves the original text without injecting extra newlines.
        """
        parts: list[str] = []
        for line in jsonl_content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_type = ev.get("type", "")
            if ev_type in ("llm_token", "llm_chunk", "text"):
                parts.append(ev.get("content", ""))
        return "".join(parts)

    async def _route_escalation(
        self, run_id: str, source_node_id: str, message: str
    ) -> None:
        """Route an escalation from a worker to its upstream node.

        In auto mode, the planner is always the upstream — the existing
        TaskScheduler handles this.  In manual mode, we look up the
        edges to find the direct upstream node(s) and emit a
        ``escalation_routed`` event targeting them.  The UI can then
        display the escalation in the upstream node's output panel.

        If the upstream node has already completed, we inject the
        escalation as a new message into the run's event stream.
        """
        run_state = self._runs.get(run_id, {})
        global_config = run_state.get("global_config", {})
        edges: list[dict] = global_config.get("_edges", [])

        # Find upstream nodes (edges where source_node_id is the target)
        upstream_ids = [
            e["source"] for e in edges
            if e.get("target") == source_node_id
        ]

        if not upstream_ids:
            logger.info(
                "Escalation from %s: no upstream node found in edges, "
                "broadcasting to run",
                source_node_id,
            )
            # No upstream — just broadcast
            await self._emit(
                "escalation_broadcast", run_id, source_node_id,
                content=message,
            )
            return

        # Route to all upstream nodes (typically just one in serial chains)
        for upstream_id in upstream_ids:
            logger.info(
                "Escalation from %s routed to upstream %s: %s",
                source_node_id, upstream_id, message[:100],
            )
            await self._emit(
                "escalation_routed", run_id, upstream_id,
                content=message,
                source_node_id=source_node_id,
            )

    def _build_upstream_context(
        self,
        node_id: str,
        edges: list[dict],
        layer_results: dict[str, Any],
    ) -> str:
        """Build a formatted string summarising upstream node outputs.

        For each upstream edge targeting *node_id*, extracts the
        ``result_summary`` from the upstream result, or falls back to the
        last 2 000 characters of LLM text in ``raw_output``.
        Returns an empty string when there are no upstream edges.
        """
        upstream_edges = [e for e in edges if e.get("target") == node_id]
        if not upstream_edges:
            return ""

        sections: list[str] = []
        for edge in upstream_edges:
            # Only inject summary if the edge allows it
            edge_data = edge.get("data", {})
            if edge_data.get("transfer_summary", True) is False:
                continue

            source_id = edge.get("source", "")
            if not source_id:
                continue
            source_result = layer_results.get(source_id)
            if not source_result or not isinstance(source_result, dict):
                continue

            summary = source_result.get("result_summary", "")
            if not summary:
                raw = source_result.get("raw_output", "")
                if raw:
                    full_text = self._extract_llm_text(raw)
                    summary = full_text[-2000:]

            if summary:
                sections.append(f"### {source_id}\n{summary}")

        if not sections:
            return ""

        return "\n\n## 上游节点输出\n" + "\n".join(sections) + "\n"

    async def _execute_layers(
        self,
        run_id: str,
        layers: list,
        edges: list[dict],
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
    ) -> dict[str, Any]:
        """Execute compiled DAG layers with sandbox reuse and upstream context.

        Iterates layers sequentially; within each layer, nodes execute in
        parallel.  Returns a dict mapping node_id -> result for all executed
        nodes.  Used by both ``_execute_dag`` and ``_execute_dynamic_plan``.
        """
        layer_results: dict[str, Any] = {}
        sandbox_map: dict[str, str] = {}  # node_id -> sandbox_id
        commit_map: dict[str, str] = {}    # node_id -> last commit hash
        retained_sandboxes: set[str] = set()

        for layer_idx, layer in enumerate(layers):
            if cancel_event.is_set():
                break

            # Layer may be a list of node dicts directly (from the engine's
            # serialisation) or a dict with a "nodes" key.
            if isinstance(layer, dict):
                nodes = layer.get("nodes", [])
                if not nodes:
                    nodes = [layer]
            else:
                nodes = layer

            logger.info(
                "Layers run=%s layer %d: executing %d nodes",
                run_id, layer_idx, len(nodes),
            )

            # -- Resolve sandbox reuse strategy for each node in the layer --
            # Single upstream: reuse if not already claimed; otherwise clone.
            # Multi-upstream: reuse the primary (last) upstream's sandbox;
            #   if already claimed, clone it.  All upstream summaries are
            #   injected separately via _build_upstream_context.
            # No upstreams: create a brand-new sandbox.
            layer_sandbox_assignments: dict[str, str | None] = {}
            layer_clone_requests: dict[str, str] = {}  # n_id -> source sandbox_id to clone
            reused_upstream_ids: set[str] = set()  # upstream ids already claimed

            for node in nodes:
                n_id = node.get("id", node.get("node_id", ""))
                upstream_ids = [
                    e["source"] for e in edges
                    if e.get("target") == n_id
                ]
                resolved_sid: str | None = None

                if len(upstream_ids) >= 1:
                    # Pick the primary upstream (last in the edges list).
                    # For single-upstream this is the only one; for
                    # multi-upstream the last edge is the primary.
                    primary_upstream = upstream_ids[-1]
                    candidate = sandbox_map.get(primary_upstream)

                    # Check transfer_files on the connecting edge
                    transfer_edge = next(
                        (e for e in edges
                         if e.get("target") == n_id and e.get("source") == primary_upstream),
                        {},
                    )
                    transfer_files = transfer_edge.get("data", {}).get("transfer_files", True)

                    if candidate and transfer_files is not False:
                        if primary_upstream not in reused_upstream_ids:
                            # Reuse the primary upstream's sandbox directly
                            resolved_sid = candidate
                            reused_upstream_ids.add(primary_upstream)
                        else:
                            # Another parallel node already claimed it --
                            # clone the sandbox so this node gets a COPY
                            # instead of an empty one.
                            layer_clone_requests[n_id] = candidate
                # else: 0 upstreams -> resolved_sid stays None (new sandbox)

                layer_sandbox_assignments[n_id] = resolved_sid

            # Execute all nodes in this layer concurrently
            tasks = []
            for node in nodes:
                n_id = node.get("id", node.get("node_id", ""))
                assigned_sid = layer_sandbox_assignments.get(n_id)
                task_id = (global_config.get("_task_db_map") or {}).get(n_id)
                if task_id:
                    await self._update_task_status_db(
                        task_id, "running", progress=5,
                    )
                    await self._emit(
                        "task_updated", run_id, "",
                        task_id=task_id,
                        status="running",
                        progress=5,
                        assigned_node_id=n_id,
                    )

                # If this node needs a clone, do it now (before launching)
                if assigned_sid is None and n_id in layer_clone_requests:
                    source_sid = layer_clone_requests[n_id]
                    try:
                        assigned_sid = await self._sandbox.clone(
                            source_sid,
                            f"ws-{n_id}-{uuid4().hex[:8]}",
                        )
                        logger.info(
                            "Cloned sandbox %s -> %s for parallel node %s",
                            source_sid[:12], assigned_sid[:12], n_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to clone sandbox %s for node %s, "
                            "will create a new empty sandbox",
                            source_sid[:12], n_id, exc_info=True,
                        )
                        assigned_sid = None

                tasks.append(
                    self._execute_node(
                        run_id, node, layer_results, global_config, cancel_event,
                        workspace_directory=workspace_directory,
                        sandbox_id=assigned_sid,
                        upstream_context=self._build_upstream_context(
                            n_id, edges, layer_results,
                        ),
                        destroy_owned_sandbox=False,
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for node, result in zip(nodes, results):
                node_id = node.get("id", node.get("node_id", ""))
                if isinstance(result, Exception):
                    import traceback
                    tb = "".join(traceback.format_exception(type(result), result, result.__traceback__)) if result.__traceback__ else str(result)
                    logger.error(
                        "Layers run=%s node %s failed: %s\n%s",
                        run_id, node_id, result, tb,
                    )
                    layer_results[node_id] = {
                        "state": "failed",
                        "error": str(result),
                    }
                else:
                    layer_results[node_id] = result
                    # Track sandbox for downstream reuse
                    _sid = result.get("sandbox_id")
                    if _sid:
                        sandbox_map[node_id] = _sid
                        retained_sandboxes.add(_sid)

                        # Persist to run state for API access
                        run_state = self._runs.get(run_id, {})
                        if not run_state.get("_sandbox_map"):
                            run_state["_sandbox_map"] = {}
                        run_state["_sandbox_map"][node_id] = _sid

                        # Post-node git checkpoint + commit_map tracking
                        try:
                            commit_hash = await self._checkpoint.auto_commit(
                                _sid, message=f"after [{node_id}]"
                            )
                            if commit_hash:
                                commit_map[node_id] = commit_hash

                                # Persist to run state for API access
                                run_state = self._runs.get(run_id, {})
                                if not run_state.get("_commit_map"):
                                    run_state["_commit_map"] = {}
                                run_state["_commit_map"][node_id] = commit_hash
                        except Exception:
                            pass

                    # Plan node: parse output and execute dynamic children
                    _node_data = node.get("data", {})
                    _atype = (
                        node.get("agent_type")
                        or node.get("type")
                        or (_node_data.get("agentType") if isinstance(_node_data, dict) else None)
                    )
                    logger.info(
                        "Layers run=%s node %s: agent_type=%s, state=%s, has_raw_output=%s",
                        run_id, node_id, _atype, result.get("state"),
                        bool(result.get("raw_output")),
                    )
                    if (
                        _atype == "plan"
                        and result.get("state") == "completed"
                        and not global_config.get("_disable_dynamic_plan")
                    ):
                        plan_results = await self._execute_dynamic_plan(
                            run_id, node_id, result, global_config, cancel_event,
                            planner_node=node,
                        )
                        layer_results.update(plan_results)

        for sandbox_id in retained_sandboxes:
            try:
                await self._sandbox.destroy(sandbox_id)
            except Exception:
                logger.debug("Failed to destroy retained sandbox %s", sandbox_id, exc_info=True)

        return layer_results

    # ------------------------------------------------------------------
    # Top-level DAG execution (manages _runs state)
    # ------------------------------------------------------------------

    async def _update_run_status_db(self, run_id: str, status: str) -> None:
        """Update run status in the database."""
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from uuid import UUID as _UUID
            async with async_session_factory() as session:
                from sqlalchemy import select
                result = await session.execute(
                    select(RunModel).where(RunModel.id == _UUID(run_id))
                )
                run_row = result.scalar_one_or_none()
                if run_row is not None:
                    run_row.status = status
                    await session.commit()
        except Exception:
            logger.warning("Failed to update run status in DB for %s", run_id, exc_info=True)

    async def _update_task_status_db(
        self,
        task_id: str,
        status: str,
        progress: int = 0,
        result_summary: str = "",
    ) -> None:
        """Update task-board row status for a running DAG node."""
        try:
            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel
            from sqlalchemy import select
            from uuid import UUID as _UUID

            async with async_session_factory() as session:
                result = await session.execute(
                    select(TaskModel).where(TaskModel.id == _UUID(task_id))
                )
                task_row = result.scalar_one_or_none()
                if task_row is not None:
                    task_row.status = status
                    task_row.progress = progress
                    if result_summary:
                        task_row.result_summary = result_summary
                    await session.commit()
        except Exception:
            logger.warning("Failed to update task status for %s", task_id, exc_info=True)

    async def _execute_task_dag(
        self,
        run_id: str,
        dag_json: dict,
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
    ) -> None:
        """Execute a saved DAG while creating task-board rows for every node."""
        logger.info(
            "Task DAG execution STARTED for run %s, %d nodes",
            run_id, len(dag_json.get("nodes", [])),
        )

        try:
            await self._emit("run_started", run_id, "")

            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel
            from uuid import uuid4

            task_db_map: dict[str, str] = {}
            normalized_nodes: list[dict] = []

            async with async_session_factory() as session:
                for node in dag_json.get("nodes", []):
                    if not isinstance(node, dict):
                        continue

                    node_id = node.get("id", "")
                    if not node_id:
                        continue

                    data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
                    agent_type = (
                        node.get("agent_type")
                        or data.get("agent_type")
                        or data.get("agentType")
                        or node.get("type")
                        or "coder"
                    )
                    label = data.get("label") or node.get("label") or node_id
                    prompt = node.get("prompt") or data.get("prompt") or ""
                    model_provider = (
                        node.get("model_provider")
                        or data.get("model_provider")
                        or data.get("modelProvider")
                        or ""
                    )
                    model_id = (
                        node.get("model_id")
                        or data.get("model_id")
                        or data.get("modelId")
                        or ""
                    )

                    task_id = uuid4()
                    db_task = TaskModel(
                        id=task_id,
                        run_id=uuid.UUID(run_id),
                        title=str(label)[:200],
                        description=prompt,
                        status="pending",
                        assigned_node_id=node_id,
                        assigned_worker_label=str(label),
                    )
                    session.add(db_task)
                    task_db_map[node_id] = str(task_id)

                    normalized_nodes.append({
                        "id": node_id,
                        "agent_type": agent_type,
                        "model_provider": model_provider,
                        "model_id": model_id,
                        "prompt": (
                            prompt
                            + f"\n\n---\nTask ID: {node_id}\n"
                            + f"When you need help or are stuck, output a line:\n"
                            + f"{ESCALATION_MARKER} <your question>\n"
                            + f"To report progress, output a line:\n"
                            + f"{PROGRESS_MARKER} <0-100>\n"
                        ),
                    })

                    await self._emit(
                        "task_created", run_id, "planner",
                        task_id=str(task_id),
                        task_title=db_task.title,
                        task_description=db_task.description,
                        status="pending",
                        child_node_id=node_id,
                    )
                    await self._emit(
                        "child_created", run_id, "planner",
                        child_node_id=node_id,
                        child_type=agent_type,
                        child_prompt=prompt,
                        child_model=(
                            f"{model_provider}/{model_id}"
                            if model_provider and model_id else model_id
                        ),
                    )

                await session.commit()

            sub_dag = {
                "nodes": normalized_nodes,
                "edges": dag_json.get("edges", []),
            }
            layers = compile_dag(sub_dag)
            global_config["_edges"] = sub_dag["edges"]
            global_config["_task_db_map"] = task_db_map
            global_config["_disable_dynamic_plan"] = True

            child_results = await self._execute_layers(
                run_id, layers, sub_dag["edges"], global_config, cancel_event,
                workspace_directory=workspace_directory,
            )

            for node_id, result in child_results.items():
                task_id = task_db_map.get(node_id)
                if not task_id:
                    continue
                state = result.get("state", "failed") if isinstance(result, dict) else "failed"
                summary = ""
                if isinstance(result, dict):
                    summary = result.get("error", "")
                    if state == "completed":
                        raw = result.get("raw_output", "")
                        summary = self._task_scheduler._summarize(raw) if raw else "completed"

                await self._update_task_status_db(
                    task_id,
                    state,
                    progress=100 if state == "completed" else 0,
                    result_summary=summary,
                )
                await self._emit(
                    "task_updated", run_id, "",
                    task_id=task_id,
                    status=state,
                    progress=100 if state == "completed" else 0,
                    result_summary=summary,
                )

            has_failed = any(
                isinstance(result, dict) and result.get("state") == "failed"
                for result in layer_results.values()
            )
            status = "cancelled" if cancel_event.is_set() else ("failed" if has_failed else "completed")
            self._runs[run_id]["status"] = status
            event_type = "run_completed" if status == "completed" else "run_failed"
            await self._emit(event_type, run_id, "", content=f"status={status}")
            await self._update_run_status_db(run_id, status)

        except Exception as exc:
            logger.exception("Task DAG execution failed for run %s", run_id)
            self._runs[run_id]["status"] = "failed"
            await self._emit("run_failed", run_id, "", content=str(exc))
            await self._update_run_status_db(run_id, "failed")

    async def _execute_dag(
        self,
        run_id: str,
        layers: list[dict],
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
    ) -> None:
        """Top-level DAG execution loop.  Delegates to _execute_layers for
        actual node execution and manages run lifecycle state."""
        logger.info("DAG execution STARTED for run %s, %d layers", run_id, len(layers))
        try:
            await self._emit("run_started", run_id, "")
            edges = global_config.get("_edges", [])

            layer_results = await self._execute_layers(
                run_id, layers, edges, global_config, cancel_event,
                workspace_directory=workspace_directory,
            )

            # Persist commit_map and sandbox_map into _runs for API access
            run_state = self._runs.get(run_id, {})
            run_state["_commit_map"] = run_state.get("_commit_map", {})
            run_state["_sandbox_map"] = run_state.get("_sandbox_map", {})
            # Store initial commit hash (first checkpoint of the run)
            if "_initial" not in run_state.get("_commit_map", {}):
                commit_map = run_state.get("_commit_map", {})
                # The first entry by time is the initial commit
                if commit_map:
                    first_key = next(iter(commit_map))
                    run_state["_commit_map"]["_initial"] = commit_map[first_key]

            has_failed = any(
                isinstance(result, dict) and result.get("state") == "failed"
                for result in child_results.values()
            )
            status = "cancelled" if cancel_event.is_set() else ("failed" if has_failed else "completed")
            self._runs[run_id]["status"] = status
            event_type = "run_completed" if status == "completed" else "run_failed"
            await self._emit(event_type, run_id, "", content=f"status={status}")
            await self._update_run_status_db(run_id, status)

        except Exception as exc:
            logger.exception("DAG execution failed for run %s", run_id)
            self._runs[run_id]["status"] = "failed"
            await self._emit("run_failed", run_id, "", content=str(exc))
            await self._update_run_status_db(run_id, "failed")

    async def _execute_node(
        self,
        run_id: str,
        node: dict,
        layer_results: dict[str, Any],
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
        sandbox_id: str | None = None,
        upstream_context: str = "",
        destroy_owned_sandbox: bool = True,
    ) -> dict:
        """Execute a single DAG node: create sandbox, run agent, stream events.

        Args:
            sandbox_id: If provided, reuse this existing sandbox instead of
                creating a new one.  The caller is responsible for its lifecycle.
            upstream_context: Formatted text describing upstream node outputs;
                appended to the prompt before writing prompt.txt.
        """
        node_id: str = node.get("id", node.get("node_id", ""))
        subprocess_env = _build_subprocess_env()

        await self._emit("node_started", run_id, node_id)
        await self._emit("status", run_id, node_id, content="running")

        # 1. Create sandbox container (use workspace_directory as template if set)
        _owns_sandbox = False
        if sandbox_id is None:
            workspace_id = f"ws-{node_id}-{uuid4().hex[:8]}"
            sandbox_id = await self._sandbox.create(
                workspace_id, template_dir=workspace_directory,
            )
            _owns_sandbox = True
            logger.info("Created sandbox %s for node %s", sandbox_id[:12], node_id)
        else:
            logger.info("Reusing sandbox %s for node %s", sandbox_id[:12], node_id)

        stream_file = "/workspace/.agent/stream.jsonl"

        try:
            # 2. Provision workspace
            try:
                await self._provisioner.provision(sandbox_id, node)
            except Exception as exc:
                logger.warning("Provisioning failed for %s: %s", node_id, exc)

            # 3. Git checkpoint before agent execution
            try:
                await self._checkpoint.auto_commit(
                    sandbox_id, message=f"before node [{node_id}]"
                )
            except Exception:
                pass

            # 4. Build agent command
            # React Flow nodes store type in top-level "type" and "data.agentType";
            # compiled node dicts may use "agent_type".  Try all.
            data = node.get("data", {})
            agent_type: str = (
                node.get("agent_type")
                or node.get("type")
                or (data.get("agentType") if isinstance(data, dict) else None)
                or "coder"
            )
            model_provider: str = (
                node.get("model_provider")
                or (data.get("modelProvider") if isinstance(data, dict) else "")
                or ""
            )
            model_id: str = (
                node.get("model_id")
                or (data.get("modelId") if isinstance(data, dict) else "")
                or ""
            )
            default_model_cfg = _load_default_model_config()
            if not model_provider:
                model_provider = default_model_cfg.get("provider", "")
            if not model_id:
                model_id = default_model_cfg.get("model", "")
            prompt: str = (
                node.get("prompt")
                or (data.get("prompt") if isinstance(data, dict) else "")
                or ""
            )

            if agent_type == "plan":
                prompt = prompt + PLAN_SYSTEM_SUFFIX

            # --- Human-in-the-Loop: pause execution and wait for approval ---
            if agent_type == "human":
                logger.info("HITL node %s: pausing run %s for human approval", node_id, run_id)

                # Emit paused event
                await self._emit("status", run_id, node_id, content="paused")
                await self._emit("node_paused", run_id, node_id, content="Awaiting human approval")

                # Update run status to paused in DB
                await self._update_run_status_db(run_id, "paused")

                # Register an asyncio.Event that the approve/reject API will set
                approval_event = asyncio.Event()
                from app.api.runs import set_approval_event, _approval_results, clear_approval
                set_approval_event(run_id, approval_event)

                # Wait for approval (with periodic cancel_event checks)
                while not approval_event.is_set() and not cancel_event.is_set():
                    try:
                        await asyncio.wait_for(approval_event.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    break

                # Check the result
                approval_info = _approval_results.get(run_id, {})
                approved = approval_info.get("approved", False)
                reason = approval_info.get("reason", "")

                # Clean up
                clear_approval(run_id)

                if not approved:
                    logger.info("HITL node %s: REJECTED (reason: %s)", node_id, reason)
                    await self._emit("node_rejected", run_id, node_id, content=reason)
                    state = "failed"
                    result: dict[str, Any] = {
                        "state": state,
                        "exit_code": 1,
                        "node_id": node_id,
                        "sandbox_id": sandbox_id,
                        "error": f"Human rejected: {reason}",
                    }
                    await self._emit("node_failed", run_id, node_id, content="Human rejected")
                    await self._emit("status", run_id, node_id, content=state)
                    return result

                logger.info("HITL node %s: APPROVED", node_id)
                await self._emit("node_approved", run_id, node_id, content="Human approved")

                # Human approved — continue execution. Human node
                # "completes" successfully, downstream nodes proceed.
                state = "completed"
                await self._emit("node_completed", run_id, node_id, content="Human approved")
                await self._emit("status", run_id, node_id, content=state)
                return {
                    "state": state,
                    "exit_code": 0,
                    "node_id": node_id,
                    "sandbox_id": sandbox_id,
                }

            # Inject escalation protocol for non-plan, non-human nodes (manual mode)
            if agent_type != "plan" and agent_type != "human":
                escalation_hint = (
                    f"\n\n---\nNode ID: {node_id}\n"
                    f"If you need help or are stuck, include this exact line in your output:\n"
                    f"ESCALATE: <your question or request>\n"
                    "This will be sent to your upstream node for guidance.\n"
                )
                prompt = prompt + escalation_hint

            # Append upstream context when provided (dual-mode data passing)
            if upstream_context:
                prompt = prompt + upstream_context

            # Resolve provider URL + API key from models.json
            from app.api.models import load_provider_config
            provider_cfg = load_provider_config().get(model_provider, {})
            provider_url = default_model_cfg.get("url", "") if default_model_cfg.get("provider") == model_provider else ""
            provider_key = default_model_cfg.get("key", "") if default_model_cfg.get("provider") == model_provider else ""
            provider_url = provider_url or provider_cfg.get("url", "")
            provider_key = provider_key or provider_cfg.get("key", "")

            # Write prompt to file to avoid shell argument length limits
            prompt_file = "/workspace/.agent/prompt.txt"
            await self._sandbox.write_file(sandbox_id, prompt_file, prompt)

            cmd = (
                f"mkdir -p /workspace/.agent /workspace/.workflow && "
                f"cd /workspace && python3 -m mas_agent "
                f"--provider {shlex.quote(model_provider)} "
                f"--model {shlex.quote(model_id)} "
                f"--agent-type {shlex.quote(agent_type)} "
                f"--run-id {shlex.quote(run_id)} "
                f"--node-id {shlex.quote(node_id)} "
                f"--prompt-file {shlex.quote(prompt_file)} "
                f"--stream-dir /workspace/.agent "
            )
            if provider_url:
                cmd += f"--provider-url {shlex.quote(provider_url)} "
            if provider_key:
                cmd += f"--provider-key {shlex.quote(provider_key)} "

            await self._emit("shell_stdout", run_id, node_id, content=f"$ {cmd}")

            # 5. Run agent asynchronously
            exec_id = await self._sandbox.exec_async(sandbox_id, cmd, env=subprocess_env)
            logger.info(
                "Started mas_agent exec %s in sandbox %s",
                exec_id[:12], sandbox_id[:12],
            )

            # 6. Poll until process completes
            log_pos = 0
            poll_count = 0
            while not cancel_event.is_set():
                # Read new stream content
                log_pos = await self._stream_log_lines(
                    sandbox_id, stream_file, log_pos, run_id, node_id,
                )

                # Check if the process is still running
                proc_info = await self._sandbox.get_process(exec_id)
                poll_count += 1
                if not proc_info.running:
                    logger.info(
                        "Process %s finished after %d polls (exit_code=%s)",
                        exec_id[:12], poll_count, proc_info.exit_code,
                    )
                    break

                await asyncio.sleep(1.0)

            # Final read to capture remaining output
            log_pos = await self._stream_log_lines(
                sandbox_id, stream_file, log_pos, run_id, node_id,
            )

            proc_info = await self._sandbox.get_process(exec_id)
            exit_code = proc_info.exit_code if proc_info.exit_code is not None else -1
            logger.info("Node %s exit_code=%d, log_pos=%d", node_id, exit_code, log_pos)
            state = "completed" if exit_code == 0 else "failed"

            await self._emit(
                "node_completed" if state == "completed" else "node_failed",
                run_id, node_id,
                content=f"exit_code={exit_code}",
            )
            await self._emit("status", run_id, node_id, content=state)

            result: dict[str, Any] = {
                "state": state,
                "exit_code": exit_code,
                "node_id": node_id,
                "exec_id": exec_id,
                "sandbox_id": sandbox_id,
            }

            # Capture stderr on failure for debugging
            if state == "failed":
                try:
                    shim = self._sandbox._find_process(exec_id)
                    if shim and shim._proc.stderr:
                        stderr_bytes = shim._proc.stderr.read()
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:2000]
                        if stderr_text.strip():
                            result["error"] = stderr_text.strip()
                            logger.error("Node %s stderr: %s", node_id, stderr_text[:500])
                except Exception:
                    pass

            # Plan nodes: capture raw output for child-task parsing
            if agent_type == "plan" and state == "completed":
                try:
                    raw_log, _ = await self._sandbox.exec(
                        sandbox_id,
                        f"cat {stream_file} 2>/dev/null || true",
                        env=subprocess_env,
                    )
                    result["raw_output"] = raw_log
                except Exception as exc:
                    logger.warning("Failed to read raw_output for plan node %s: %s", node_id, exc)

            return result

        finally:
            # Sync sandbox changes back to the workspace directory if configured
            if workspace_directory:
                try:
                    await self._sandbox.sync_back(sandbox_id, workspace_directory)
                except Exception:
                    logger.warning(
                        "sync_back failed for sandbox %s -> %s",
                        sandbox_id[:12], workspace_directory, exc_info=True,
                    )

            # Only destroy the sandbox if we created it and the DAG layer
            # executor does not need to reuse it for downstream nodes.
            if _owns_sandbox and destroy_owned_sandbox:
                try:
                    await self._sandbox.destroy(sandbox_id)
                except Exception:
                    pass

    async def _stream_log_lines(
        self,
        sandbox_id: str,
        stream_file: str,
        start_pos: int,
        run_id: str,
        node_id: str,
    ) -> int:
        """Read new lines from the agent's stream.jsonl and emit events.

        Returns the new file position after reading.
        """
        try:
            log_content, _ = await self._sandbox.exec(
                sandbox_id,
                f"cat {stream_file} 2>/dev/null || true",
            )
        except Exception as exc:
            logger.warning("_stream_log_lines exec failed: %s", exc)
            return start_pos

        if len(log_content) <= start_pos:
            if start_pos == 0:
                logger.debug("_stream_log_lines: file empty (len=%d)", len(log_content))
            return start_pos

        logger.info("_stream_log_lines: read %d new bytes (pos %d→%d)", len(log_content) - start_pos, start_pos, len(log_content))

        new_content = log_content[start_pos:]
        for line in new_content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                event_type = ev.get("type", "")
                if event_type in _KNOWN_EVENT_TYPES:
                    await self._emit(
                        event_type, run_id, node_id,
                        content=ev.get("content", ""),
                        tool_name=ev.get("tool_name", ""),
                        timestamp=ev.get("timestamp", 0),
                    )
                elif event_type == "text":
                    # Backward compat: some agents emit "text" for LLM tokens
                    await self._emit(
                        "llm_token", run_id, node_id,
                        content=ev.get("content", ""),
                    )
                elif event_type:
                    content = ev.get("content", "")
                    if content:
                        await self._emit(event_type, run_id, node_id, content=content)
            except json.JSONDecodeError:
                # Non-JSON line (stdout pollution) -- treat as plain text
                # Check for escalation markers in the streamed content
                if "ESCALATE:" in line:
                    escalation_msg = line.split("ESCALATE:", 1)[1].strip()
                    await self._emit(
                        "worker_escalation", run_id, node_id, content=escalation_msg,
                    )
                    # In manual mode, route the escalation to the upstream node
                    # based on the edges defined in the workflow.
                    await self._route_escalation(run_id, node_id, escalation_msg)
                await self._emit("shell_stdout", run_id, node_id, content=line)

        return len(log_content)

    async def _execute_dynamic_plan(
        self,
        run_id: str,
        parent_node_id: str,
        parent_result: dict,
        global_config: dict,
        cancel_event: asyncio.Event,
        planner_node: dict | None = None,
    ) -> dict[str, Any]:
        """After a planner node completes, parse its output for child tasks,
        persist them as Task rows, and execute them.

        In auto mode with structured DAG output (from ``parse_plan_to_dag``),
        the child edges are used to compile the sub-DAG into layers so that
        independent tasks run in parallel.  Otherwise, tasks are executed
        sequentially via the TaskScheduler (legacy behaviour).

        Returns a dict mapping child node_id -> result.
        """
        raw_output = parent_result.get("raw_output", "")
        if not raw_output:
            logger.warning("_execute_dynamic_plan: no raw_output for parent %s", parent_node_id)
            return {}

        # Extract LLM text from stream.jsonl -- the parser needs plain text,
        # not raw JSONL event lines
        extracted_text = self._extract_llm_text(raw_output)
        logger.info(
            "_execute_dynamic_plan: extracted %d chars of LLM text from %d chars of raw output",
            len(extracted_text), len(raw_output),
        )

        # In auto mode, try structured DAG parsing first
        auto_mode = global_config.get("_mode") == "auto"
        dag_result = None
        parsed_tasks: list[dict] = []

        if auto_mode:
            dag_result = parse_plan_to_dag(extracted_text)
            if dag_result:
                child_nodes, child_edges = dag_result
                logger.info(
                    "_execute_dynamic_plan (auto): parse_plan_to_dag returned %d nodes, %d edges",
                    len(child_nodes), len(child_edges),
                )
                # Convert DAG nodes to the flat task format for DB persistence.
                # Each node becomes a dict with at least type, prompt, and an
                # optional model field.
                for dag_node in child_nodes:
                    nd = dag_node.get("data", {})
                    task_entry: dict = {
                        "node_id": dag_node.get("id", ""),
                        "title": nd.get("label") or dag_node.get("id", ""),
                        "type": nd.get("agent_type") or dag_node.get("type", "coder"),
                        "prompt": nd.get("prompt", ""),
                    }
                    if nd.get("model"):
                        task_entry["model"] = nd["model"]
                    parsed_tasks.append(task_entry)
            else:
                logger.info(
                    "_execute_dynamic_plan (auto): parse_plan_to_dag returned None, "
                    "falling back to parse_plan_output",
                )

        # If not auto mode, or auto mode fell back, use the legacy parser
        if not parsed_tasks:
            parsed_tasks = parse_plan_output(extracted_text)

        if not parsed_tasks:
            logger.info(
                "Plan node %s produced no child tasks", parent_node_id,
            )
            return {}

        logger.info(
            "Plan node %s produced %d child tasks", parent_node_id, len(parsed_tasks),
        )

        # Persist tasks to DB and emit events
        from app.core.database import async_session_factory
        from app.models.task import Task as TaskModel
        from uuid import uuid4

        # Build worker node dicts and persist to DB in one pass
        worker_nodes: list[dict] = []
        task_db_map: dict[str, tuple] = {}  # node_id -> (task_id, session_managed)

        async with async_session_factory() as session:
            for idx, parsed in enumerate(parsed_tasks):
                child_node_id = parsed.get("node_id") or f"{parent_node_id}_child_{idx}"

                # Create DB task
                task_id = uuid4()
                task_type = parsed.get("type", "coder")
                worker_label = parsed.get("title") or f"{task_type} #{idx + 1}"
                db_task = TaskModel(
                    id=task_id,
                    run_id=uuid.UUID(run_id),
                    title=(parsed.get("title") or parsed.get("prompt", ""))[:200],
                    description=parsed.get("prompt", ""),
                    status="pending",
                    assigned_node_id=child_node_id,
                    assigned_worker_label=worker_label,
                )
                session.add(db_task)
                await session.commit()

                # Emit task_created event
                await self._emit(
                    "task_created", run_id, parent_node_id,
                    task_id=str(task_id),
                    task_title=db_task.title,
                    task_description=db_task.description,
                    status="pending",
                    child_node_id=child_node_id,
                )

                # Also emit child_created for backward compat (frontend canvas)
                await self._emit(
                    "child_created", run_id, parent_node_id,
                    child_node_id=child_node_id,
                    child_type=parsed.get("type", "coder"),
                    child_prompt=parsed.get("prompt", ""),
                    child_model=parsed.get("model", ""),
                )

                # Build the worker node dict
                model_str = parsed.get("model", "")
                model_parts = model_str.split("/", 1) if model_str else ("", "")
                model_provider = model_parts[0] if len(model_parts) > 1 else ""
                model_id = model_parts[1] if len(model_parts) > 1 else model_str

                # Inherit planner's model config as fallback when task doesn't specify one
                if not model_provider or not model_id:
                    p_data = planner_node.get("data", {}) if planner_node else {}
                    fallback_provider = (
                        planner_node.get("model_provider", "")
                        if planner_node else ""
                    ) or (p_data.get("modelProvider", "") if isinstance(p_data, dict) else "")
                    fallback_model = (
                        planner_node.get("model_id", "")
                        if planner_node else ""
                    ) or (p_data.get("modelId", "") if isinstance(p_data, dict) else "")
                    if not model_provider and fallback_provider:
                        model_provider = fallback_provider
                    if not model_id and fallback_model:
                        model_id = fallback_model

                # Inject escalation protocol into worker prompt
                worker_prompt = parsed.get("prompt", "")
                worker_prompt += (
                    f"\n\n---\nTask ID: {child_node_id}\n"
                    f"When you need help or are stuck, output a line:\n"
                    f"{ESCALATION_MARKER} <your question>\n"
                    f"To report progress, output a line:\n"
                    f"{PROGRESS_MARKER} <0-100>\n"
                )

                worker_node = {
                    "id": child_node_id,
                    "agent_type": parsed.get("type", "coder"),
                    "model_provider": model_provider,
                    "model_id": model_id,
                    "prompt": worker_prompt,
                }
                worker_nodes.append(worker_node)
                task_db_map[child_node_id] = (str(task_id), worker_label)

            # ---------- Execute child tasks ----------

            if auto_mode and dag_result is not None:
                # --- DAG-based execution: honour edges from the planner ---
                child_nodes_dag, child_edges_dag = dag_result

                # Replace auto-generated node IDs with the worker node dicts
                # that include model config and escalation prompts.
                worker_by_id = {wn["id"]: wn for wn in worker_nodes}

                # Build execution nodes: use the worker_node as the base (it
                # has the correct model config + escalation prompt) but
                # preserve the original dag_node id.
                exec_nodes: list[dict] = []
                for dag_node in child_nodes_dag:
                    dag_id = dag_node.get("id", "")
                    base = worker_by_id.get(dag_id, dag_node)
                    # Start from the worker_node (has model config + prompt)
                    # and overlay the dag_node id to ensure consistency.
                    merged = dict(base)
                    merged["id"] = dag_id
                    exec_nodes.append(merged)

                # Compile the sub-DAG into topologically-sorted layers
                sub_dag = {"nodes": exec_nodes, "edges": child_edges_dag}
                try:
                    sub_layers = compile_dag(sub_dag)
                except ValueError as exc:
                    logger.warning(
                        "_execute_dynamic_plan: sub-DAG compile failed (%s), "
                        "falling back to sequential execution",
                        exc,
                    )
                    sub_layers = [[n] for n in exec_nodes]

                logger.info(
                    "_execute_dynamic_plan (auto): compiled sub-DAG into %d layers for %d nodes",
                    len(sub_layers), len(exec_nodes),
                )
                for li, layer in enumerate(sub_layers):
                    logger.info(
                        "_execute_dynamic_plan (auto): layer %d = %s",
                        li, [n.get("id", "?") for n in layer],
                    )

                # Execute the sub-DAG via _execute_layers
                child_results = await self._execute_layers(
                    run_id, sub_layers, child_edges_dag, global_config, cancel_event,
                    workspace_directory=None,
                )

                # Update DB task statuses based on results
                for node_id, result in child_results.items():
                    if node_id not in task_db_map:
                        continue
                    task_id_str, _label = task_db_map[node_id]
                    state = result.get("state", "failed") if isinstance(result, dict) else "failed"
                    try:
                        from app.models.task import Task as TaskModel
                        from sqlalchemy import select
                        from uuid import UUID as _UUID
                        db_result = await session.execute(
                            select(TaskModel).where(TaskModel.id == _UUID(task_id_str))
                        )
                        row = db_result.scalar_one_or_none()
                        if row:
                            row.status = state
                            if state == "completed":
                                raw = result.get("raw_output", "")
                                summary = self._task_scheduler._summarize(raw) if raw else ""
                                row.result_summary = summary or f"state={state}"
                            else:
                                row.result_summary = result.get("error", "execution failed")
                            await session.commit()
                    except Exception:
                        logger.warning(
                            "Failed to update DB task %s status", task_id_str, exc_info=True,
                        )

                    await self._emit(
                        "child_completed", run_id, parent_node_id,
                        child_node_id=node_id,
                        content=f"state={state}",
                    )
                    await self._emit(
                        "task_updated", run_id, "",
                        task_id=task_id_str,
                        status=state,
                        progress=100 if state == "completed" else 0,
                        result_summary=result.get("error", "") if state != "completed" else "",
                    )

                return child_results

            else:
                # --- Legacy sequential execution via TaskScheduler ---
                managed_tasks: list = []

                for worker_node in worker_nodes:
                    child_node_id = worker_node["id"]
                    if child_node_id not in task_db_map:
                        continue
                    task_id_str, _label = task_db_map[child_node_id]

                    from app.core.task_scheduler import ManagedTask
                    managed = ManagedTask(
                        db_id=task_id_str,
                        run_id=run_id,
                        title=worker_node.get("prompt", "")[:200],
                        description=worker_node.get("prompt", ""),
                        status="pending",
                        assigned_node_id=child_node_id,
                    )
                    managed_tasks.append((managed, worker_node))

                child_results: dict[str, Any] = {}

                # Run all tasks via the scheduler (with escalation support)
                for managed, worker_node in managed_tasks:
                    managed.worker_task = asyncio.create_task(
                        self._task_scheduler.run_worker_task(
                            worker_node=worker_node,
                            task=managed,
                            global_config=global_config,
                            cancel_event=cancel_event,
                            db_session=session,
                            planner_node=planner_node,
                        ),
                        name=f"worker-{managed.assigned_node_id}",
                    )

                # Wait for all workers to complete
                for managed, worker_node in managed_tasks:
                    try:
                        result = await asyncio.wait_for(
                            managed.worker_task, timeout=300,
                        )
                    except asyncio.TimeoutError:
                        result = {"state": "failed", "error": "timeout"}
                        await self._task_scheduler.update_task_status(
                            managed, "failed", session, result_summary="timeout",
                        )
                    except Exception as exc:
                        result = {"state": "failed", "error": str(exc)}
                        await self._task_scheduler.update_task_status(
                            managed, "failed", session, result_summary=str(exc),
                        )

                    child_node_id = managed.assigned_node_id or ""
                    child_results[child_node_id] = result

                    await self._emit(
                        "child_completed", run_id, parent_node_id,
                        child_node_id=child_node_id,
                        content=f"state={result.get('state', 'unknown')}",
                    )

                return child_results
