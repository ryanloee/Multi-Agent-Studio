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
from app.workflows.plan_parser import parse_plan_output, PLAN_SYSTEM_SUFFIX

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
    ) -> str:
        """Start DAG execution as a background asyncio task."""
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            self._execute_dag(run_id, layers, global_config or {}, cancel_event),
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
        }
        logger.info("DAG task created for run %s with %d layers", run_id, len(layers))
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

    async def _execute_dag(
        self,
        run_id: str,
        layers: list[dict],
        global_config: dict,
        cancel_event: asyncio.Event,
    ) -> None:
        """Core DAG execution loop: layers sequential, nodes parallel."""
        logger.info("DAG execution STARTED for run %s, %d layers", run_id, len(layers))
        try:
            await self._emit("run_started", run_id, "")
            layer_results: dict[str, Any] = {}

            for layer_idx, layer in enumerate(layers):
                if cancel_event.is_set():
                    break

                # Layer may be a list of node dicts directly (from the engine's
                # serialisation) or a dict with a "nodes" key.
                if isinstance(layer, dict):
                    nodes = layer.get("nodes", [])
                    if not nodes:
                        # The layer dict itself might be a single node
                        nodes = [layer]
                else:
                    nodes = layer

                logger.info(
                    "DAG run=%s layer %d: executing %d nodes",
                    run_id, layer_idx, len(nodes),
                )

                # Execute all nodes in this layer concurrently
                tasks = [
                    self._execute_node(run_id, node, layer_results, global_config, cancel_event)
                    for node in nodes
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for node, result in zip(nodes, results):
                    node_id = node.get("id", node.get("node_id", ""))
                    if isinstance(result, Exception):
                        import traceback
                        tb = "".join(traceback.format_exception(type(result), result, result.__traceback__)) if result.__traceback__ else str(result)
                        logger.error(
                            "DAG run=%s node %s failed: %s\n%s",
                            run_id, node_id, result, tb,
                        )
                        layer_results[node_id] = {
                            "state": "failed",
                            "error": str(result),
                        }
                    else:
                        layer_results[node_id] = result
                        # Plan node: parse output and execute dynamic children
                        _node_data = node.get("data", {})
                        _atype = (
                            node.get("agent_type")
                            or node.get("type")
                            or (_node_data.get("agentType") if isinstance(_node_data, dict) else None)
                        )
                        logger.info(
                            "DAG run=%s node %s: agent_type=%s, state=%s, has_raw_output=%s",
                            run_id, node_id, _atype, result.get("state"),
                            bool(result.get("raw_output")),
                        )
                        if (
                            _atype == "plan"
                            and result.get("state") == "completed"
                        ):
                            plan_results = await self._execute_dynamic_plan(
                                run_id, node_id, result, global_config, cancel_event,
                                planner_node=node,
                            )
                            layer_results.update(plan_results)

            status = "cancelled" if cancel_event.is_set() else "completed"
            self._runs[run_id]["status"] = status
            event_type = "run_completed" if status == "completed" else "run_failed"
            await self._emit(event_type, run_id, "", content=f"status={status}")

            # Update run status in DB so REST API polls reflect the true state
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

        except Exception as exc:
            logger.exception("DAG execution failed for run %s", run_id)
            self._runs[run_id]["status"] = "failed"
            await self._emit("run_failed", run_id, "", content=str(exc))

            # Update run status in DB on failure too
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
                        run_row.status = "failed"
                        await session.commit()
            except Exception:
                logger.warning("Failed to update run status in DB for %s", run_id, exc_info=True)

    async def _execute_node(
        self,
        run_id: str,
        node: dict,
        layer_results: dict[str, Any],
        global_config: dict,
        cancel_event: asyncio.Event,
    ) -> dict:
        """Execute a single DAG node: create sandbox, run agent, stream events."""
        node_id: str = node.get("id", node.get("node_id", ""))
        workspace_id = f"ws-{node_id}-{uuid4().hex[:8]}"
        subprocess_env = _build_subprocess_env()

        await self._emit("node_started", run_id, node_id)
        await self._emit("status", run_id, node_id, content="running")

        # 1. Create sandbox container
        sandbox_id = await self._sandbox.create(workspace_id)
        logger.info("Created sandbox %s for node %s", sandbox_id[:12], node_id)

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
            prompt: str = (
                node.get("prompt")
                or (data.get("prompt") if isinstance(data, dict) else "")
                or ""
            )

            if agent_type == "plan":
                prompt = prompt + PLAN_SYSTEM_SUFFIX

            # Resolve provider URL + API key from models.json
            from app.api.models import load_provider_config
            provider_cfg = load_provider_config().get(model_provider, {})
            provider_url = provider_cfg.get("url", "")
            provider_key = provider_cfg.get("key", "")

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
            # Always clean up the sandbox
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
        persist them as Task rows, and execute via the TaskScheduler.

        Returns a dict mapping child node_id -> result.
        """
        raw_output = parent_result.get("raw_output", "")
        if not raw_output:
            logger.warning("_execute_dynamic_plan: no raw_output for parent %s", parent_node_id)
            return {}

        # Extract LLM text from stream.jsonl — the parser needs plain text,
        # not raw JSONL event lines
        extracted_text = self._extract_llm_text(raw_output)
        logger.info(
            "_execute_dynamic_plan: extracted %d chars of LLM text from %d chars of raw output",
            len(extracted_text), len(raw_output),
        )

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

        managed_tasks: list = []
        child_results: dict[str, Any] = {}

        async with async_session_factory() as session:
            for idx, parsed in enumerate(parsed_tasks):
                child_node_id = f"{parent_node_id}_child_{idx}"

                # Create DB task
                task_id = uuid4()
                task_type = parsed.get("type", "coder")
                worker_label = f"{task_type} #{idx + 1}"
                db_task = TaskModel(
                    id=task_id,
                    run_id=uuid.UUID(run_id),
                    title=parsed.get("prompt", "")[:200],
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

                # Create managed task for the scheduler
                from app.core.task_scheduler import ManagedTask
                managed = ManagedTask(
                    db_id=str(task_id),
                    run_id=run_id,
                    title=db_task.title,
                    description=db_task.description,
                    status="pending",
                    assigned_node_id=child_node_id,
                )
                managed_tasks.append((managed, worker_node))

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
