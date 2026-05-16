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
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.core.task_scheduler import PROGRESS_MARKER, TaskScheduler
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner
from app.workflows.compiler import compile_dag
from app.workflows.plan_parser import parse_plan_output, parse_plan_to_dag

logger = logging.getLogger(__name__)

_model_config_cache: dict[str, dict] = {}
_model_config_cache_time: float = 0
_MODEL_CONFIG_CACHE_TTL = 5.0  # seconds

_db_semaphore: asyncio.Semaphore | None = None


def _get_db_semaphore() -> asyncio.Semaphore:
    global _db_semaphore
    if _db_semaphore is None:
        _db_semaphore = asyncio.Semaphore(10)
    return _db_semaphore

# Event types that the agent writes to stream.jsonl in the correct format
_KNOWN_EVENT_TYPES = frozenset({
    "llm_token", "llm_chunk", "tool_call", "tool_result", "shell_stdout",
    "shell_stderr", "status", "error", "node_started", "node_completed",
    "node_failed", "child_created", "child_completed",
    "task_created", "task_updated", "task_message",
    "artifact_created", "worker_message",
    "idle_warning", "agent_status",
})

# Idle timeout defaults per agent type (seconds).  Override with
# MAS_NODE_IDLE_TIMEOUT_SECONDS for the global default.
_NODE_IDLE_TIMEOUT: dict[str, int] = {
    "explore": 600,
    "plan": 600,
    "design": 600,
    "coder": 480,
    "shell": 300,
    "review": 300,
    "human": 0,  # human nodes never idle-timeout
    "merge": 300,
}
_DEFAULT_IDLE_TIMEOUT = 480

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
    global _model_config_cache, _model_config_cache_time
    now = time.time()
    if now - _model_config_cache_time < _MODEL_CONFIG_CACHE_TTL:
        return _model_config_cache.get("models", [])

    settings_path = Path(__file__).resolve().parents[3] / "data" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _model_config_cache = {"models": []}
        _model_config_cache_time = now
        return []

    models = data.get("models", [])
    if not isinstance(models, list) or not models:
        _model_config_cache = {"models": []}
        _model_config_cache_time = now
        return []

    result = [m for m in models if isinstance(m, dict)]
    _model_config_cache = {"models": result}
    _model_config_cache_time = now
    return result


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


