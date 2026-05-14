"""Local asyncio-based DAG executor.

Replaces Temporal.io workflow engine with a pure-asyncio implementation that
executes DAG layers sequentially, nodes within each layer in parallel.

Orchestration flow for each node:
  1. Create sandbox container
  2. Provision workspace directories + Git init
  3. Git checkpoint (auto-commit before agent runs)
  4. Build and launch the opencode source runner
  5. Poll process status, streaming events from stream.jsonl
  6. On completion: emit node_completed/node_failed, destroy sandbox
  7. Plan nodes: parse output and execute dynamic child tasks
"""

import asyncio
import json
import logging
import os
import shlex
import time
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.task_scheduler import TaskScheduler, ESCALATION_MARKER, PROGRESS_MARKER
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner
from app.workflows.compiler import compile_dag
from app.workflows.plan_parser import parse_plan_output, parse_plan_to_dag

logger = logging.getLogger(__name__)

# Event types that the agent writes to stream.jsonl in the correct format
_KNOWN_EVENT_TYPES = frozenset({
    "llm_token", "llm_chunk", "tool_call", "tool_result", "shell_stdout",
    "shell_stderr", "status", "error", "node_started", "node_completed",
    "node_failed", "child_created", "child_completed",
    "task_created", "task_updated", "task_message", "worker_escalation",
    "artifact_created", "worker_message", "planner_guidance",
    "task_blocked", "task_unblocked",
})

# Resolve repository-local runner/source paths.
# __file__ = .../apps/orchestrator/app/core/local_engine.py
# parents[0] = .../apps/orchestrator/app/core
# parents[1] = .../apps/orchestrator/app
# parents[2] = .../apps/orchestrator
# parents[3] = .../apps
# Project root = parents[4] = .../mas
_REPO_ROOT = Path(__file__).resolve().parents[4]
_OPENCODE_RUNNER = _REPO_ROOT / "apps" / "opencode-runner" / "run-node.ts"
_OPENCODE_PACKAGE_DIR = Path(
    os.environ.get(
        "MAS_OPENCODE_PACKAGE_DIR",
        str(_REPO_ROOT / "apps" / "opencode-runner" / "vendor" / "opencode" / "packages" / "opencode"),
    )
)
_OPENCODE_SOURCE_ENTRY = Path(
    os.environ.get(
        "MAS_OPENCODE_SOURCE_ENTRY",
        str(_OPENCODE_PACKAGE_DIR / "src" / "index.ts"),
    )
)


def _build_subprocess_env() -> dict[str, str]:
    """Build environment for node subprocesses."""
    env = dict(os.environ)
    env["OPENCODE_PACKAGE_DIR"] = str(_OPENCODE_PACKAGE_DIR)
    env["OPENCODE_SOURCE_ENTRY"] = str(_OPENCODE_SOURCE_ENTRY)
    env.setdefault("OPENCODE_DISABLE_MODELS_FETCH", "1")
    return env


def _load_settings_models() -> list[dict[str, Any]]:
    """Load UI model entries from settings.json."""
    settings_path = Path(__file__).resolve().parents[3] / "data" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    models = data.get("models", [])
    if not isinstance(models, list) or not models:
        return []
    return [m for m in models if isinstance(m, dict)]


def _normalize_model_config(entry: dict[str, Any]) -> dict[str, str | int]:
    return {
        "provider": str(entry.get("format") or ""),
        "model": str(entry.get("default_model") or entry.get("name") or ""),
        "url": str(entry.get("base_url") or "").rstrip("/"),
        "key": str(entry.get("api_key") or ""),
        "context_window": int(entry.get("context_window") or 128000),
        "max_output_tokens": int(entry.get("max_output_tokens") or 4096),
    }


def _load_default_model_config() -> dict[str, str | int]:
    """Load the first configured UI model for agent execution fallback."""
    models = _load_settings_models()
    if not models:
        return {}

    first = models[0]
    return _normalize_model_config(first)


def _load_model_config(model_provider: str, model_id: str) -> dict[str, str | int]:
    """Load the configured model matching provider/model, with first model fallback."""
    models = _load_settings_models()
    if not models:
        return {}

    for entry in models:
        provider = str(entry.get("format") or "")
        configured_model = str(entry.get("default_model") or entry.get("name") or "")
        if provider == model_provider and configured_model == model_id:
            return _normalize_model_config(entry)

    for entry in models:
        if str(entry.get("format") or "") == model_provider:
            return _normalize_model_config(entry)

    if not model_provider and not model_id:
        return _normalize_model_config(models[0])
    return {}


def _parse_full_model_id(full_id: str) -> tuple[str, str]:
    if not full_id:
        return "", ""
    parts = full_id.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", full_id


