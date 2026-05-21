"""NodeRunner — executes a single agent node and returns structured results.

Extracted from the old LocalDAGExecutor so that the new DirectorLoop can
dispatch individual sub-agents without pulling in the entire DAG machinery.

Responsibilities:
  - Create/reuse sandbox
  - Provision workspace + git checkpoint
  - Run the Python agent loop (AgentRunner) directly
  - Collect raw output and return a NodeResult
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core import debug_logger as _dbg
from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)

_db_semaphore: asyncio.Semaphore | None = None


def _get_db_semaphore() -> asyncio.Semaphore:
    global _db_semaphore
    if _db_semaphore is None:
        _db_semaphore = asyncio.Semaphore(10)
    return _db_semaphore


@dataclass
class NodeResult:
    """Structured result from a single node execution."""

    state: str  # "completed" | "failed"
    exit_code: int = -1
    node_id: str = ""
    sandbox_id: str = ""
    exec_id: str = ""
    raw_output: str = ""
    result_summary: str = ""
    error: str = ""
    files_changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "exit_code": self.exit_code,
            "node_id": self.node_id,
            "sandbox_id": self.sandbox_id,
            "exec_id": self.exec_id,
            "raw_output": self.raw_output,
            "result_summary": self.result_summary,
            "error": self.error,
            "files_changed": self.files_changed,
        }


def _summarize(text: str, max_len: int = 2400) -> str:
    """Produce a compact summary of raw agent output."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, {len(text)} chars total)"




# ---------------------------------------------------------------------------
# Model config helpers (shared with old engine — will be deduped later)
# ---------------------------------------------------------------------------

_model_config_cache: dict[str, dict] = {}
_model_config_cache_time: float = 0
_MODEL_CONFIG_CACHE_TTL = 5.0


def _load_settings_models() -> list[dict[str, Any]]:
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
    result = [m for m in models if isinstance(m, dict) and m.get("enabled", True)]
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
        "thinking_mode": bool(entry.get("reasoning_passthrough", True)),
    }


def _load_default_model_config() -> dict[str, str | int]:
    models = _load_settings_models()
    if not models:
        return {}
    return _normalize_model_config(models[0])


def _load_model_config(model_provider: str, model_id: str) -> dict[str, str | int]:
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