def _normalize_node_config(
    node: dict,
    auto_child_model_map: dict,
    node_id: str,
) -> tuple[dict, str, str, str]:
    """Normalize a node dict for execution.

    Returns: (normalized_node, agent_type, model_provider, model_id)
    """
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

    normalized = {
        "id": node_id,
        "agent_type": agent_type,
        "model_provider": model_provider,
        "model_id": model_id,
        "prompt": (
            prompt
            + f"\n\n---\nTask ID: {node_id}\n"
            + "To report progress, output a line:\n"
            + f"{PROGRESS_MARKER} <0-100>\n"
        ),
    }
    # Preserve rich context fields from planner output
    for rich_key in ("target_files", "interface_contract", "context_summary"):
        value = node.get(rich_key) or data.get(rich_key)
        if value:
            normalized[rich_key] = value
    return normalized, str(agent_type), model_provider, model_id


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

    async def start_run(
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
            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from app.models.db import Workflow
            from app.models.task import Task as TaskModel

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
                        if task.status == "running":
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
        async with _get_db_semaphore():
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

    async def _build_upstream_context(
        self,
        node_id: str,
        edges: list[dict],
        layer_results: dict[str, Any],
    ) -> str:
        """Build a formatted string summarising upstream node outputs.

        For each upstream edge targeting *node_id*, extracts the
        ``result_summary`` from the upstream result, or falls back to the
        last 4 000 characters of LLM text in ``raw_output``.
        Also includes a list of changed files when available.
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
                    summary = full_text[-4000:]

            if summary:
                sections.append(f"### {source_id}\n{summary}")

            # Include changed files list from the upstream sandbox
            source_sandbox = source_result.get("sandbox_id")
            if source_sandbox:
                try:
                    changed_out, _ = await self._sandbox.exec(
                        source_sandbox,
                        'git diff --name-status HEAD~1 HEAD 2>/dev/null || true',
                    )
                    if changed_out.strip():
                        sections.append(
                            f"### {source_id} 变更文件\n```\n{changed_out.strip()}\n```"
                        )
                except Exception:
                    pass

        if not sections:
            return ""

        return "\n\n## 上游节点输出\n" + "\n".join(sections) + "\n"

    async def _build_rich_node_context(
        self,
        node: dict,
        sandbox_id: str,
        workspace_directory: str | None,
    ) -> str:
        """Build rich context from planner-specified fields: target file contents,
        interface contract, and context summary."""
        sections: list[str] = []
        data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}

        # 1. Interface contract
        contract = node.get("interface_contract") or data.get("interface_contract") or ""
        if contract:
            sections.append(f"## 接口契约\n{contract}")

        # 2. Context summary
        ctx = node.get("context_summary") or data.get("context_summary") or ""
        if ctx:
            sections.append(f"## 上下文说明\n{ctx}")

        # 3. Target files — read current content from sandbox
        target_files = node.get("target_files") or data.get("target_files") or []
        if isinstance(target_files, list) and target_files:
            file_sections: list[str] = []
            for fpath in target_files[:10]:
                fpath = str(fpath).strip()
                if not fpath:
                    continue
                content = ""
                try:
                    content = await self._sandbox.read_file(sandbox_id, f"/workspace/{fpath}")
                except Exception:
                    pass
                if not content and workspace_directory:
                    try:
                        host_path = Path(workspace_directory) / fpath
                        if host_path.is_file():
                            content = host_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                if content:
                    if len(content) > 3000:
                        content = content[:3000] + f"\n... (文件截断，共 {len(content)} 字符)"
                    file_sections.append(f"### {fpath}\n```\n{content}\n```")
                else:
                    file_sections.append(f"### {fpath}\n（文件不存在，需要创建）")
            if file_sections:
                sections.append("## 目标文件当前内容\n" + "\n\n".join(file_sections))

        if not sections:
            return ""
        return "\n\n" + "\n\n".join(sections) + "\n"

    async def _execute_layers(
        self,
        run_id: str,
        layers: list,
        edges: list[dict],
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
        task_db_map: dict[str, str] | None = None,
        task_label_map: dict[str, str] | None = None,
        task_type_map: dict[str, str] | None = None,
        parent_node_id: str = "planner",
        completed_results: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Unified layer executor — handles both task-aware and simple DAG modes.

        Iterates layers sequentially; within each layer, nodes execute in
        parallel.  When *task_db_map* is provided each node is routed through
        ``TaskScheduler.run_worker_task()``; otherwise ``_execute_node()`` is
        called directly.  Returns a dict mapping node_id -> result.
        """
        task_db_map = task_db_map or {}
        task_label_map = task_label_map or {}
        task_type_map = task_type_map or {}
        has_tasks = bool(task_db_map)

        layer_results: dict[str, Any] = dict(completed_results or {})
        sandbox_map: dict[str, str] = {}
        commit_map: dict[str, str] = {}
        retained_sandboxes: set[str] = set()

        workflow_id = await self._get_run_workflow_id(run_id) if has_tasks else None

        for layer_idx, layer in enumerate(layers):
            if cancel_event.is_set():
                break

            if isinstance(layer, dict):
                nodes = layer.get("nodes", [])
                if not nodes:
                    nodes = [layer]
            else:
                nodes = layer

            runnable_nodes = [
                node for node in nodes
                if str(node.get("id", node.get("node_id", ""))) not in layer_results
            ]

            logger.info(
                "Layers run=%s layer %d: executing %d nodes (%d already completed)",
                run_id, layer_idx, len(runnable_nodes), len(nodes) - len(runnable_nodes),
            )
            if not runnable_nodes:
                continue

            # Dependency failure check
            runnable_after_dependency_check: list[dict] = []
            for node in runnable_nodes:
                node_id = str(node.get("id", node.get("node_id", "")))
                upstream_ids = self._upstream_ids_for_node(node_id, edges)
                bad_upstreams = [
                    upstream_id for upstream_id in upstream_ids
                    if isinstance(layer_results.get(upstream_id), dict)
                    and layer_results[upstream_id].get("state") == "failed"
                ]
                if not bad_upstreams:
                    runnable_after_dependency_check.append(node)
                    continue
                message = (
                    "Failed because upstream node(s) did not complete: "
                    + ", ".join(bad_upstreams)
                )
                result = {
                    "state": "failed",
                    "node_id": node_id,
                    "error": message,
                    "result_summary": message,
                }
                layer_results[node_id] = result
                if has_tasks:
                    task_id = task_db_map.get(node_id)
                    if task_id:
                        await self._update_task_status_db(
                            task_id, "failed", progress=0, result_summary=message,
                        )
                        await self._emit(
                            "task_updated", run_id, "",
                            task_id=task_id, status="failed", progress=0,
                            result_summary=message,
                        )
                await self._emit("node_failed", run_id, node_id, content=message)
                await self._emit(
                    "child_completed", run_id, parent_node_id,
                    child_node_id=node_id, content="state=failed",
                )
            runnable_nodes = runnable_after_dependency_check
            if not runnable_nodes:
                continue

            # Sandbox reuse strategy
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
                default_retries = 2 if agent_type == "coder" else 1
                max_retries = int(
                    (node.get("data", {}).get("retry_count") if isinstance(node.get("data"), dict) else None)
                    or node.get("retry_count")
                    or default_retries
                )

                for attempt in range(max_retries + 1):
                    if cancel_event.is_set():
                        return node_id, {"state": "failed", "error": "cancelled", "node_id": node_id}

                    assigned_sid = layer_sandbox_assignments.get(node_id)
                    if assigned_sid is None and node_id in layer_clone_requests:
                        source_sid = layer_clone_requests[node_id]
                        try:
                            assigned_sid = await self._sandbox.clone(
                                source_sid,
                                f"ws-{node_id}-{uuid4().hex[:8]}",
                            )
                        except Exception:
                            logger.warning("Failed to clone sandbox %s for layer node %s", source_sid[:12], node_id, exc_info=True)
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

                    if has_tasks and node_id in task_db_map:
                        from app.core.task_scheduler import ManagedTask
                        managed = ManagedTask(
                            db_id=task_db_map[node_id],
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
                            layer_results=layer_results,
                            workspace_directory=workspace_directory,
                            upstream_context=await self._build_upstream_context(
                                node_id, edges, layer_results,
                            ) + merge_context,
                            sandbox_id=assigned_sid,
                            destroy_owned_sandbox=False,
                        )
                    else:
                        result = await self._execute_node(
                            run_id, node, layer_results, global_config, cancel_event,
                            workspace_directory=workspace_directory,
                            sandbox_id=assigned_sid,
                            upstream_context=await self._build_upstream_context(
                                node_id, edges, layer_results,
                            ) + merge_context,
                            destroy_owned_sandbox=False,
                        )

                    # Retry on failure — inject error context into the node prompt
                    if result.get("state") == "failed" and attempt < max_retries:
                        logger.info(
                            "Node %s attempt %d/%d failed, retrying: %s",
                            node_id, attempt + 1, max_retries + 1,
                            result.get("error", "")[:200],
                        )
                        await self._emit(
                            "status", run_id, node_id,
                            content=f"retrying (attempt {attempt + 2}/{max_retries + 1})",
                        )
                        # Build failure context for the retry prompt
                        retry_error = result.get("error", "") or ""
                        retry_llm = ""
                        raw_out = result.get("raw_output", "")
                        if raw_out:
                            retry_llm = self._extract_llm_text(raw_out)[-1500:]
                        failure_ctx = (
                            f"\n\n## 上次执行失败（第 {attempt + 1} 次尝试）\n"
                            f"错误信息：{retry_error[:500]}\n"
                        )
                        if retry_llm:
                            failure_ctx += f"上次输出摘要：\n```\n{retry_llm}\n```\n"
                        failure_ctx += "请分析失败原因，修正你的实现，避免重复同样的错误。\n"
                        # Shallow-copy node to avoid mutating the layer list
                        node = dict(node)
                        node_data = node.get("data")
                        if isinstance(node_data, dict):
                            node["data"] = {**node_data, "prompt": node_data.get("prompt", "") + failure_ctx}
                        else:
                            node["prompt"] = node.get("prompt", "") + failure_ctx
                        # Persist retry info to task DB
                        if has_tasks and node_id in task_db_map:
                            await self._update_task_retry_db(
                                task_id=task_db_map[node_id],
                                retry_count=attempt + 1,
                                last_error=retry_error[:2000],
                            )
                        layer_sandbox_assignments[node_id] = None
                        continue

                    if has_tasks and result.get("state") == "completed" and node_id in task_db_map:
                        await self._create_artifact_for_task(
                            run_id=run_id,
                            workflow_id=workflow_id,
                            task_id=task_db_map[node_id],
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

                return node_id, {"state": "failed", "error": "max retries exceeded", "node_id": node_id}

            results = await asyncio.gather(
                *[_run_one(node) for node in runnable_nodes],
                return_exceptions=True,
            )
            for node, item in zip(runnable_nodes, results):
                node_id = str(node.get("id", node.get("node_id", "")))
                if isinstance(item, Exception):
                    logger.warning("Layer execution failed for %s: %s", node_id, item, exc_info=True)
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
                        child_node_id=node_id, content="state=failed",
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
                        logger.debug("Failed to auto-commit layer sandbox for %s", node_id, exc_info=True)

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
                        workspace_directory=workspace_directory,
                    )
                    layer_results.update(plan_results)

        if has_tasks:
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
                    total=total, completed=completed_count, failed=failed_count,
                )

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
            from uuid import UUID as _UUID

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel
            from app.models.db import Workflow as WorkflowModel
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
                            workflow_row.lifecycle_phase = "review"
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
        async with _get_db_semaphore():
            try:
                from uuid import UUID as _UUID

                from sqlalchemy import select

                from app.core.database import async_session_factory
                from app.models.task import Task as TaskModel

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

    async def _update_task_retry_db(
        self,
        task_id: str,
        retry_count: int,
        last_error: str,
    ) -> None:
        """Persist retry count and last error message for a task."""
        async with _get_db_semaphore():
            try:
                from uuid import UUID as _UUID

                from sqlalchemy import select

                from app.core.database import async_session_factory
                from app.models.task import Task as TaskModel

                async with async_session_factory() as session:
                    result = await session.execute(
                        select(TaskModel).where(TaskModel.id == _UUID(task_id))
                    )
                    task_row = result.scalar_one_or_none()
                    if task_row is not None:
                        task_row.retry_count = retry_count
                        task_row.last_error = last_error[:2000]
                        await session.commit()
            except Exception:
                logger.warning("Failed to update task retry info for %s", task_id, exc_info=True)

    async def _get_run_workflow_id(self, run_id: str) -> str | None:
        try:
            from uuid import UUID as _UUID

            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.db import Run as RunModel

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
            from uuid import UUID as _UUID
            from uuid import uuid4

            from app.core.database import async_session_factory
            from app.models.task import Artifact as ArtifactModel
            from app.models.task import TaskMessage as TaskMessageModel

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

            from uuid import uuid4

            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel

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

                    normalized, agent_type, model_provider, model_id = _normalize_node_config(
                        node, auto_child_model_map, node_id,
                    )
                    label = (
                        node.get("data", {}).get("label")
                        or node.get("label")
                        or node_id
                    )
                    prompt = (
                        node.get("prompt")
                        or (node.get("data", {}).get("prompt")
                            if isinstance(node.get("data"), dict)
                            else "")
                        or ""
                    )

                    task_id = uuid4()
                    deps = deps_by_node.get(node_id, [])
                    db_task = TaskModel(
                        id=task_id,
                        run_id=uuid.UUID(run_id),
                        title=str(label)[:200],
                        description=prompt,
                        status="pending",
                        assigned_node_id=node_id,
                        assigned_worker_label=str(label),
                        dependencies=json.dumps(deps) if deps else None,
                    )
                    session.add(db_task)
                    task_db_map[node_id] = str(task_id)
                    task_type_map[node_id] = agent_type
                    task_label_map[node_id] = str(label)

                    normalized_nodes.append(normalized)

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

            child_results = await self._execute_layers(
                run_id=run_id,
                layers=layers,
                edges=sub_dag["edges"],
                global_config=global_config,
                cancel_event=cancel_event,
                workspace_directory=workspace_directory,
                task_db_map=task_db_map,
                task_type_map=task_type_map,
                task_label_map=task_label_map,
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
                isinstance(result, dict) and result.get("state") == "failed"
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
        stale running tasks are re-enqueued. This avoids restarting
        the entire workflow after a backend crash.
        """
        logger.info("Task DAG recovery STARTED for run %s", run_id)
        try:
            await self._emit("status", run_id, "", content="running")

            from uuid import uuid4

            from sqlalchemy import select

            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel

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

                    normalized, agent_type, model_provider, model_id = _normalize_node_config(
                        node, auto_child_model_map, node_id,
                    )
                    data = node.get("data", {})
                    label = str(
                        data.get("label")
                        if isinstance(data, dict)
                        else node.get("label")
                        or node_id
                    )
                    prompt = (
                        node.get("prompt")
                        or (data.get("prompt")
                            if isinstance(data, dict)
                            else "")
                        or ""
                    )

                    task = tasks_by_node.get(node_id)
                    if task is None:
                        deps = deps_by_node.get(node_id, [])
                        task = TaskModel(
                            id=uuid4(),
                            run_id=uuid.UUID(run_id),
                            title=label[:200],
                            description=prompt,
                            status="pending",
                            assigned_node_id=node_id,
                            assigned_worker_label=label,
                            dependencies=json.dumps(deps) if deps else None,
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
                    elif task.status == "running":
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

                    normalized_nodes.append(normalized)

                await session.commit()

            sub_dag = {"nodes": normalized_nodes, "edges": edges}
            layers = compile_dag(sub_dag)
            global_config["_edges"] = edges
            global_config["_task_db_map"] = task_db_map
            global_config["_disable_dynamic_plan"] = True
            global_config["_recovered"] = True

            child_results = await self._execute_layers(
                run_id=run_id,
                layers=layers,
                edges=edges,
                global_config=global_config,
                cancel_event=cancel_event,
                workspace_directory=workspace_directory,
                task_db_map=task_db_map,
                task_type_map=task_type_map,
                task_label_map=task_label_map,
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
                isinstance(result, dict) and result.get("state") == "failed"
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
            # Clear stale stream.jsonl from previous node to prevent
            # _stream_log_lines from picking up ghost terminal events.
            await self._sandbox.exec(
                sandbox_id,
                "mkdir -p /workspace/.agent && : > /workspace/.agent/stream.jsonl",
            )

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

            if not model_provider or not model_id:
                return {
                    "state": "failed",
                    "error": "No model configured. Please configure a model in settings.",
                    "node_id": node_id,
                    "sandbox_id": sandbox_id,
                }

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
                from app.api.runs import _approval_results, clear_approval, set_approval_event
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

            # Append upstream context when provided (dual-mode data passing)
            if upstream_context:
                prompt = prompt + upstream_context

            # Inject rich context from planner-specified fields
            rich_context = await self._build_rich_node_context(node, sandbox_id, workspace_directory)
            if rich_context:
                prompt = prompt + rich_context

            # Resolve provider URL + API key from models.json
            from app.api.models import load_provider_config
            provider_cfg = load_provider_config().get(model_provider, {})
            provider_url = str(model_cfg.get("url", ""))
            provider_key = str(model_cfg.get("key", ""))
            provider_url = provider_url or provider_cfg.get("url", "")
            provider_key = provider_key or provider_cfg.get("key", "")

            # Fallback: scan all providers in models.json for a matching key
            if not provider_url or not provider_key:
                for _pid, _pcfg in load_provider_config().items():
                    if _pcfg.get("url") and _pcfg.get("key"):
                        provider_url = provider_url or _pcfg["url"]
                        provider_key = provider_key or _pcfg["key"]
                        break

            # Fallback: environment variables (same as planner_chat.py)
            if not provider_url or not provider_key:
                provider_url = provider_url or os.environ.get("MIMO_API_URL", "")
                provider_key = provider_key or os.environ.get("MIMO_API_KEY", "")
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

            # 6. Subscribe to runner SSE for real-time events
            # Try SSE first (run-node.ts exposes /events), fall back to file polling
            _agent_type_for_timeout = agent_type or "coder"
            idle_timeout_seconds = _NODE_IDLE_TIMEOUT.get(
                _agent_type_for_timeout,
                _DEFAULT_IDLE_TIMEOUT,
            )
            env_timeout = os.environ.get("MAS_NODE_IDLE_TIMEOUT_SECONDS")
            if env_timeout:
                idle_timeout_seconds = int(env_timeout)
            forced_failure_reason = ""

            sse_port = await self._read_runner_port(
                sandbox_id, timeout=15,
                run_id=run_id,
                workspace_directory=workspace_directory,
            )
            if sse_port:
                forced_failure_reason = await self._consume_runner_sse(
                    sse_port, run_id, node_id, exec_id,
                    cancel_event, idle_timeout_seconds,
                )
                # SSE failed → switch to real-time file polling so the
                # frontend still sees progress instead of a blank screen.
                if forced_failure_reason:
                    logger.info(
                        "SSE failed for node %s, switching to file polling for real-time events",
                        node_id,
                    )
                    forced_failure_reason = await self._poll_stream_file(
                        sandbox_id, stream_file, run_id, node_id, exec_id,
                        cancel_event, idle_timeout_seconds, _agent_type_for_timeout,
                    )
            else:
                forced_failure_reason = await self._poll_stream_file(
                    sandbox_id, stream_file, run_id, node_id, exec_id,
                    cancel_event, idle_timeout_seconds, _agent_type_for_timeout,
                )

            # Wait for the process to actually finish before checking exit code.
            # Without this, if SSE/polling returns early, we'd read None → -1
            # while the runner is still working.
            try:
                exit_code = await asyncio.wait_for(
                    self._sandbox.wait_process(exec_id), timeout=30,
                )
            except asyncio.TimeoutError:
                proc_info = await self._sandbox.get_process(exec_id)
                exit_code = proc_info.exit_code if proc_info.exit_code is not None else -1

            # Final read to capture any remaining output from stream.jsonl
            await self._stream_log_lines(
                sandbox_id, stream_file, 0, run_id, node_id,
            )
            logger.info("Node %s exit_code=%d", node_id, exit_code)
            if forced_failure_reason and exit_code == 0:
                logger.info(
                    "Node %s had error but exit_code=0, treating as completed",
                    node_id,
                )
                forced_failure_reason = ""
            state = "failed" if (forced_failure_reason or exit_code != 0) else "completed"

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
                        stderr_text = await asyncio.to_thread(
                            lambda: shim._proc.stderr.read().decode("utf-8", errors="replace")[:2000]
                        )
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
            if start_pos == 0:
                log_content, _ = await self._sandbox.exec(
                    sandbox_id,
                    f"cat {stream_file} 2>/dev/null || true",
                )
            else:
                log_content, _ = await self._sandbox.exec(
                    sandbox_id,
                    f"tail -c +{start_pos + 1} {stream_file} 2>/dev/null || true",
                )
        except Exception as exc:
            logger.warning("_stream_log_lines exec failed: %s", exc)
            return start_pos

        if len(log_content) <= start_pos:
            if start_pos == 0:
                logger.debug("_stream_log_lines: file empty (len=%d)", len(log_content))
            return start_pos

        logger.info("_stream_log_lines: read %d new bytes (pos %d→%d)", len(log_content) - start_pos, start_pos, len(log_content))

        for line in log_content.strip().split("\n"):
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
                await self._emit("shell_stdout", run_id, node_id, content=line)

        return len(log_content)

    # ------------------------------------------------------------------
    # SSE-based real-time event subscription (replaces file polling)
    # ------------------------------------------------------------------

    async def _read_runner_port(
        self,
        sandbox_id: str,
        timeout: int = 15,
        *,
        run_id: str | None = None,
        workspace_directory: str | None = None,
    ) -> int | None:
        """Read the SSE port from runner.port file written by run-node.ts.

        Tries direct host filesystem first (faster), then sandbox exec.
        If sandbox state is not in memory, attempts to reconstruct the path
        from run_id and workspace_directory.
        """
        # Method 1: direct host filesystem read via sandbox path mapping
        port_path: Path | None = None
        try:
            state = self._sandbox._state(sandbox_id)
            port_path = state.workspace_dir / ".agent" / "runner.port"
            logger.debug("runner.port path from sandbox state: %s", port_path)
        except KeyError:
            # Sandbox state not in memory (e.g., after restart) - reconstruct path
            if run_id and workspace_directory:
                sandbox_root = self._mas_sandbox_root(workspace_directory, run_id)
                if sandbox_root:
                    port_path = sandbox_root / sandbox_id / "workspace" / ".agent" / "runner.port"
                    logger.info(
                        "Reconstructed runner.port path from run context: %s", port_path,
                    )
            if port_path is None:
                logger.warning(
                    "Sandbox %s not in memory and cannot reconstruct path "
                    "(run_id=%s, workspace_directory=%s)",
                    sandbox_id[:12], run_id, workspace_directory,
                )
        except Exception:
            logger.debug("Unexpected error getting sandbox state", exc_info=True)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Try direct read first
            if port_path is not None:
                try:
                    content = await asyncio.to_thread(lambda: port_path.read_text().strip())
                    if content and content.isdigit():
                        port = int(content)
                        logger.info("Read runner SSE port (direct): %d", port)
                        return port
                except FileNotFoundError:
                    pass
                except Exception:
                    pass

            # Fallback: sandbox exec
            try:
                content, _ = await self._sandbox.exec(
                    sandbox_id, "cat /workspace/.agent/runner.port 2>/dev/null || true",
                )
                port_str = content.strip()
                if port_str and port_str.isdigit():
                    port = int(port_str)
                    logger.info("Read runner SSE port (exec): %d", port)
                    return port
            except Exception:
                pass
            await asyncio.sleep(0.5)
        logger.warning("Failed to read runner.port within %ds for sandbox %s", timeout, sandbox_id[:12])
        return None

    async def _consume_runner_sse(
        self,
        port: int,
        run_id: str,
        node_id: str,
        exec_id: str,
        cancel_event: asyncio.Event,
        idle_timeout_seconds: int,
    ) -> str:
        """Subscribe to run-node.ts SSE and process events in real-time.

        Returns forced_failure_reason (empty string if no failure).
        """
        import httpx

        url = f"http://127.0.0.1:{port}/events"
        forced_failure_reason = ""
        last_activity = time.monotonic()
        last_busy_time: float | None = None
        idle_warnings_sent: set[int] = set()
        hard_timeout = idle_timeout_seconds * 2 if idle_timeout_seconds > 0 else 0

        async def _idle_monitor() -> str:
            """Concurrent task that enforces idle timeout."""
            nonlocal last_activity, last_busy_time, forced_failure_reason
            while True:
                await asyncio.sleep(5)
                if cancel_event.is_set():
                    break
                now = time.monotonic()
                idle_seconds = int(now - last_activity)

                # If we've seen busy recently, agent is working — only check hard timeout
                if last_busy_time is not None and (now - last_busy_time) < idle_timeout_seconds:
                    # Reset stale last_busy_time to prevent permanent bypass
                    if last_busy_time is not None and (now - last_busy_time) > hard_timeout and hard_timeout > 0:
                        last_busy_time = None
                    continue

                # Hard timeout: kill regardless of status
                if hard_timeout > 0 and idle_seconds >= hard_timeout:
                    forced_failure_reason = (
                        f"node hard timeout after {idle_seconds}s "
                        f"(exceeded {hard_timeout}s hard limit)"
                    )
                    logger.warning("Node %s SSE hard timeout: %s", node_id, forced_failure_reason)
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
                    return forced_failure_reason

                # If no busy events AND no activity for full timeout, kill
                if idle_timeout_seconds > 0 and idle_seconds >= idle_timeout_seconds:
                    forced_failure_reason = (
                        f"node idle timeout after {idle_seconds}s "
                        f"without SSE activity (status events stopped)"
                    )
                    logger.warning("Node %s SSE idle timeout: %s", node_id, forced_failure_reason)
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
                    return forced_failure_reason

                # Progressive warnings
                if idle_timeout_seconds > 0 and idle_seconds > 0:
                    pct = int(idle_seconds / idle_timeout_seconds * 100)
                    for threshold in (50, 75, 90):
                        if pct >= threshold and threshold not in idle_warnings_sent:
                            idle_warnings_sent.add(threshold)
                            await self._emit(
                                "idle_warning", run_id, node_id,
                                content=f"节点无活动 {idle_seconds}s，超过 {threshold}%",
                                idle_seconds=idle_seconds,
                                timeout_seconds=idle_timeout_seconds,
                                threshold_pct=threshold,
                            )
            return ""

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=None, write=5, pool=5), trust_env=False) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        logger.warning("SSE connection failed: %d", response.status_code)
                        return ""

                    logger.info("SSE connected to runner on port %d for node %s", port, node_id)

                    monitor_task = asyncio.create_task(_idle_monitor())

                    try:
                        async for raw_line in response.aiter_lines():
                            if cancel_event.is_set() or forced_failure_reason:
                                break

                            if not raw_line.startswith("data:"):
                                continue

                            raw_line = raw_line[5:].strip()
                            if not raw_line:
                                continue

                            try:
                                ev = json.loads(raw_line)
                            except json.JSONDecodeError:
                                continue

                            event_type = ev.get("type", "")

                            # Track terminal events
                            if event_type in {"node_completed", "node_failed"}:
                                run_state = self._runs.get(run_id)
                                if isinstance(run_state, dict):
                                    terminal_events = run_state.setdefault("_node_terminal_events", {})
                                    if isinstance(terminal_events, dict):
                                        terminal_events[node_id] = event_type

                            # agent_status from opencode: busy/idle/retry
                            if event_type == "agent_status":
                                status_type = ev.get("status_type", "")
                                last_activity = time.monotonic()
                                if status_type == "busy":
                                    last_busy_time = time.monotonic()
                                logger.debug(
                                    "Node %s agent_status=%s", node_id, status_type,
                                )
                                await self._emit(
                                    "agent_status", run_id, node_id,
                                    content=ev.get("content", ""),
                                    status_type=status_type,
                                )
                                continue

                            # Emit known event types
                            if event_type in _KNOWN_EVENT_TYPES:
                                extra: dict[str, Any] = {
                                    "content": ev.get("content", ""),
                                    "tool_name": ev.get("tool_name", ""),
                                    "timestamp": ev.get("timestamp", 0),
                                }
                                if isinstance(ev.get("metadata"), dict):
                                    extra["metadata"] = ev["metadata"]
                                await self._emit(event_type, run_id, node_id, **extra)
                                last_activity = time.monotonic()
                                content = ev.get("content", "")
                                if isinstance(content, str):
                                    if "ASK_WORKER:" in content or "BROADCAST_TO_PEERS:" in content:
                                        await self._emit(
                                            "worker_message", run_id, node_id,
                                            content=content,
                                        )
                    finally:
                        monitor_task.cancel()
                        try:
                            await monitor_task
                        except asyncio.CancelledError:
                            pass

        except Exception:
            # If the runner already sent a terminal event (node_completed),
            # the connection drop is not a real failure — the runner finished.
            run_state = self._runs.get(run_id)
            terminal_events = run_state.get("_node_terminal_events", {}) if isinstance(run_state, dict) else {}
            terminal_event = terminal_events.get(node_id) if isinstance(terminal_events, dict) else None
            if terminal_event == "node_completed":
                logger.info(
                    "SSE connection lost for node %s but runner already completed — ignoring",
                    node_id,
                )
            else:
                logger.warning("SSE subscription error for node %s", node_id, exc_info=True)
                if not forced_failure_reason:
                    forced_failure_reason = f"SSE stream error for node {node_id}: runner connection lost"

        return forced_failure_reason

    async def _poll_stream_file(
        self,
        sandbox_id: str,
        stream_file: str,
        run_id: str,
        node_id: str,
        exec_id: str,
        cancel_event: asyncio.Event,
        idle_timeout_seconds: int,
        agent_type: str,
    ) -> str:
        """Legacy file-polling fallback when SSE is not available."""
        log_pos = 0
        poll_count = 0
        last_stream_activity = time.monotonic()
        last_heartbeat = 0.0
        idle_warnings_sent: set[int] = set()

        while not cancel_event.is_set():
            new_log_pos = await self._stream_log_lines(
                sandbox_id, stream_file, log_pos, run_id, node_id,
            )
            if new_log_pos != log_pos:
                last_stream_activity = time.monotonic()
            log_pos = new_log_pos

            terminal_events = self._runs.get(run_id, {}).get("_node_terminal_events", {})
            terminal_event = terminal_events.get(node_id) if isinstance(terminal_events, dict) else None

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
                break
            if not proc_info.running:
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
                # NOTE: do NOT reset last_stream_activity here!
                # In file-polling mode, heartbeats are self-generated by Python,
                # not from the model. Resetting would make timeout impossible.

            # Progressive warnings
            if idle_timeout_seconds > 0:
                pct = int(idle_seconds / idle_timeout_seconds * 100)
                for threshold in (50, 75, 90):
                    if pct >= threshold and threshold not in idle_warnings_sent:
                        idle_warnings_sent.add(threshold)
                        await self._emit(
                            "idle_warning", run_id, node_id,
                            content=f"节点已空闲 {idle_seconds}s，超过 {threshold}%",
                            idle_seconds=idle_seconds,
                            timeout_seconds=idle_timeout_seconds,
                            threshold_pct=threshold,
                        )

            if idle_timeout_seconds > 0 and idle_seconds >= idle_timeout_seconds:
                reason = f"node idle timeout after {idle_seconds}s without stream output"
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
                return reason

            await asyncio.sleep(1.0)

        return ""

    async def _execute_dynamic_plan(
        self,
        run_id: str,
        parent_node_id: str,
        parent_result: dict,
        global_config: dict,
        cancel_event: asyncio.Event,
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
                    # Preserve rich context fields from planner output
                    for rich_key in ("target_files", "interface_contract", "context_summary"):
                        val = nd.get(rich_key) or dag_node.get(rich_key)
                        if val:
                            task_entry[rich_key] = val
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
            from sqlalchemy import select as sa_select

            from app.models.db import SharedDocument as SharedDocModel
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
        from uuid import uuid4

        from app.core.database import async_session_factory
        from app.models.task import Task as TaskModel

        # Build worker node dicts and persist to DB in one pass
        worker_nodes: list[dict] = []
        task_db_map: dict[str, tuple] = {}  # node_id -> (task_id, label, type)
        auto_child_model_map = global_config.get("_auto_child_model_map", {})

        async with async_session_factory() as session:
            for idx, parsed in enumerate(parsed_tasks):
                child_node_id = parsed.get("node_id") or f"{parent_node_id}_child_{idx}"

                task_id = uuid4()
                task_type = parsed.get("type", "coder")
                worker_label = parsed.get("title") or f"{task_type} #{idx + 1}"

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
                task_db_map[child_node_id] = (str(task_id), worker_label, task_type)

            await session.commit()

        for idx, parsed in enumerate(parsed_tasks):
            child_node_id = parsed.get("node_id") or f"{parent_node_id}_child_{idx}"
            task_id, worker_label, task_type = task_db_map[child_node_id]

            await self._emit(
                "task_created", run_id, parent_node_id,
                task_id=task_id,
                task_title=parsed.get("title") or parsed.get("prompt", "")[:200],
                task_description=parsed.get("prompt", ""),
                status="pending",
                child_node_id=child_node_id,
                dependencies=parsed.get("depends_on", []) or parsed.get("dependencies", []) or "",
            )

            model_str = parsed.get("model", "")
            model_provider, model_id = _parse_full_model_id(model_str)

            strategy_provider, strategy_model_id = _resolve_auto_child_model(
                auto_child_model_map, str(task_type)
            )
            if not model_provider and strategy_provider:
                model_provider = strategy_provider
            if not model_id and strategy_model_id:
                model_id = strategy_model_id

            if not model_provider or not model_id:
                pass

            resolved_model = (
                f"{model_provider}/{model_id}"
                if model_provider and model_id
                else model_id
            )

            await self._emit(
                "child_created", run_id, parent_node_id,
                child_node_id=child_node_id,
                child_type=parsed.get("type", "coder"),
                child_prompt=parsed.get("prompt", ""),
                child_model=resolved_model,
            )

            worker_prompt = parsed.get("prompt", "")

            project_goal = global_config.get("_goal", "")
            if project_goal:
                worker_prompt = f"## 项目目标\n{project_goal}\n\n---\n\n{worker_prompt}"

            # Inject interface contract and context summary
            contract = parsed.get("interface_contract", "")
            if contract:
                worker_prompt = f"## 接口契约\n{contract}\n\n---\n\n{worker_prompt}"
            ctx = parsed.get("context_summary", "")
            if ctx:
                worker_prompt = f"## 上下文说明\n{ctx}\n\n---\n\n{worker_prompt}"

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

            if shared_doc_content:
                worker_prompt += f"\n\n## 项目共享文档\n{shared_doc_content}"

            worker_prompt += (
                f"\n\n---\nTask ID: {child_node_id}\n"
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
            # Carry rich context fields through to execution
            for rich_key in ("target_files", "interface_contract", "context_summary"):
                if parsed.get(rich_key):
                    worker_node[rich_key] = parsed[rich_key]
            worker_nodes.append(worker_node)

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

                child_results = await self._execute_layers(
                    run_id=run_id,
                    layers=sub_layers,
                    edges=child_edges_dag,
                    global_config=global_config,
                    cancel_event=cancel_event,
                    workspace_directory=workspace_directory,
                    task_db_map={node_id: meta[0] for node_id, meta in task_db_map.items()},
                    task_type_map={node_id: meta[2] for node_id, meta in task_db_map.items()},
                    task_label_map={node_id: meta[1] for node_id, meta in task_db_map.items()},
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

                return await self._execute_layers(
                    run_id=run_id,
                    layers=legacy_layers,
                    edges=legacy_edges,
                    global_config=global_config,
                    cancel_event=cancel_event,
                    workspace_directory=workspace_directory,
                    task_db_map={node_id: meta[0] for node_id, meta in task_db_map.items()},
                    task_type_map={node_id: meta[2] for node_id, meta in task_db_map.items()},
                    task_label_map={node_id: meta[1] for node_id, meta in task_db_map.items()},
                    parent_node_id=parent_node_id,
                )
