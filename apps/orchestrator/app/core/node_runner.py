"""NodeRunner — executes a single agent node and returns structured results.

Extracted from the old LocalDAGExecutor so that the new DirectorLoop can
dispatch individual sub-agents without pulling in the entire DAG machinery.

Responsibilities:
  - Create/reuse sandbox
  - Provision workspace + git checkpoint
  - Launch the opencode runner subprocess
  - Stream events via SSE or file polling
  - Collect raw output and return a NodeResult
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)

import app.core.debug_logger as _dbg

_repo_root = Path(__file__).resolve().parents[4]
_OPENCODE_RUNNER = _repo_root / "apps" / "opencode-runner" / "run-node.ts"
_OPENCODE_PACKAGE_DIR = Path(
    os.environ.get(
        "MAS_OPENCODE_PACKAGE_DIR",
        str(_repo_root / "apps" / "opencode-runner" / "vendor" / "opencode" / "packages" / "opencode"),
    )
)
_OPENCODE_SOURCE_ENTRY = Path(
    os.environ.get(
        "MAS_OPENCODE_SOURCE_ENTRY",
        str(_OPENCODE_PACKAGE_DIR / "src" / "index.ts"),
    )
)

_KNOWN_EVENT_TYPES = frozenset({
    "llm_token", "llm_chunk", "tool_call", "tool_result", "shell_stdout",
    "shell_stderr", "status", "error", "node_started", "node_completed",
    "node_failed", "child_created", "child_completed",
    "task_created", "task_updated", "task_message",
    "artifact_created", "worker_message",
    "idle_warning", "agent_status",
})

_NODE_IDLE_TIMEOUT: dict[str, int] = {
    "explore": 600, "scout": 600, "plan": 600, "design": 600,
    "coder": 480, "worker": 480, "shell": 300, "tester": 300,
    "review": 300, "human": 0, "merge": 300,
}
_DEFAULT_IDLE_TIMEOUT = 480

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


def _build_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["OPENCODE_PACKAGE_DIR"] = str(_OPENCODE_PACKAGE_DIR)
    env["OPENCODE_SOURCE_ENTRY"] = str(_OPENCODE_SOURCE_ENTRY)
    env.setdefault("OPENCODE_DISABLE_MODELS_FETCH", "1")
    return env


def _summarize(text: str, max_len: int = 2400) -> str:
    """Produce a compact summary of raw agent output."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, {len(text)} chars total)"