class NodeRunner:
    """Execute a single agent node in a sandbox and return structured results."""

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
        # Maps "run_id:node_id" -> host path of stream.jsonl for active nodes
        self._stream_files: dict[str, str] = {}

    async def execute_node(
        self,
        run_id: str,
        node_id: str,
        agent_type: str,
        prompt: str,
        sandbox_id: str | None = None,
        workspace_directory: str | None = None,
        global_config: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        model_provider: str = "",
        model_id: str = "",
        destroy_sandbox: bool = True,
        enable_self_review: bool = False,
    ) -> NodeResult:
        """Execute one agent, return structured result.

        If *sandbox_id* is provided the sandbox is reused (trunk-based);
        otherwise a fresh one is created from *workspace_directory*.

        Each node gets an isolated container under .mas/containers/{node_id}/.
        After completion, changes are committed and merged to the main workspace.
        """
        _dbg.info(
            __name__,
            "execute_node starting",
            node_id=node_id,
            agent_type=agent_type,
            model_provider=model_provider,
            model_id=model_id,
            sandbox_id=(sandbox_id or "")[:12],
        )
        if cancel_event is None:
            cancel_event = asyncio.Event()
        global_config = global_config or {}

        await self._emit("node_started", run_id, node_id)
        await self._emit("status", run_id, node_id, content="running")

        _owns_sandbox = False
        if sandbox_id is None:
            workspace_id = f"ws-{node_id}-{uuid4().hex[:8]}"
            sandbox_id = await self._sandbox.create(
                workspace_id,
                template_dir=workspace_directory,
                user_workspace=workspace_directory,
            )
            _owns_sandbox = True
            logger.info("Created sandbox %s for node %s", sandbox_id[:12], node_id)
        else:
            logger.info("Reusing sandbox %s for node %s", sandbox_id[:12], node_id)

        # Node container path: .mas/containers/{node_id}
        node_container = f"/workspace/.mas/containers/{node_id}"
        node_agent_dir = f"{node_container}/.agent"
        stream_file = f"{node_agent_dir}/stream.jsonl"

        try:
            # Create node container directory structure (Python, no shell)
            node_agent_host = self._sandbox.resolve_virtual_path(sandbox_id, node_agent_dir)
            Path(node_agent_host).mkdir(parents=True, exist_ok=True)
            stream_host = self._sandbox.resolve_virtual_path(sandbox_id, stream_file)
            Path(stream_host).write_text("", encoding="utf-8")
            # Register stream file for _emit to write events
            _stream_key = f"{run_id}:{node_id}"
            self._stream_files[_stream_key] = stream_host
            logger.info("Created node container %s for node %s", node_container, node_id)

            try:
                await self._checkpoint.auto_commit(sandbox_id, message=f"before node [{node_id}]")
            except Exception:
                pass

            # Resolve model config
            if not model_provider:
                default_cfg = _load_default_model_config()
                model_provider = str(default_cfg.get("provider", ""))
            if not model_id:
                default_cfg = _load_default_model_config()
                model_id = str(default_cfg.get("model", ""))

            if not model_provider or not model_id:
                return NodeResult(
                    state="failed",
                    node_id=node_id,
                    sandbox_id=sandbox_id,
                    error="No model configured. Please configure a model in settings.",
                )

            model_cfg = _load_model_config(model_provider, model_id)

            # Resolve provider URL + API key
            from app.api.models import load_provider_config
            provider_cfg = load_provider_config().get(model_provider, {})
            provider_url = str(model_cfg.get("url", ""))
            provider_key = str(model_cfg.get("key", ""))
            provider_url = provider_url or provider_cfg.get("url", "")
            provider_key = provider_key or provider_cfg.get("key", "")

            if not provider_url or not provider_key:
                for _pid, _pcfg in load_provider_config().items():
                    if _pcfg.get("url") and _pcfg.get("key"):
                        provider_url = provider_url or _pcfg["url"]
                        provider_key = provider_key or _pcfg["key"]
                        break

            if not provider_url or not provider_key:
                provider_url = provider_url or os.environ.get("MIMO_API_URL", "")
                provider_key = provider_key or os.environ.get("MIMO_API_KEY", "")

            context_window = int(model_cfg.get("context_window") or 128000)
            max_output_tokens = int(model_cfg.get("max_output_tokens") or 4096)

            _dbg.debug(__name__, "Model config resolved", node_id=node_id,
                       provider_url=provider_url, model_provider=model_provider,
                       model_id=model_id, context_window=context_window,
                       max_output_tokens=max_output_tokens)

            # Resolve native workspace path for agent runner
            native_workspace = self._sandbox.resolve_virtual_path(sandbox_id, node_container)

            # Build model config for agent runner
            agent_model_config = {
                "provider": model_provider,
                "model": model_id,
                "url": provider_url,
                "key": provider_key,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                "thinking_mode": bool(model_cfg.get("reasoning_passthrough", True)),
            }

            # Run agent loop directly (replaces Bun subprocess)
            from app.core.agent_runner import AgentRunner
            runner = AgentRunner()
            try:
                agent_result = await runner.run(
                    prompt=prompt,
                    model_config=agent_model_config,
                    agent_type=agent_type,
                    workspace=native_workspace,
                    emit=self._emit,
                    run_id=run_id,
                    node_id=node_id,
                    cancel_event=cancel_event,
                    enable_self_review=enable_self_review,
                )
            finally:
                await runner.close()

            state = "completed" if agent_result.success else "failed"
            _dbg.log_node_lifecycle(
                __name__, node_id=node_id, agent_type=agent_type, event=state,
                exit_code=0 if agent_result.success else 1,
                error=agent_result.error[:500] if agent_result.error else "",
            )

            result = NodeResult(
                state=state,
                exit_code=0 if agent_result.success else 1,
                node_id=node_id,
                sandbox_id=sandbox_id,
            )
            result.raw_output = agent_result.output
            result.error = agent_result.error
            result.files_changed = agent_result.files_changed
            result.result_summary = (
                _summarize(agent_result.output) if agent_result.output else agent_result.error
            )

            # Commit node changes and merge to main workspace if successful
            if state == "completed":
                try:
                    # Commit changes in the node container
                    commit_hash = await self._checkpoint.commit_node_changes(
                        sandbox_id, node_id,
                        message=f"node {node_id} completed"
                    )
                    logger.info("Committed node %s changes: %s", node_id, commit_hash[:12])

                    # Merge node changes to main workspace
                    merged = await self._checkpoint.merge_node_to_main(sandbox_id, node_id)
                    if merged:
                        logger.info("Merged node %s changes to main workspace", node_id)
                    else:
                        logger.warning("Failed to merge node %s changes", node_id)
                except Exception as exc:
                    logger.warning("Git sync failed for node %s: %s", node_id, exc)

            return result

        finally:
            # Unregister stream file
            self._stream_files.pop(f"{run_id}:{node_id}", None)

            if workspace_directory:
                try:
                    await self._sandbox.sync_back(sandbox_id, workspace_directory)
                except Exception:
                    logger.warning(
                        "sync_back failed for sandbox %s",
                        sandbox_id[:12],
                        exc_info=True,
                    )

            if _owns_sandbox and destroy_sandbox:
                try:
                    await self._sandbox.destroy(sandbox_id)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, run_id: str, node_id: str, **extra: Any) -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "run_id": run_id,
            "node_id": node_id,
            "timestamp": time.time(),
            **extra,
        }
        await self._persist_run_event(event)

        # Append event to stream.jsonl (JSONL format, one JSON per line)
        _stream_key = f"{run_id}:{node_id}"
        stream_path = self._stream_files.get(_stream_key)
        if stream_path:
            try:
                with open(stream_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                logger.debug("Failed to write to stream file %s", stream_path, exc_info=True)

        channel = f"run:{run_id}:stream"
        try:
            await self._event_bus.publish(channel, event)
        except Exception:
            logger.warning("Failed to publish event %s", event_type, exc_info=True)

    async def _persist_run_event(self, event: dict[str, Any]) -> None:
        async with _get_db_semaphore():
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
                logger.warning("Failed to persist run event %s", event.get("type"), exc_info=True)