def _resolve_auto_child_model(
    auto_child_model_map: dict[str, Any] | None,
    agent_type: str,
) -> tuple[str, str]:
    if not isinstance(auto_child_model_map, dict):
        return "", ""
    raw = str(auto_child_model_map.get(agent_type) or "")
    return _parse_full_model_id(raw)


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
        self._runs[run_id] = {
            "status": "running",
            "task": None,
            "cancel_event": cancel_event,
            "global_config": global_config or {},
            "workspace_directory": workspace_directory,
            "kind": "workflow",
            "layers": layers,
        }
        await self._write_mas_run_manifest(
            run_id=run_id,
            workspace_directory=workspace_directory,
            kind="workflow",
            status="running",
            payload={
                "layers": layers,
                "global_config": global_config or {},
            },
        )
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

        self._runs[run_id]["task"] = task
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
        self._runs[run_id] = {
            "status": "running",
            "task": None,
            "cancel_event": cancel_event,
            "global_config": global_config or {},
            "workspace_directory": workspace_directory,
            "kind": "task_dag",
            "dag_json": dag_json,
        }
        await self._write_mas_run_manifest(
            run_id=run_id,
            workspace_directory=workspace_directory,
            kind="task_dag",
            status="running",
            payload={
                "dag_json": dag_json,
                "global_config": global_config or {},
            },
        )
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

        self._runs[run_id]["task"] = task
        logger.info(
            "Task DAG created for run %s with %d nodes",
            run_id, len(dag_json.get("nodes", [])),
        )
        return run_id

    async def recover_interrupted_runs(self) -> None:
        """Resume DB runs that were left running when the backend stopped.

        We cannot resume a killed Python coroutine or an in-flight LLM request.
        Instead, we restore the run DAG from the workflow/.mas manifest, keep
        completed tasks as completed, reset stale running tasks to pending, and
        re-execute only unfinished nodes.
        """
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel, Workflow
            from app.models.task import Task as TaskModel
            from sqlalchemy import select

            async with async_session_factory() as session:
                result = await session.execute(
                    select(RunModel, Workflow)
                    .join(Workflow, RunModel.workflow_id == Workflow.id)
                    .where(RunModel.status.in_(("running", "pending", "cancelling")))
                )
                rows = result.all()
                for run, workflow in rows:
                    task_result = await session.execute(
                        select(TaskModel).where(TaskModel.run_id == run.id)
                    )
                    for task in task_result.scalars().all():
                        if task.status in ("running", "blocked"):
                            task.status = "pending"
                    run.status = "running"
                await session.commit()

            for run, workflow in rows:
                dag_json = await self._load_mas_dag_json(str(run.id), workflow.workspace_directory)
                if not dag_json:
                    dag_json = workflow.dag_json or {}
                if not dag_json.get("nodes"):
                    logger.warning("Cannot recover run %s: no DAG JSON", run.id)
                    await self._update_run_status_db(str(run.id), "failed")
                    continue
                await self.resume_task_dag(
                    run_id=str(run.id),
                    dag_json=dag_json,
                    global_config={
                        "_mode": "auto",
                        "_goal": workflow.goal or "",
                        "_workflow_id": str(workflow.id),
                        "_edges": dag_json.get("edges", []),
                        "_recovered": True,
                    },
                    workspace_directory=workflow.workspace_directory,
                )
        except Exception:
            logger.warning("Failed to recover interrupted runs", exc_info=True)

    async def resume_task_dag(
        self,
        run_id: str,
        dag_json: dict,
        global_config: dict | None = None,
        workspace_directory: str | None = None,
    ) -> str:
        """Resume an existing task DAG without recreating completed tasks."""
        cancel_event = asyncio.Event()
        self._runs[run_id] = {
            "status": "running",
            "task": None,
            "cancel_event": cancel_event,
            "global_config": global_config or {},
            "workspace_directory": workspace_directory,
            "kind": "task_dag",
            "dag_json": dag_json,
            "recovered": True,
        }
        await self._write_mas_run_manifest(
            run_id=run_id,
            workspace_directory=workspace_directory,
            kind="task_dag",
            status="running",
            payload={
                "dag_json": dag_json,
                "global_config": global_config or {},
                "recovered": True,
            },
        )
        task = asyncio.create_task(
            self._resume_task_dag(
                run_id, dag_json, global_config or {}, cancel_event,
                workspace_directory=workspace_directory,
            ),
            name=f"resume-task-dag-{run_id}",
        )
        task.add_done_callback(lambda t: logger.exception("Recovered run failed for %s", run_id) if (not t.cancelled() and t.exception()) else None)
        self._runs[run_id]["task"] = task
        logger.info("Recovered task DAG created for run %s", run_id)
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
        """Persist and publish an event to the in-process event bus."""
        event = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "run_id": run_id,
            "node_id": node_id,
            "timestamp": time.time(),
            **extra,
        }
        await self._persist_run_event(event)
        await self._append_mas_event(run_id, event)
        channel = f"run:{run_id}:stream"
        try:
            await self._event_bus.publish(channel, event)
        except Exception:
            logger.warning("Failed to publish event %s", event_type, exc_info=True)

    async def _persist_run_event(self, event: dict[str, Any]) -> None:
        """Store a stream event so UI panels can be restored after reconnect."""
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run, RunEvent

            run_id = uuid.UUID(str(event.get("run_id", "")))
            node_id = str(event.get("node_id") or "")
            async with async_session_factory() as session:
                run = await session.get(Run, run_id)
                if run is None:
                    run_state = self._runs.get(str(run_id), {})
                    workflow_id_raw = (
                        (run_state.get("global_config") or {}).get("_workflow_id")
                        or event.get("workflow_id")
                    )
                    if workflow_id_raw:
                        session.add(Run(
                            id=run_id,
                            workflow_id=uuid.UUID(str(workflow_id_raw)),
                            status=str(run_state.get("status") or "running"),
                            engine_workflow_id=str(run_id),
                        ))
                session.add(RunEvent(
                    run_id=run_id,
                    event_type=str(event.get("type") or ""),
                    node_id=node_id,
                    payload=event,
                ))
                await session.commit()
        except Exception:
            logger.warning("Failed to persist run event %s", event.get("type"), exc_info=True)

    def _mas_run_dir(self, workspace_directory: str | None, run_id: str) -> Path | None:
        if not workspace_directory:
            return None
        try:
            workspace = Path(workspace_directory).expanduser().resolve()
            return workspace / ".mas" / "runs" / run_id
        except Exception:
            return None

    def _mas_node_output_dir(
        self,
        workspace_directory: str | None,
        run_id: str,
        node_id: str,
    ) -> Path | None:
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return None
        return run_dir / "node-output" / node_id

    def _mas_integration_dir(
        self,
        workspace_directory: str | None,
        run_id: str,
    ) -> Path | None:
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return None
        return run_dir / "integration"

    def _mas_sandbox_root(self, workspace_directory: str | None, run_id: str) -> Path | None:
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return None
        return run_dir / "sandboxes"

    async def _write_mas_json(self, workspace_directory: str | None, run_id: str, relative: str, data: dict) -> None:
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return

        def _write() -> None:
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        try:
            await asyncio.to_thread(_write)
        except Exception:
            logger.warning("Failed to write .mas state %s for run %s", relative, run_id, exc_info=True)

    async def _write_mas_text(
        self,
        workspace_directory: str | None,
        run_id: str,
        relative: str,
        content: str,
    ) -> None:
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return

        def _write() -> None:
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        try:
            await asyncio.to_thread(_write)
        except Exception:
            logger.warning("Failed to write .mas text %s for run %s", relative, run_id, exc_info=True)

    @staticmethod
    def _node_agent_type(node: dict[str, Any]) -> str:
        data = node.get("data", {})
        agent_type = (
            node.get("agent_type")
            or node.get("type")
            or (data.get("agentType") if isinstance(data, dict) else None)
            or "coder"
        )
        node_id = str(node.get("id") or node.get("node_id") or "")
        if agent_type == "plan" and node_id != "planner":
            return "design"
        return str(agent_type)

    @staticmethod
    def _upstream_ids_for_node(node_id: str, edges: list[dict[str, Any]]) -> list[str]:
        return [
            str(edge.get("source"))
            for edge in edges
            if edge.get("target") == node_id and edge.get("source")
        ]

    async def _write_mas_run_manifest(
        self,
        run_id: str,
        workspace_directory: str | None,
        kind: str,
        status: str,
        payload: dict,
    ) -> None:
        await self._write_mas_json(
            workspace_directory,
            run_id,
            "run.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "kind": kind,
                "status": status,
                "workspace_directory": workspace_directory or "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            },
        )

    async def _load_mas_dag_json(self, run_id: str, workspace_directory: str | None) -> dict | None:
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return None

        def _read() -> dict | None:
            path = run_dir / "run.json"
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            dag = data.get("dag_json")
            return dag if isinstance(dag, dict) else None

        return await asyncio.to_thread(_read)

    async def _append_mas_event(self, run_id: str, event: dict[str, Any]) -> None:
        run_state = self._runs.get(run_id, {})
        workspace_directory = run_state.get("workspace_directory")
        run_dir = self._mas_run_dir(workspace_directory, run_id)
        if run_dir is None:
            return

        def _append() -> None:
            path = run_dir / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

        try:
            await asyncio.to_thread(_append)
        except Exception:
            logger.warning("Failed to append .mas event for run %s", run_id, exc_info=True)

    async def _write_mas_node_state(
        self,
        run_id: str,
        node_id: str,
        status: str,
        node: dict | None = None,
        extra: dict | None = None,
    ) -> None:
        run_state = self._runs.get(run_id, {})
        workspace_directory = run_state.get("workspace_directory")
        data = {
            "schema_version": 1,
            "run_id": run_id,
            "node_id": node_id,
            "status": status,
            "node": node or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            data.update(extra)
        await self._write_mas_json(workspace_directory, run_id, f"nodes/{node_id}.json", data)
        await self._write_mas_json(workspace_directory, run_id, f"node-output/{node_id}/state.json", data)

    async def _prepare_node_output_workspace(
        self,
        run_id: str,
        node_id: str,
        workspace_directory: str | None,
        sandbox_id: str,
    ) -> None:
        node_dir = self._mas_node_output_dir(workspace_directory, run_id, node_id)
        if node_dir is None:
            return

        sandbox_workspace = self._sandbox.get_workspace_path(sandbox_id)

        def _prepare() -> None:
            node_dir.mkdir(parents=True, exist_ok=True)
            workspace_link = node_dir / "workspace"
            if workspace_link.is_symlink() or workspace_link.exists():
                if workspace_link.is_symlink() or workspace_link.is_file():
                    workspace_link.unlink(missing_ok=True)
                else:
                    shutil.rmtree(workspace_link, ignore_errors=True)
            try:
                workspace_link.symlink_to(sandbox_workspace, target_is_directory=True)
                (node_dir / "workspace.live").write_text(
                    f"{sandbox_workspace}\n",
                    encoding="utf-8",
                )
            except OSError:
                (node_dir / "workspace.live").write_text(
                    f"{sandbox_workspace}\n",
                    encoding="utf-8",
                )

        try:
            await asyncio.to_thread(_prepare)
        except Exception:
            logger.warning("Failed to prepare node output workspace for %s", node_id, exc_info=True)

    async def _snapshot_workspace_to_dir(self, sandbox_id: str, target_dir: Path) -> None:
        sandbox_workspace = self._sandbox.get_workspace_path(sandbox_id)

        def _copy() -> None:
            skip_dirs = {".git", ".agent", ".workflow", ".mas", "sandbox-meta"}
            if target_dir.exists():
                if target_dir.is_symlink() or target_dir.is_file():
                    target_dir.unlink(missing_ok=True)
                else:
                    shutil.rmtree(target_dir, ignore_errors=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            for item in sandbox_workspace.iterdir():
                if item.name in skip_dirs:
                    continue
                dest = target_dir / item.name
                try:
                    if dest.exists() and item.samefile(dest):
                        continue
                except OSError:
                    pass
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)

        await asyncio.to_thread(_copy)

    async def _finalize_node_output_workspace(
        self,
        run_id: str,
        node_id: str,
        workspace_directory: str | None,
        sandbox_id: str,
    ) -> None:
        node_dir = self._mas_node_output_dir(workspace_directory, run_id, node_id)
        if node_dir is None:
            return
        try:
            await self._snapshot_workspace_to_dir(sandbox_id, node_dir / "workspace")
        except Exception:
            logger.warning("Failed to finalize node workspace snapshot for %s", node_id, exc_info=True)

    async def _write_integration_workspace_snapshot(
        self,
        run_id: str,
        workspace_directory: str | None,
        sandbox_id: str,
    ) -> None:
        integration_dir = self._mas_integration_dir(workspace_directory, run_id)
        if integration_dir is None:
            return
        try:
            await self._snapshot_workspace_to_dir(sandbox_id, integration_dir / "workspace")
        except Exception:
            logger.warning("Failed to write integration workspace snapshot for run %s", run_id, exc_info=True)

    def _result_summary_text(self, result: dict[str, Any]) -> str:
        raw = result.get("raw_output", "") if isinstance(result, dict) else ""
        summary = self._task_scheduler._summarize(raw, max_len=2400) if raw else ""
        if raw and not summary:
            summary = raw.strip()[:2400]
        if not summary:
            summary = str(result.get("error", "")).strip()
        if not summary:
            summary = f"Task finished with state={result.get('state', 'unknown')}"
        return summary.strip()

    def _node_report_title(self, agent_type: str) -> str:
        if agent_type == "explore":
            return "调查报告"
        if agent_type == "review":
            return "审查报告"
        if agent_type == "merge":
            return "合并报告"
        if agent_type == "shell":
            return "执行报告"
        if agent_type in {"plan", "design"}:
            return "规划结果"
        return "变更摘要"

    async def _build_commit_patch(
        self,
        sandbox_id: str,
        commit_hash: str,
    ) -> str:
        parent_hash = await self._parent_commit_hash(sandbox_id, commit_hash)
        if not parent_hash:
            return ""
        patch_stdout, _ = await self._sandbox.exec(
            sandbox_id,
            (
                'git --git-dir="/sandbox-meta/.git" --work-tree="/workspace" '
                f"diff {shlex.quote(parent_hash)} {shlex.quote(commit_hash)} "
                "-- . ':(exclude).agent/**' ':(exclude).workflow/**' ':(exclude).mas/**'"
            ),
        )
        return patch_stdout

    async def _parent_commit_hash(
        self,
        sandbox_id: str,
        commit_hash: str,
    ) -> str:
        parent_stdout, _ = await self._sandbox.exec(
            sandbox_id,
            f'git --git-dir="/sandbox-meta/.git" --work-tree="/workspace" rev-parse {shlex.quote(commit_hash)}^',
        )
        return parent_stdout.strip()

    async def _write_node_git_artifacts(
        self,
        run_id: str,
        node_id: str,
        workspace_directory: str | None,
        sandbox_id: str,
        commit_hash: str,
    ) -> None:
        parent_hash = await self._parent_commit_hash(sandbox_id, commit_hash)
        patch_text = await self._build_commit_patch(sandbox_id, commit_hash)
        commit_payload = {
            "node_id": node_id,
            "commit": commit_hash,
            "parent_commit": parent_hash,
        }
        await self._write_mas_json(
            workspace_directory,
            run_id,
            f"node-output/{node_id}/commit.json",
            commit_payload,
        )
        if patch_text.strip():
            await self._write_mas_text(
                workspace_directory,
                run_id,
                f"node-output/{node_id}/patch.diff",
                patch_text,
            )

    async def _prepare_merge_sandbox(
        self,
        run_id: str,
        node_id: str,
        workspace_directory: str | None,
        upstream_ids: list[str],
        sandbox_map: dict[str, str],
        commit_map: dict[str, str],
        layer_results: dict[str, Any],
    ) -> tuple[str | None, str]:
        notes: list[str] = []
        manifest: dict[str, Any] = {
            "node_id": node_id,
            "run_id": run_id,
            "primary_upstream": "",
            "askable_nodes": list(upstream_ids),
            "entries": [],
        }
        available_upstreams = [uid for uid in upstream_ids if sandbox_map.get(uid)]
        if not available_upstreams:
            return None, ""

        primary_upstream = available_upstreams[-1]
        primary_sandbox = sandbox_map.get(primary_upstream)
        if not primary_sandbox:
            return None, ""

        merge_sandbox_id: str | None = None
        try:
            merge_sandbox_id = await self._sandbox.clone(
                primary_sandbox,
                f"ws-{node_id}-{uuid4().hex[:8]}",
            )
            notes.append(f"- 已使用 `{primary_upstream}` 作为集成基线工作区。")
            manifest["primary_upstream"] = primary_upstream
        except Exception:
            logger.warning("Failed to clone primary upstream sandbox for merge node %s", node_id, exc_info=True)
            return None, ""

        patch_dir = f"/workspace/.agent/merge-patches/{node_id}"
        await self._sandbox.exec(merge_sandbox_id, f"mkdir -p {patch_dir}")
        upstream_summary = layer_results.get(primary_upstream, {}) if isinstance(layer_results.get(primary_upstream), dict) else {}
        manifest["entries"].append({
            "node_id": primary_upstream,
            "status": "base",
            "commit": commit_map.get(primary_upstream, ""),
            "patch_path": "",
            "error_path": "",
            "summary": upstream_summary.get("result_summary", "") or "",
        })

        for upstream_id in available_upstreams[:-1]:
            upstream_sandbox = sandbox_map.get(upstream_id)
            commit_hash = commit_map.get(upstream_id, "")
            upstream_result = layer_results.get(upstream_id, {}) if isinstance(layer_results.get(upstream_id), dict) else {}
            entry = {
                "node_id": upstream_id,
                "status": "skipped",
                "commit": commit_hash,
                "patch_path": "",
                "error_path": "",
                "summary": upstream_result.get("result_summary", "") or "",
            }
            if not upstream_sandbox or not commit_hash:
                notes.append(f"- `{upstream_id}` 缺少可用提交信息，未自动并入。")
                entry["status"] = "missing_commit"
                manifest["entries"].append(entry)
                continue

            patch_text = await self._build_commit_patch(upstream_sandbox, commit_hash)
            if not patch_text.strip():
                notes.append(f"- `{upstream_id}` 没有可应用的增量 patch。")
                entry["status"] = "no_patch"
                manifest["entries"].append(entry)
                continue

            patch_path = f"{patch_dir}/{upstream_id}.patch"
            await self._sandbox.write_file(merge_sandbox_id, patch_path, patch_text)
            entry["patch_path"] = patch_path
            apply_stdout, apply_err = await self._sandbox.exec(
                merge_sandbox_id,
                (
                    'git --git-dir="/sandbox-meta/.git" --work-tree="/workspace" '
                    f"apply --3way {shlex.quote(patch_path)}; "
                    'printf "\\n__MAS_RC:%s" "$?"'
                ),
            )
            rc = 1
            rc_marker = "__MAS_RC:"
            if rc_marker in apply_stdout:
                _, _, rc_text = apply_stdout.rpartition(rc_marker)
                try:
                    rc = int(rc_text.strip() or "1")
                except ValueError:
                    rc = 1
            clean_stdout = apply_stdout.split(rc_marker, 1)[0].strip() if rc_marker in apply_stdout else apply_stdout.strip()
            conflict_path = f"{patch_dir}/{upstream_id}.error.txt"
            error_text = "\n".join(part for part in (clean_stdout, apply_err.strip()) if part).strip()
            if error_text:
                await self._sandbox.write_file(merge_sandbox_id, conflict_path, error_text + "\n")
                entry["error_path"] = conflict_path
            if rc != 0:
                notes.append(
                    f"- `{upstream_id}` 自动应用失败，patch 保存在 `{patch_path}`，错误记录在 `{conflict_path}`。"
                )
                entry["status"] = "conflict"
            else:
                notes.append(f"- `{upstream_id}` 的 patch 已自动并入集成工作区。")
                entry["status"] = "applied"
            manifest["entries"].append(entry)

        merge_context = ""
        if notes:
            merge_context = "\n\n## Merge 集成准备\n" + "\n".join(notes) + "\n"
            await self._write_mas_text(
                workspace_directory,
                run_id,
                f"integration/{node_id}.prep.md",
                merge_context.lstrip(),
            )
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        await self._sandbox.write_file(
            merge_sandbox_id,
            f"/workspace/.agent/merge-manifest-{node_id}.json",
            manifest_json,
        )
        await self._write_mas_text(
            workspace_directory,
            run_id,
            f"integration/{node_id}.manifest.json",
            manifest_json,
        )
        conflicting_nodes = [
            str(entry.get("node_id"))
            for entry in manifest.get("entries", [])
            if isinstance(entry, dict) and entry.get("status") == "conflict"
        ]
        if conflicting_nodes:
            merge_context += (
                "\n## 冲突处理建议\n"
                f"- 当前需要重点核对的上游节点: {', '.join(conflicting_nodes)}\n"
                "- 可以用 `ASK_WORKER: <node_id>: <question>` 询问具体冲突来源。\n"
                "- 如果涉及架构或范围取舍，用 `ESCALATE_TO_PLANNER: <question>` 请求 Planner 决策。\n"
                f"- 结构化清单见 `/workspace/.agent/merge-manifest-{node_id}.json`。\n"
            )
        elif merge_sandbox_id:
            merge_context += f"\n- 结构化清单见 `/workspace/.agent/merge-manifest-{node_id}.json`。\n"
        return merge_sandbox_id, merge_context

    async def _write_node_report_files(
        self,
        run_id: str,
        node_id: str,
        agent_type: str,
        workspace_directory: str | None,
        result: dict[str, Any],
    ) -> None:
        node_dir = self._mas_node_output_dir(workspace_directory, run_id, node_id)
        if node_dir is None:
            return

        summary = self._result_summary_text(result)
        raw_output = str(result.get("raw_output", "") or "")
        title = self._node_report_title(agent_type)
        report_md = (
            f"# {title}\n\n"
            f"- Node ID: `{node_id}`\n"
            f"- Agent Type: `{agent_type}`\n"
            f"- State: `{result.get('state', 'unknown')}`\n"
            f"- Exit Code: `{result.get('exit_code', '')}`\n\n"
            f"## 摘要\n\n{summary}\n"
        )

        await self._write_mas_text(
            workspace_directory,
            run_id,
            f"node-output/{node_id}/report.md",
            report_md,
        )
        if raw_output:
            await self._write_mas_text(
                workspace_directory,
                run_id,
                f"node-output/{node_id}/stream.jsonl",
                raw_output,
            )

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
                agent_type = self._node_agent_type(node)
                upstream_ids = self._upstream_ids_for_node(n_id, edges)
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

                merge_context = ""
                if agent_type == "merge":
                    prepared_sid, prepared_context = await self._prepare_merge_sandbox(
                        run_id=run_id,
                        node_id=n_id,
                        workspace_directory=workspace_directory,
                        upstream_ids=upstream_ids,
                        sandbox_map=sandbox_map,
                        commit_map=commit_map,
                        layer_results=layer_results,
                    )
                    if prepared_sid:
                        assigned_sid = prepared_sid
                    merge_context = prepared_context

                tasks.append(
                    self._execute_node(
                        run_id, node, layer_results, global_config, cancel_event,
                        workspace_directory=workspace_directory,
                        sandbox_id=assigned_sid,
                        upstream_context=self._build_upstream_context(
                            n_id, edges, layer_results,
                        ) + merge_context,
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
                                await self._write_node_git_artifacts(
                                    run_id=run_id,
                                    node_id=node_id,
                                    workspace_directory=workspace_directory,
                                    sandbox_id=_sid,
                                    commit_hash=commit_hash,
                                )
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
                    allow_dynamic_plan = (
                        node_id == "planner"
                        or bool(global_config.get("_allow_dynamic_plan"))
                        or (
                            isinstance(_node_data, dict)
                            and bool(_node_data.get("allowDynamicPlan") or _node_data.get("allow_dynamic_plan"))
                        )
                    )
                    if (
                        _atype == "plan"
                        and result.get("state") == "completed"
                        and not global_config.get("_disable_dynamic_plan")
                        and allow_dynamic_plan
                    ):
                        plan_results = await self._execute_dynamic_plan(
                            run_id, node_id, result, global_config, cancel_event,
                            planner_node=node,
                            workspace_directory=workspace_directory,
                        )
                        layer_results.update(plan_results)

        for sandbox_id in retained_sandboxes:
            try:
                await self._sandbox.destroy(sandbox_id)
            except Exception:
                logger.debug("Failed to destroy retained sandbox %s", sandbox_id, exc_info=True)

        return layer_results

    async def _execute_task_layers(
        self,
        run_id: str,
        layers: list,
        edges: list[dict],
        global_config: dict,
        cancel_event: asyncio.Event,
        task_db_map: dict[str, str],
        task_type_map: dict[str, str],
        task_label_map: dict[str, str],
        workspace_directory: str | None = None,
        planner_node: dict | None = None,
        parent_node_id: str = "planner",
        completed_results: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute DAG layers through the task-aware runner.

        This path preserves DAG parallelism while routing every worker through
        TaskScheduler so progress markers, planner escalation, worker
        collaboration, task DB updates, and artifacts behave consistently.
        """
        layer_results: dict[str, Any] = dict(completed_results or {})
        dag_layers: list[list[str]] = []
        for layer in layers:
            if isinstance(layer, dict):
                nodes = layer.get("nodes", []) or [layer]
            else:
                nodes = layer
            dag_layers.append([str(node.get("id", node.get("node_id", ""))) for node in nodes])

        global_config["_edges"] = edges
        global_config["_task_db_map"] = task_db_map
        global_config["_dag_layers"] = dag_layers

        fallback_planner = planner_node or {
            "id": "planner",
            "agent_type": "plan",
            "model_provider": "",
            "model_id": "",
            "prompt": "Answer worker escalation questions concisely using the run context.",
        }
        workflow_id = await self._get_run_workflow_id(run_id)
        sandbox_map: dict[str, str] = {}
        commit_map: dict[str, str] = {}
        retained_sandboxes: set[str] = set()

        for layer_idx, layer in enumerate(layers):
            if cancel_event.is_set():
                break
            nodes = layer.get("nodes", []) if isinstance(layer, dict) else layer
            if isinstance(layer, dict) and not nodes:
                nodes = [layer]
            runnable_nodes = [
                node for node in nodes
                if str(node.get("id", node.get("node_id", ""))) not in layer_results
            ]

            logger.info(
                "Task-aware layers run=%s layer %d: executing %d nodes (%d already completed)",
                run_id, layer_idx, len(runnable_nodes), len(nodes) - len(runnable_nodes),
            )
            if not runnable_nodes:
                continue

            runnable_after_dependency_check: list[dict] = []
            for node in runnable_nodes:
                node_id = str(node.get("id", node.get("node_id", "")))
                upstream_ids = self._upstream_ids_for_node(node_id, edges)
                bad_upstreams = [
                    upstream_id for upstream_id in upstream_ids
                    if isinstance(layer_results.get(upstream_id), dict)
                    and layer_results[upstream_id].get("state") != "completed"
                ]
                if not bad_upstreams:
                    runnable_after_dependency_check.append(node)
                    continue
                message = (
                    "Blocked because upstream node(s) did not complete: "
                    + ", ".join(bad_upstreams)
                )
                result = {
                    "state": "blocked",
                    "node_id": node_id,
                    "error": message,
                    "result_summary": message,
                }
                layer_results[node_id] = result
                task_id = task_db_map.get(node_id)
                if task_id:
                    await self._update_task_status_db(
                        task_id,
                        "blocked",
                        progress=0,
                        result_summary=message,
                    )
                    await self._emit(
                        "task_updated", run_id, "",
                        task_id=task_id,
                        status="blocked",
                        progress=0,
                        result_summary=message,
                    )
                await self._emit("node_failed", run_id, node_id, content=message)
                await self._emit(
                    "child_completed", run_id, parent_node_id,
                    child_node_id=node_id,
                    content="state=blocked",
                )
            runnable_nodes = runnable_after_dependency_check
            if not runnable_nodes:
                continue

            layer_sandbox_assignments: dict[str, str | None] = {}
            layer_clone_requests: dict[str, str] = {}
            reused_upstream_ids: set[str] = set()

            for node in runnable_nodes:
                node_id = str(node.get("id", node.get("node_id", "")))
                upstream_ids = self._upstream_ids_for_node(node_id, edges)
                resolved_sid: str | None = None
                if upstream_ids:
                    primary_upstream = upstream_ids[-1]
                    candidate = sandbox_map.get(primary_upstream)
                    transfer_edge = next(
                        (
                            e for e in edges
                            if e.get("target") == node_id and e.get("source") == primary_upstream
                        ),
                        {},
                    )
                    transfer_files = transfer_edge.get("data", {}).get("transfer_files", True)
                    if candidate and transfer_files is not False:
                        if primary_upstream not in reused_upstream_ids:
                            resolved_sid = candidate
                            reused_upstream_ids.add(primary_upstream)
                        else:
                            layer_clone_requests[node_id] = candidate
                layer_sandbox_assignments[node_id] = resolved_sid

            async def _run_one(node: dict) -> tuple[str, dict]:
                node_id = str(node.get("id", node.get("node_id", "")))
                agent_type = self._node_agent_type(node)
                assigned_sid = layer_sandbox_assignments.get(node_id)
                if assigned_sid is None and node_id in layer_clone_requests:
                    source_sid = layer_clone_requests[node_id]
                    try:
                        assigned_sid = await self._sandbox.clone(
                            source_sid,
                            f"ws-{node_id}-{uuid4().hex[:8]}",
                        )
                    except Exception:
                        logger.warning("Failed to clone sandbox %s for task-layer node %s", source_sid[:12], node_id, exc_info=True)
                        assigned_sid = None

                merge_context = ""
                if agent_type == "merge":
                    prepared_sid, prepared_context = await self._prepare_merge_sandbox(
                        run_id=run_id,
                        node_id=node_id,
                        workspace_directory=workspace_directory,
                        upstream_ids=self._upstream_ids_for_node(node_id, edges),
                        sandbox_map=sandbox_map,
                        commit_map=commit_map,
                        layer_results=layer_results,
                    )
                    if prepared_sid:
                        assigned_sid = prepared_sid
                    merge_context = prepared_context

                task_id = task_db_map.get(node_id)
                if not task_id:
                    result = await self._execute_node(
                        run_id, node, layer_results, global_config, cancel_event,
                        workspace_directory=workspace_directory,
                        sandbox_id=assigned_sid,
                        upstream_context=self._build_upstream_context(
                            node_id, edges, layer_results,
                        ) + merge_context,
                        destroy_owned_sandbox=False,
                    )
                    return node_id, result

                from app.core.task_scheduler import ManagedTask
                managed = ManagedTask(
                    db_id=task_id,
                    run_id=run_id,
                    title=task_label_map.get(node_id, node_id),
                    description=node.get("prompt", ""),
                    status="pending",
                    assigned_node_id=node_id,
                    assigned_worker_label=task_label_map.get(node_id, node_id),
                )
                result = await self._task_scheduler.run_worker_task(
                    worker_node=node,
                    task=managed,
                    global_config=global_config,
                    cancel_event=cancel_event,
                    db_session=True,
                    planner_node=fallback_planner,
                    layer_results=layer_results,
                    workspace_directory=workspace_directory,
                    upstream_context=self._build_upstream_context(
                        node_id, edges, layer_results,
                    ) + merge_context,
                    sandbox_id=assigned_sid,
                    destroy_owned_sandbox=False,
                )
                if result.get("state") == "completed":
                    await self._create_artifact_for_task(
                        run_id=run_id,
                        workflow_id=workflow_id,
                        task_id=task_id,
                        node_id=node_id,
                        agent_type=task_type_map.get(node_id, "coder"),
                        title=task_label_map.get(node_id, node_id),
                        result=result,
                    )
                await self._emit(
                    "child_completed", run_id, parent_node_id,
                    child_node_id=node_id,
                    content=f"state={result.get('state', 'unknown')}",
                )
                return node_id, result

            results = await asyncio.gather(
                *[_run_one(node) for node in runnable_nodes],
                return_exceptions=True,
            )
            for node, item in zip(runnable_nodes, results):
                node_id = str(node.get("id", node.get("node_id", "")))
                if isinstance(item, Exception):
                    logger.warning("Task-aware layer execution failed for %s: %s", node_id, item, exc_info=True)
                    result = {
                        "state": "failed",
                        "node_id": node_id,
                        "error": str(item),
                        "result_summary": str(item),
                    }
                    layer_results[node_id] = result
                    await self._emit("node_failed", run_id, node_id, content=str(item))
                    await self._emit("status", run_id, node_id, content="failed")
                    await self._emit(
                        "child_completed", run_id, parent_node_id,
                        child_node_id=node_id,
                        content="state=failed",
                    )
                    continue
                node_id, result = item
                layer_results[node_id] = result
                _sid = result.get("sandbox_id")
                if _sid:
                    sandbox_map[node_id] = _sid
                    retained_sandboxes.add(_sid)
                    run_state = self._runs.get(run_id, {})
                    run_state.setdefault("_sandbox_map", {})
                    run_state["_sandbox_map"][node_id] = _sid
                    try:
                        commit_hash = await self._checkpoint.auto_commit(
                            _sid, message=f"after [{node_id}]"
                        )
                        if commit_hash:
                            commit_map[node_id] = commit_hash
                            run_state.setdefault("_commit_map", {})
                            run_state["_commit_map"][node_id] = commit_hash
                            await self._write_node_git_artifacts(
                                run_id=run_id,
                                node_id=node_id,
                                workspace_directory=workspace_directory,
                                sandbox_id=_sid,
                                commit_hash=commit_hash,
                            )
                    except Exception:
                        logger.debug("Failed to auto-commit task-layer sandbox for %s", node_id, exc_info=True)

        total = len(task_db_map)
        completed_count = sum(
            1 for result in layer_results.values()
            if isinstance(result, dict) and result.get("state") == "completed"
        )
        failed_count = sum(
            1 for result in layer_results.values()
            if isinstance(result, dict) and result.get("state") != "completed"
        )
        if total:
            await self._emit(
                "progress_summary", run_id, "",
                total=total,
                completed=completed_count,
                failed=failed_count,
            )

        for sandbox_id in retained_sandboxes:
            try:
                await self._sandbox.destroy(sandbox_id)
            except Exception:
                logger.debug("Failed to destroy retained task-layer sandbox %s", sandbox_id, exc_info=True)

        return layer_results

    # ------------------------------------------------------------------
    # Top-level DAG execution (manages _runs state)
    # ------------------------------------------------------------------

    async def _update_run_status_db(self, run_id: str, status: str) -> None:
        """Update run status in the database."""
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from app.models.db import Workflow as WorkflowModel
            from uuid import UUID as _UUID
            async with async_session_factory() as session:
                from sqlalchemy import select
                result = await session.execute(
                    select(RunModel).where(RunModel.id == _UUID(run_id))
                )
                run_row = result.scalar_one_or_none()
                if run_row is not None:
                    run_row.status = status
                    if status in ("completed", "failed", "cancelled"):
                        from datetime import datetime as _datetime
                        run_row.completed_at = _datetime.utcnow()
                    wf_result = await session.execute(
                        select(WorkflowModel).where(WorkflowModel.id == run_row.workflow_id)
                    )
                    workflow_row = wf_result.scalar_one_or_none()
                    if workflow_row is not None:
                        if status == "running":
                            workflow_row.lifecycle_phase = "running"
                            workflow_row.blockers_json = []
                        elif status in ("completed", "failed", "cancelled"):
                            workflow_row.lifecycle_phase = "review"
                        elif status == "paused":
                            workflow_row.lifecycle_phase = "blocked"
                            blockers = workflow_row.blockers_json or []
                            if not blockers:
                                workflow_row.blockers_json = [{
                                    "code": "human_approval_required",
                                    "message": "当前运行暂停，等待人工审批或继续处理。",
                                }]
                    await session.commit()
            run_state = self._runs.get(run_id, {})
            await self._write_mas_run_manifest(
                run_id=run_id,
                workspace_directory=run_state.get("workspace_directory"),
                kind=run_state.get("kind", "run"),
                status=status,
                payload={
                    "dag_json": run_state.get("dag_json"),
                    "layers": run_state.get("layers"),
                    "global_config": run_state.get("global_config", {}),
                    "recovered": bool(run_state.get("recovered")),
                },
            )
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

    async def _get_run_workflow_id(self, run_id: str) -> str | None:
        try:
            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from sqlalchemy import select
            from uuid import UUID as _UUID

            async with async_session_factory() as session:
                result = await session.execute(
                    select(RunModel).where(RunModel.id == _UUID(run_id))
                )
                run_row = result.scalar_one_or_none()
                return str(run_row.workflow_id) if run_row is not None else None
        except Exception:
            logger.warning("Failed to load workflow id for run %s", run_id, exc_info=True)
            return None

    def _artifact_type_for_node(self, agent_type: str) -> str:
        if agent_type == "explore":
            return "research_note"
        if agent_type == "review":
            return "review_report"
        if agent_type == "merge":
            return "merge_report"
        if agent_type == "shell":
            return "test_result"
        if agent_type == "plan":
            return "final_output"
        if agent_type == "design":
            return "decision"
        return "file_change"

    async def _create_artifact_for_task(
        self,
        run_id: str,
        workflow_id: str | None,
        task_id: str | None,
        node_id: str,
        agent_type: str,
        title: str,
        result: dict[str, Any],
    ) -> str | None:
        """Persist a structured artifact and emit artifact/message events."""
        if not workflow_id:
            return None

        try:
            from app.core.database import async_session_factory
            from app.models.task import Artifact as ArtifactModel, TaskMessage as TaskMessageModel
            from uuid import UUID as _UUID, uuid4

            raw = result.get("raw_output", "") if isinstance(result, dict) else ""
            summary = self._task_scheduler._summarize(raw, max_len=1200) if raw else ""
            if raw and not summary:
                summary = raw.strip()[:1200]
            if not summary:
                summary = result.get("error", "") if isinstance(result, dict) else ""
            if not summary:
                summary = f"Task finished with state={result.get('state', 'unknown')}"

            artifact_type = self._artifact_type_for_node(agent_type)
            artifact_id = uuid4()
            task_uuid = _UUID(task_id) if task_id else None

            async with async_session_factory() as session:
                artifact = ArtifactModel(
                    id=artifact_id,
                    run_id=_UUID(run_id),
                    workflow_id=_UUID(workflow_id),
                    task_id=task_uuid,
                    node_id=node_id,
                    type=artifact_type,
                    title=title[:512] or f"{agent_type} artifact",
                    content=summary,
                    metadata_json={
                        "state": result.get("state"),
                        "exit_code": result.get("exit_code"),
                        "agent_type": agent_type,
                    },
                    created_by=node_id or agent_type,
                )
                session.add(artifact)
                if task_uuid:
                    session.add(
                        TaskMessageModel(
                            task_id=task_uuid,
                            sender_type="planner" if agent_type == "plan" else "worker",
                            sender_id=node_id or agent_type,
                            message_type="artifact_created",
                            content=f"Artifact created: {artifact.title}",
                            artifact_id=artifact_id,
                        )
                    )
                await session.commit()

            await self._emit(
                "artifact_created", run_id, node_id,
                artifact_id=str(artifact_id),
                task_id=task_id or "",
                artifact_type=artifact_type,
                title=title[:512],
            )
            if task_id:
                await self._emit(
                    "task_message", run_id, node_id,
                    task_id=task_id,
                    sender_type="planner" if agent_type == "plan" else "worker",
                    sender_id=node_id or agent_type,
                    message_type="artifact_created",
                    content=f"Artifact created: {title[:512]}",
                    artifact_id=str(artifact_id),
                )
            return str(artifact_id)
        except Exception:
            logger.warning("Failed to create artifact for node %s", node_id, exc_info=True)
            return None

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
            task_type_map: dict[str, str] = {}
            task_label_map: dict[str, str] = {}
            normalized_nodes: list[dict] = []
            auto_child_model_map = global_config.get("_auto_child_model_map", {})
            edges = dag_json.get("edges", [])
            deps_by_node: dict[str, list[str]] = {}
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                source = edge.get("source")
                target = edge.get("target")
                if source and target:
                    deps_by_node.setdefault(str(target), []).append(str(source))

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
                    if agent_type == "plan" and node_id != "planner":
                        agent_type = "design"
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
                    if not model_provider or not model_id:
                        fallback_provider, fallback_model_id = _resolve_auto_child_model(
                            auto_child_model_map, str(agent_type)
                        )
                        if not model_provider:
                            model_provider = fallback_provider
                        if not model_id:
                            model_id = fallback_model_id

                    task_id = uuid4()
                    db_task = TaskModel(
                        id=task_id,
                        run_id=uuid.UUID(run_id),
                        title=str(label)[:200],
                        description=prompt,
                        status="pending",
                        assigned_node_id=node_id,
                        assigned_worker_label=str(label),
                        dependencies=json.dumps(deps_by_node.get(node_id, [])) if deps_by_node.get(node_id) else None,
                    )
                    session.add(db_task)
                    task_db_map[node_id] = str(task_id)
                    task_type_map[node_id] = agent_type
                    task_label_map[node_id] = str(label)

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
                        dependencies=db_task.dependencies or "",
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
                "edges": edges,
            }
            layers = compile_dag(sub_dag)
            global_config["_edges"] = sub_dag["edges"]
            global_config["_task_db_map"] = task_db_map
            global_config["_disable_dynamic_plan"] = True

            child_results = await self._execute_task_layers(
                run_id=run_id,
                layers=layers,
                edges=sub_dag["edges"],
                global_config=global_config,
                cancel_event=cancel_event,
                task_db_map=task_db_map,
                task_type_map=task_type_map,
                task_label_map=task_label_map,
                workspace_directory=workspace_directory,
                planner_node=None,
                parent_node_id="planner",
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
                isinstance(result, dict) and result.get("state") in {"failed", "blocked"}
                for result in child_results.values()
            )
            status = "cancelled" if cancel_event.is_set() else ("failed" if has_failed else "completed")
            self._runs[run_id]["status"] = status
            if status == "completed":
                await self._create_artifact_for_task(
                    run_id=run_id,
                    workflow_id=await self._get_run_workflow_id(run_id),
                    task_id=None,
                    node_id="planner",
                    agent_type="plan",
                    title="Final output",
                    result={"state": status, "raw_output": f"Run completed with {len(child_results)} task results."},
                )
            event_type = "run_completed" if status == "completed" else "run_failed"
            await self._emit(event_type, run_id, "", content=f"status={status}")
            await self._update_run_status_db(run_id, status)

        except Exception as exc:
            logger.exception("Task DAG execution failed for run %s", run_id)
            self._runs[run_id]["status"] = "failed"
            await self._emit("run_failed", run_id, "", content=str(exc))
            await self._update_run_status_db(run_id, "failed")

    async def _resume_task_dag(
        self,
        run_id: str,
        dag_json: dict,
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
    ) -> None:
        """Resume an existing auto DAG from persisted task rows.

        Completed tasks are treated as immutable upstream context. Pending,
        stale running, and blocked tasks are re-enqueued. This avoids restarting
        the entire workflow after a backend crash.
        """
        logger.info("Task DAG recovery STARTED for run %s", run_id)
        try:
            await self._emit("status", run_id, "", content="running")

            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel
            from sqlalchemy import select
            from uuid import uuid4

            edges = dag_json.get("edges", [])
            deps_by_node: dict[str, list[str]] = {}
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                source = edge.get("source")
                target = edge.get("target")
                if source and target:
                    deps_by_node.setdefault(str(target), []).append(str(source))

            task_db_map: dict[str, str] = {}
            task_type_map: dict[str, str] = {}
            task_label_map: dict[str, str] = {}
            completed_results: dict[str, dict[str, Any]] = {}
            normalized_nodes: list[dict] = []
            auto_child_model_map = global_config.get("_auto_child_model_map", {})

            async with async_session_factory() as session:
                task_result = await session.execute(
                    select(TaskModel).where(TaskModel.run_id == uuid.UUID(run_id))
                )
                tasks_by_node = {
                    task.assigned_node_id: task
                    for task in task_result.scalars().all()
                    if task.assigned_node_id
                }

                for node in dag_json.get("nodes", []):
                    if not isinstance(node, dict):
                        continue
                    node_id = str(node.get("id") or "")
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
                    if agent_type == "plan" and node_id != "planner":
                        agent_type = "design"
                    label = str(data.get("label") or node.get("label") or node_id)
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
                    if not model_provider or not model_id:
                        fallback_provider, fallback_model_id = _resolve_auto_child_model(
                            auto_child_model_map, str(agent_type)
                        )
                        if not model_provider:
                            model_provider = fallback_provider
                        if not model_id:
                            model_id = fallback_model_id

                    task = tasks_by_node.get(node_id)
                    if task is None:
                        task = TaskModel(
                            id=uuid4(),
                            run_id=uuid.UUID(run_id),
                            title=label[:200],
                            description=prompt,
                            status="pending",
                            assigned_node_id=node_id,
                            assigned_worker_label=label,
                            dependencies=json.dumps(deps_by_node.get(node_id, [])) if deps_by_node.get(node_id) else None,
                        )
                        session.add(task)
                        await self._emit(
                            "task_created", run_id, "planner",
                            task_id=str(task.id),
                            task_title=task.title,
                            task_description=task.description,
                            status="pending",
                            child_node_id=node_id,
                            dependencies=task.dependencies or "",
                        )
                    elif task.status in ("running", "blocked"):
                        task.status = "pending"

                    task_db_map[node_id] = str(task.id)
                    task_type_map[node_id] = agent_type
                    task_label_map[node_id] = label

                    if task.status == "completed":
                        completed_results[node_id] = {
                            "state": "completed",
                            "node_id": node_id,
                            "raw_output": task.result_summary or "",
                            "recovered": True,
                        }

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

                await session.commit()

            sub_dag = {"nodes": normalized_nodes, "edges": edges}
            layers = compile_dag(sub_dag)
            global_config["_edges"] = edges
            global_config["_task_db_map"] = task_db_map
            global_config["_disable_dynamic_plan"] = True
            global_config["_recovered"] = True

            child_results = await self._execute_task_layers(
                run_id=run_id,
                layers=layers,
                edges=edges,
                global_config=global_config,
                cancel_event=cancel_event,
                task_db_map=task_db_map,
                task_type_map=task_type_map,
                task_label_map=task_label_map,
                workspace_directory=workspace_directory,
                planner_node=None,
                parent_node_id="planner",
                completed_results=completed_results,
            )

            for node_id, result in child_results.items():
                task_id = task_db_map.get(node_id)
                if not task_id:
                    continue
                state = result.get("state", "failed") if isinstance(result, dict) else "failed"
                if node_id in completed_results and state == "completed":
                    continue
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
                isinstance(result, dict) and result.get("state") in {"failed", "blocked"}
                for result in child_results.values()
            )
            status = "cancelled" if cancel_event.is_set() else ("failed" if has_failed else "completed")
            self._runs[run_id]["status"] = status
            if status == "completed":
                await self._create_artifact_for_task(
                    run_id=run_id,
                    workflow_id=await self._get_run_workflow_id(run_id),
                    task_id=None,
                    node_id="planner",
                    agent_type="plan",
                    title="Final output",
                    result={"state": status, "raw_output": f"Recovered run completed with {len(child_results)} task results."},
                )
            event_type = "run_completed" if status == "completed" else "run_failed"
            await self._emit(event_type, run_id, "", content=f"status={status}")
            await self._update_run_status_db(run_id, status)

        except Exception as exc:
            logger.exception("Task DAG recovery failed for run %s", run_id)
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
                for result in layer_results.values()
            )
            status = "cancelled" if cancel_event.is_set() else ("failed" if has_failed else "completed")
            self._runs[run_id]["status"] = status
            if status == "completed":
                await self._create_artifact_for_task(
                    run_id=run_id,
                    workflow_id=await self._get_run_workflow_id(run_id),
                    task_id=None,
                    node_id="planner",
                    agent_type="plan",
                    title="Final output",
                    result={"state": status, "raw_output": f"Run completed with {len(layer_results)} node results."},
                )
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
        await self._write_mas_node_state(
            run_id, node_id, "running", node=node,
            extra={"started_at": datetime.now(timezone.utc).isoformat()},
        )

        # 1. Create sandbox container (use workspace_directory as template if set)
        _owns_sandbox = False
        if sandbox_id is None:
            workspace_id = f"ws-{node_id}-{uuid4().hex[:8]}"
            sandbox_storage_root = self._mas_sandbox_root(workspace_directory, run_id)
            sandbox_id = await self._sandbox.create(
                workspace_id,
                template_dir=workspace_directory,
                storage_root=sandbox_storage_root,
            )
            _owns_sandbox = True
            logger.info("Created sandbox %s for node %s", sandbox_id[:12], node_id)
        else:
            logger.info("Reusing sandbox %s for node %s", sandbox_id[:12], node_id)

        await self._prepare_node_output_workspace(
            run_id, node_id, workspace_directory, sandbox_id,
        )

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

            # 4. Build opencode source runner command
            # React Flow nodes store type in top-level "type" and "data.agentType";
            # compiled node dicts may use "agent_type".  Try all.
            data = node.get("data", {})
            agent_type: str = (
                node.get("agent_type")
                or node.get("type")
                or (data.get("agentType") if isinstance(data, dict) else None)
                or "coder"
            )
            if agent_type == "plan" and node_id != "planner":
                agent_type = "design"
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
                model_provider = str(default_model_cfg.get("provider", ""))
            if not model_id:
                model_id = str(default_model_cfg.get("model", ""))
            model_cfg = _load_model_config(model_provider, model_id)
            prompt: str = (
                node.get("prompt")
                or (data.get("prompt") if isinstance(data, dict) else "")
                or ""
            )

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
            provider_url = str(model_cfg.get("url", ""))
            provider_key = str(model_cfg.get("key", ""))
            provider_url = provider_url or provider_cfg.get("url", "")
            provider_key = provider_key or provider_cfg.get("key", "")
            context_window = int(model_cfg.get("context_window") or 128000)
            max_output_tokens = int(model_cfg.get("max_output_tokens") or 4096)

            # Write prompt to file to avoid shell argument length limits
            prompt_file = "/workspace/.agent/prompt.txt"
            await self._sandbox.write_file(sandbox_id, prompt_file, prompt)

            runner_path = str(_OPENCODE_RUNNER)
            cmd = (
                f"mkdir -p /workspace/.agent /workspace/.workflow && "
                f"cd /workspace && bun {shlex.quote(runner_path)} "
                f"--provider {shlex.quote(model_provider)} "
                f"--model {shlex.quote(model_id)} "
                f"--agent-type {shlex.quote(agent_type)} "
                f"--run-id {shlex.quote(run_id)} "
                f"--node-id {shlex.quote(node_id)} "
                f"--workspace /workspace "
                f"--prompt-file {shlex.quote(prompt_file)} "
                f"--stream-dir /workspace/.agent "
                f"--max-tokens {max_output_tokens} "
                f"--context-window {context_window} "
            )
            if provider_url:
                cmd += f"--provider-url {shlex.quote(provider_url)} "

            runner_env = dict(subprocess_env)
            if provider_key:
                runner_env["MAS_OPENCODE_PROVIDER_KEY"] = provider_key

            await self._emit("shell_stdout", run_id, node_id, content=f"$ {cmd}")

            # 5. Run agent asynchronously
            exec_id = await self._sandbox.exec_async(sandbox_id, cmd, env=runner_env)
            await self._write_mas_node_state(
                run_id, node_id, "running", node=node,
                extra={
                    "sandbox_id": sandbox_id,
                    "exec_id": exec_id,
                    "command": cmd,
                    "prompt_file": prompt_file,
                    "stream_file": stream_file,
                },
            )
            logger.info(
                "Started opencode source runner exec %s in sandbox %s",
                exec_id[:12], sandbox_id[:12],
            )

            # 6. Poll until process completes
            log_pos = 0
            poll_count = 0
            last_stream_activity = time.monotonic()
            last_heartbeat = 0.0
            idle_timeout_seconds = int(os.environ.get("MAS_NODE_IDLE_TIMEOUT_SECONDS", "240") or "240")
            forced_failure_reason = ""
            while not cancel_event.is_set():
                # Read new stream content
                new_log_pos = await self._stream_log_lines(
                    sandbox_id, stream_file, log_pos, run_id, node_id,
                )
                if new_log_pos != log_pos:
                    last_stream_activity = time.monotonic()
                log_pos = new_log_pos

                terminal_events = self._runs.get(run_id, {}).get("_node_terminal_events", {})
                terminal_event = terminal_events.get(node_id) if isinstance(terminal_events, dict) else None

                # Check if the process is still running
                proc_info = await self._sandbox.get_process(exec_id)
                poll_count += 1
                if terminal_event:
                    if proc_info.running:
                        shim = self._sandbox._find_process(exec_id)
                        if shim is not None:
                            try:
                                shim.terminate()
                                await asyncio.wait_for(shim.wait(), timeout=2.0)
                            except Exception:
                                try:
                                    shim.kill()
                                except Exception:
                                    pass
                    logger.info(
                        "Process %s finished by stream terminal event %s after %d polls",
                        exec_id[:12], terminal_event, poll_count,
                    )
                    break
                if not proc_info.running:
                    logger.info(
                        "Process %s finished after %d polls (exit_code=%s)",
                        exec_id[:12], poll_count, proc_info.exit_code,
                    )
                    break

                now = time.monotonic()
                idle_seconds = int(now - last_stream_activity)
                if idle_seconds >= 20 and now - last_heartbeat >= 20:
                    await self._emit(
                        "agent_heartbeat", run_id, node_id,
                        content=f"节点仍在运行，等待模型或工具输出 {idle_seconds}s",
                        idle_seconds=idle_seconds,
                        poll_count=poll_count,
                    )
                    last_heartbeat = now

                if idle_timeout_seconds > 0 and idle_seconds >= idle_timeout_seconds:
                    forced_failure_reason = f"node idle timeout after {idle_seconds}s without stream output"
                    shim = self._sandbox._find_process(exec_id)
                    if shim is not None:
                        try:
                            shim.terminate()
                            await asyncio.wait_for(shim.wait(), timeout=2.0)
                        except Exception:
                            try:
                                shim.kill()
                            except Exception:
                                pass
                    await self._emit(
                        "node_failed", run_id, node_id,
                        content=forced_failure_reason,
                    )
                    logger.warning("Node %s idle timed out: %s", node_id, forced_failure_reason)
                    break

                await asyncio.sleep(1.0)

            # Final read to capture remaining output
            log_pos = await self._stream_log_lines(
                sandbox_id, stream_file, log_pos, run_id, node_id,
            )

            proc_info = await self._sandbox.get_process(exec_id)
            exit_code = proc_info.exit_code if proc_info.exit_code is not None else -1
            logger.info("Node %s exit_code=%d, log_pos=%d", node_id, exit_code, log_pos)
            state = "failed" if forced_failure_reason else ("completed" if exit_code == 0 else "failed")

            await self._emit(
                "node_completed" if state == "completed" else "node_failed",
                run_id, node_id,
                content=f"exit_code={exit_code}",
            )
            await self._emit("status", run_id, node_id, content=state)
            await self._write_mas_node_state(
                run_id, node_id, state, node=node,
                extra={
                    "sandbox_id": sandbox_id,
                    "exec_id": exec_id,
                    "exit_code": exit_code,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "log_pos": log_pos,
                },
            )

            result: dict[str, Any] = {
                "state": state,
                "exit_code": exit_code,
                "node_id": node_id,
                "exec_id": exec_id,
                "sandbox_id": sandbox_id,
            }
            if forced_failure_reason:
                result["error"] = forced_failure_reason

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

            # Capture raw stream output for summaries, artifacts, and plan parsing.
            try:
                raw_log, _ = await self._sandbox.exec(
                    sandbox_id,
                    f"cat {stream_file} 2>/dev/null || true",
                    env=subprocess_env,
                )
                result["raw_output"] = raw_log
            except Exception as exc:
                logger.warning("Failed to read raw_output for node %s: %s", node_id, exc)

            result["result_summary"] = self._result_summary_text(result)

            await self._write_node_report_files(
                run_id, node_id, agent_type, workspace_directory, result,
            )

            return result

        finally:
            final_agent_type = self._node_agent_type(node)
            # Sync sandbox changes back to the workspace directory if configured
            if workspace_directory:
                try:
                    await self._sandbox.sync_back(sandbox_id, workspace_directory)
                except Exception:
                    logger.warning(
                        "sync_back failed for sandbox %s -> %s",
                        sandbox_id[:12], workspace_directory, exc_info=True,
                    )

            if final_agent_type == "merge":
                try:
                    await self._write_integration_workspace_snapshot(
                        run_id, workspace_directory, sandbox_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to snapshot integration workspace for merge node %s",
                        node_id, exc_info=True,
                    )

            try:
                await self._finalize_node_output_workspace(
                    run_id, node_id, workspace_directory, sandbox_id,
                )
            except Exception:
                logger.warning(
                    "Failed to finalize node output workspace for sandbox %s",
                    sandbox_id[:12], exc_info=True,
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
                if event_type in {"node_completed", "node_failed"}:
                    run_state = self._runs.get(run_id)
                    if isinstance(run_state, dict):
                        terminal_events = run_state.setdefault("_node_terminal_events", {})
                        if isinstance(terminal_events, dict):
                            terminal_events[node_id] = event_type
                if event_type in _KNOWN_EVENT_TYPES:
                    extra: dict[str, Any] = {
                        "content": ev.get("content", ""),
                        "tool_name": ev.get("tool_name", ""),
                        "timestamp": ev.get("timestamp", 0),
                    }
                    if isinstance(ev.get("metadata"), dict):
                        extra["metadata"] = ev["metadata"]
                    await self._emit(event_type, run_id, node_id, **extra)
                    content = ev.get("content", "")
                    if isinstance(content, str):
                        if "ESCALATE_TO_PLANNER:" in content:
                            await self._emit(
                                "planner_guidance", run_id, node_id,
                                content=content.split("ESCALATE_TO_PLANNER:", 1)[1].strip(),
                            )
                        if "ASK_WORKER:" in content or "BROADCAST_TO_PEERS:" in content:
                            await self._emit(
                                "worker_message", run_id, node_id,
                                content=content,
                            )
                elif event_type == "text":
                    # Backward compat: some agents emit "text" for LLM tokens
                    await self._emit(
                        "llm_token", run_id, node_id,
                        content=ev.get("content", ""),
                    )
                elif event_type:
                    extra = {
                        key: value
                        for key, value in ev.items()
                        if key not in {"type", "run_id", "node_id"}
                    }
                    await self._emit(event_type, run_id, node_id, **extra)
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
        workspace_directory: str | None = None,
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
                deps_by_child: dict[str, list[str]] = {}
                for edge in child_edges:
                    if not isinstance(edge, dict):
                        continue
                    source = edge.get("source")
                    target = edge.get("target")
                    if source and target:
                        deps_by_child.setdefault(str(target), []).append(str(source))
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
                        "depends_on": deps_by_child.get(dag_node.get("id", ""), []),
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

        # Load shared document for worker context injection
        shared_doc_content = ""
        try:
            from app.models.db import SharedDocument as SharedDocModel
            from sqlalchemy import select as sa_select
            wf_id = global_config.get("_workflow_id")
            if wf_id:
                from app.core.database import async_session_factory as _doc_session_factory
                async with _doc_session_factory() as doc_session:
                    doc_result = await doc_session.execute(
                        sa_select(SharedDocModel).where(
                            SharedDocModel.workflow_id == uuid.UUID(wf_id)
                        )
                    )
                    doc_row = doc_result.scalar_one_or_none()
                    if doc_row and doc_row.content.strip():
                        shared_doc_content = doc_row.content.strip()
        except Exception:
            logger.debug("Failed to load shared doc for workers", exc_info=True)

        # Persist tasks to DB and emit events
        from app.core.database import async_session_factory
        from app.models.task import Task as TaskModel
        from uuid import uuid4

        # Build worker node dicts and persist to DB in one pass
        worker_nodes: list[dict] = []
        task_db_map: dict[str, tuple] = {}  # node_id -> (task_id, label, type)
        auto_child_model_map = global_config.get("_auto_child_model_map", {})

        async with async_session_factory() as session:
            for idx, parsed in enumerate(parsed_tasks):
                child_node_id = parsed.get("node_id") or f"{parent_node_id}_child_{idx}"

                # Create DB task
                task_id = uuid4()
                task_type = parsed.get("type", "coder")
                worker_label = parsed.get("title") or f"{task_type} #{idx + 1}"

                # Compute dependencies from DAG edges or task depends_on
                deps_list = parsed.get("depends_on", []) or parsed.get("dependencies", [])
                deps_json = json.dumps(deps_list) if deps_list else None

                db_task = TaskModel(
                    id=task_id,
                    run_id=uuid.UUID(run_id),
                    title=(parsed.get("title") or parsed.get("prompt", ""))[:200],
                    description=parsed.get("prompt", ""),
                    status="pending",
                    assigned_node_id=child_node_id,
                    assigned_worker_label=worker_label,
                    dependencies=deps_json,
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
                    dependencies=db_task.dependencies or "",
                )

                # Build the worker node dict
                model_str = parsed.get("model", "")
                model_provider, model_id = _parse_full_model_id(model_str)

                strategy_provider, strategy_model_id = _resolve_auto_child_model(
                    auto_child_model_map, str(task_type)
                )
                if not model_provider and strategy_provider:
                    model_provider = strategy_provider
                if not model_id and strategy_model_id:
                    model_id = strategy_model_id

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

                resolved_model = (
                    f"{model_provider}/{model_id}"
                    if model_provider and model_id
                    else model_id
                )

                # Also emit child_created for backward compat (frontend canvas)
                await self._emit(
                    "child_created", run_id, parent_node_id,
                    child_node_id=child_node_id,
                    child_type=parsed.get("type", "coder"),
                    child_prompt=parsed.get("prompt", ""),
                    child_model=resolved_model,
                )

                # Inject escalation protocol into worker prompt
                worker_prompt = parsed.get("prompt", "")

                # Inject project context
                project_goal = global_config.get("_goal", "")
                if project_goal:
                    worker_prompt = f"## 项目目标\n{project_goal}\n\n---\n\n{worker_prompt}"

                # Inject sibling task summary for context awareness
                if len(parsed_tasks) > 1:
                    sibling_lines = []
                    for si, sib in enumerate(parsed_tasks):
                        sib_id = sib.get("node_id") or f"{parent_node_id}_child_{si}"
                        sib_title = sib.get("title") or sib.get("prompt", "")[:60]
                        sib_type = sib.get("type", "coder")
                        marker = " → " if sib_id == child_node_id else "   "
                        sibling_lines.append(f"{marker}[{sib_type}] {sib_title}")
                    worker_prompt += (
                        f"\n\n## 任务上下文（共 {len(parsed_tasks)} 个子任务，当前第 {idx + 1} 个）\n"
                        + "\n".join(sibling_lines)
                    )

                # Inject shared document context
                if shared_doc_content:
                    worker_prompt += f"\n\n## 项目共享文档\n{shared_doc_content}"

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
                task_db_map[child_node_id] = (str(task_id), worker_label, task_type)

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

                child_results = await self._execute_task_layers(
                    run_id=run_id,
                    layers=sub_layers,
                    edges=child_edges_dag,
                    global_config=global_config,
                    cancel_event=cancel_event,
                    task_db_map={node_id: meta[0] for node_id, meta in task_db_map.items()},
                    task_type_map={node_id: meta[2] for node_id, meta in task_db_map.items()},
                    task_label_map={node_id: meta[1] for node_id, meta in task_db_map.items()},
                    workspace_directory=workspace_directory,
                    planner_node=planner_node,
                    parent_node_id=parent_node_id,
                )

                return child_results

            else:
                worker_ids = {node["id"] for node in worker_nodes}
                legacy_edges: list[dict] = []
                for idx, parsed in enumerate(parsed_tasks):
                    target_id = parsed.get("node_id") or f"{parent_node_id}_child_{idx}"
                    for dep in parsed.get("depends_on", []) or parsed.get("dependencies", []) or []:
                        dep_id = str(dep)
                        if dep_id in worker_ids:
                            legacy_edges.append({
                                "id": f"e_{dep_id}_{target_id}",
                                "source": dep_id,
                                "target": target_id,
                            })

                try:
                    legacy_layers = compile_dag({
                        "nodes": worker_nodes,
                        "edges": legacy_edges,
                    })
                except ValueError:
                    legacy_layers = [[node] for node in worker_nodes]

                return await self._execute_task_layers(
                    run_id=run_id,
                    layers=legacy_layers,
                    edges=legacy_edges,
                    global_config=global_config,
                    cancel_event=cancel_event,
                    task_db_map={node_id: meta[0] for node_id, meta in task_db_map.items()},
                    task_type_map={node_id: meta[2] for node_id, meta in task_db_map.items()},
                    task_label_map={node_id: meta[1] for node_id, meta in task_db_map.items()},
                    workspace_directory=workspace_directory,
                    planner_node=planner_node,
                    parent_node_id=parent_node_id,
                )