def _extract_llm_text(jsonl_content: str) -> str:
    """Extract plain LLM text from stream.jsonl event lines."""
    parts: list[str] = []
    for line in jsonl_content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type", "") in ("llm_token", "llm_chunk", "text"):
            parts.append(ev.get("content", ""))
    return "".join(parts)


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
    ) -> NodeResult:
        """Execute one agent, return structured result.

        If *sandbox_id* is provided the sandbox is reused (trunk-based);
        otherwise a fresh one is created from *workspace_directory*.
        """
        _dbg.info(__name__, "execute_node starting", node_id=node_id, agent_type=agent_type,
                  model_provider=model_provider, model_id=model_id, sandbox_id=(sandbox_id or "")[:12])
        if cancel_event is None:
            cancel_event = asyncio.Event()
        global_config = global_config or {}
        subprocess_env = _build_subprocess_env()

        await self._emit("node_started", run_id, node_id)
        await self._emit("status", run_id, node_id, content="running")

        _owns_sandbox = False
        if sandbox_id is None:
            workspace_id = f"ws-{node_id}-{uuid4().hex[:8]}"
            sandbox_id = await self._sandbox.create(
                workspace_id,
                template_dir=workspace_directory,
            )
            _owns_sandbox = True
            logger.info("Created sandbox %s for node %s", sandbox_id[:12], node_id)
        else:
            logger.info("Reusing sandbox %s for node %s", sandbox_id[:12], node_id)
            await self._sandbox.exec(
                sandbox_id,
                "mkdir -p /workspace/.agent && : > /workspace/.agent/stream.jsonl",
            )

        stream_file = "/workspace/.agent/stream.jsonl"

        try:
            try:
                await self._provisioner.provision(sandbox_id, {"agent_type": agent_type})
            except Exception as exc:
                logger.warning("Provisioning failed for %s: %s", node_id, exc)

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

            _dbg.debug(__name__, "Model config resolved", node_id=node_id,
                       provider_url=provider_url, model_provider=model_provider,
                       model_id=model_id, context_window=context_window,
                       max_output_tokens=max_output_tokens)

            if not provider_url or not provider_key:
                provider_url = provider_url or os.environ.get("MIMO_API_URL", "")
                provider_key = provider_key or os.environ.get("MIMO_API_KEY", "")

            context_window = int(model_cfg.get("context_window") or 128000)
            max_output_tokens = int(model_cfg.get("max_output_tokens") or 4096)

            # Write prompt
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

            # Launch
            exec_id = await self._sandbox.exec_async(sandbox_id, cmd, env=runner_env)
            logger.info("Started runner exec %s in sandbox %s", exec_id[:12], sandbox_id[:12])

            # Stream events
            idle_timeout = _NODE_IDLE_TIMEOUT.get(agent_type, _DEFAULT_IDLE_TIMEOUT)
            env_timeout = os.environ.get("MAS_NODE_IDLE_TIMEOUT_SECONDS")
            if env_timeout:
                idle_timeout = int(env_timeout)
            forced_failure_reason = ""

            sse_port = await self._read_runner_port(sandbox_id, timeout=15)
            if sse_port:
                forced_failure_reason = await self._consume_runner_sse(
                    sse_port, run_id, node_id, exec_id, cancel_event, idle_timeout,
                )
                if forced_failure_reason:
                    forced_failure_reason = await self._poll_stream_file(
                        sandbox_id, stream_file, run_id, node_id, exec_id,
                        cancel_event, idle_timeout, agent_type,
                    )
            else:
                forced_failure_reason = await self._poll_stream_file(
                    sandbox_id, stream_file, run_id, node_id, exec_id,
                    cancel_event, idle_timeout, agent_type,
                )

            try:
                exit_code = await asyncio.wait_for(
                    self._sandbox.wait_process(exec_id), timeout=30,
                )
            except asyncio.TimeoutError:
                proc_info = await self._sandbox.get_process(exec_id)
                exit_code = proc_info.exit_code if proc_info.exit_code is not None else -1

            # Final read
            await self._stream_log_lines(sandbox_id, stream_file, 0, run_id, node_id)

            if forced_failure_reason and exit_code == 0:
                forced_failure_reason = ""

            state = "failed" if (forced_failure_reason or exit_code != 0) else "completed"
            _dbg.log_node_lifecycle(
                __name__, node_id=node_id, agent_type=agent_type, event=state,
                exit_code=exit_code, error=(forced_failure_reason or "")[:500],
            )

            await self._emit(
                "node_completed" if state == "completed" else "node_failed",
                run_id, node_id, content=f"exit_code={exit_code}",
            )
            await self._emit("status", run_id, node_id, content=state)

            result = NodeResult(
                state=state,
                exit_code=exit_code,
                node_id=node_id,
                exec_id=exec_id,
                sandbox_id=sandbox_id,
            )
            if forced_failure_reason:
                result.error = forced_failure_reason

            if state == "failed":
                try:
                    shim = self._sandbox._find_process(exec_id)
                    if shim and shim._proc.stderr:
                        stderr_text = await asyncio.to_thread(
                            lambda: shim._proc.stderr.read().decode("utf-8", errors="replace")[:2000]
                        )
                        if stderr_text.strip():
                            result.error = stderr_text.strip()
                except Exception:
                    pass

            # Capture raw output
            try:
                raw_log, _ = await self._sandbox.exec(
                    sandbox_id, f"cat {stream_file} 2>/dev/null || true",
                    env=subprocess_env,
                )
                result.raw_output = raw_log
            except Exception:
                pass

            result.result_summary = _summarize(result.raw_output) if result.raw_output else result.error
            return result

        finally:
            if workspace_directory:
                try:
                    await self._sandbox.sync_back(sandbox_id, workspace_directory)
                except Exception:
                    logger.warning("sync_back failed for sandbox %s", sandbox_id[:12], exc_info=True)

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

    # ------------------------------------------------------------------
    # SSE + file polling (adapted from local_engine.py)
    # ------------------------------------------------------------------

    async def _read_runner_port(self, sandbox_id: str, timeout: int = 15) -> int | None:
        port_path: Path | None = None
        try:
            state = self._sandbox._state(sandbox_id)
            port_path = state.workspace_dir / ".agent" / "runner.port"
        except KeyError:
            pass
        except Exception:
            pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port_path is not None:
                try:
                    content = await asyncio.to_thread(lambda: port_path.read_text().strip())
                    if content and content.isdigit():
                        return int(content)
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
            try:
                content, _ = await self._sandbox.exec(
                    sandbox_id, "cat /workspace/.agent/runner.port 2>/dev/null || true",
                )
                if content.strip().isdigit():
                    return int(content.strip())
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return None

    async def _consume_runner_sse(
        self, port: int, run_id: str, node_id: str, exec_id: str,
        cancel_event: asyncio.Event, idle_timeout_seconds: int,
    ) -> str:
        import httpx

        url = f"http://127.0.0.1:{port}/events"
        forced_failure_reason = ""
        last_activity = time.monotonic()
        last_busy_time: float | None = None
        idle_warnings_sent: set[int] = set()
        hard_timeout = idle_timeout_seconds * 2 if idle_timeout_seconds > 0 else 0

        async def _idle_monitor() -> str:
            nonlocal last_activity, last_busy_time, forced_failure_reason
            while True:
                await asyncio.sleep(5)
                if cancel_event.is_set():
                    break
                now = time.monotonic()
                idle_seconds = int(now - last_activity)
                if last_busy_time is not None and (now - last_busy_time) < idle_timeout_seconds:
                    if hard_timeout > 0 and (now - last_busy_time) > hard_timeout:
                        last_busy_time = None
                    continue
                if hard_timeout > 0 and idle_seconds >= hard_timeout:
                    forced_failure_reason = f"node hard timeout after {idle_seconds}s"
                    self._kill_process(exec_id)
                    return forced_failure_reason
                if idle_timeout_seconds > 0 and idle_seconds >= idle_timeout_seconds:
                    forced_failure_reason = f"node idle timeout after {idle_seconds}s"
                    self._kill_process(exec_id)
                    return forced_failure_reason
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
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5, read=None, write=5, pool=5),
                trust_env=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
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
                            elif event_type == "agent_status":
                                last_activity = time.monotonic()
                                if ev.get("status_type") == "busy":
                                    last_busy_time = time.monotonic()
                                await self._emit(
                                    "agent_status", run_id, node_id,
                                    content=ev.get("content", ""),
                                    status_type=ev.get("status_type", ""),
                                )
                    finally:
                        monitor_task.cancel()
                        try:
                            await monitor_task
                        except asyncio.CancelledError:
                            pass
        except Exception:
            logger.warning("SSE subscription error for node %s", node_id, exc_info=True)
            if not forced_failure_reason:
                forced_failure_reason = f"SSE stream error for node {node_id}"

        return forced_failure_reason

    async def _poll_stream_file(
        self, sandbox_id: str, stream_file: str, run_id: str, node_id: str,
        exec_id: str, cancel_event: asyncio.Event, idle_timeout_seconds: int,
        agent_type: str,
    ) -> str:
        log_pos = 0
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

            proc_info = await self._sandbox.get_process(exec_id)
            if not proc_info.running:
                break

            now = time.monotonic()
            idle_seconds = int(now - last_stream_activity)

            if idle_seconds >= 20 and now - last_heartbeat >= 20:
                await self._emit(
                    "agent_heartbeat", run_id, node_id,
                    content=f"节点仍在运行，等待模型或工具输出 {idle_seconds}s",
                    idle_seconds=idle_seconds,
                )
                last_heartbeat = now

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
                self._kill_process(exec_id)
                return f"node idle timeout after {idle_seconds}s"

            await asyncio.sleep(1.0)
        return ""

    async def _stream_log_lines(
        self, sandbox_id: str, stream_file: str, start_pos: int,
        run_id: str, node_id: str,
    ) -> int:
        try:
            if start_pos == 0:
                log_content, _ = await self._sandbox.exec(
                    sandbox_id, f"cat {stream_file} 2>/dev/null || true",
                )
            else:
                log_content, _ = await self._sandbox.exec(
                    sandbox_id, f"tail -c +{start_pos + 1} {stream_file} 2>/dev/null || true",
                )
        except Exception:
            return start_pos

        if len(log_content) <= start_pos:
            return start_pos

        for line in log_content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                event_type = ev.get("type", "")
                if event_type in _KNOWN_EVENT_TYPES:
                    extra: dict[str, Any] = {
                        "content": ev.get("content", ""),
                        "tool_name": ev.get("tool_name", ""),
                        "timestamp": ev.get("timestamp", 0),
                    }
                    if isinstance(ev.get("metadata"), dict):
                        extra["metadata"] = ev["metadata"]
                    await self._emit(event_type, run_id, node_id, **extra)
                elif event_type == "text":
                    await self._emit("llm_token", run_id, node_id, content=ev.get("content", ""))
                elif event_type:
                    extra = {k: v for k, v in ev.items() if k not in {"type", "run_id", "node_id"}}
                    await self._emit(event_type, run_id, node_id, **extra)
            except json.JSONDecodeError:
                await self._emit("shell_stdout", run_id, node_id, content=line)

        return len(log_content)

    def _kill_process(self, exec_id: str) -> None:
        try:
            shim = self._sandbox._find_process(exec_id)
            if shim is not None:
                try:
                    shim.terminate()
                except Exception:
                    try:
                        shim.kill()
                    except Exception:
                        pass
        except Exception:
            pass
