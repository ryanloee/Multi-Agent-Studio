import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons (MVP: simple globals, injected at worker startup)
# ---------------------------------------------------------------------------
_sandbox_manager: "SandboxManager | None" = None
_checkpoint_manager: "GitCheckpointManager | None" = None
_provisioner: "SandboxProvisioner | None" = None
_stream_publisher: "StreamPublisher | None" = None

# Registry: exec_id -> {container_id, exec_id, node_id, ...}
_exec_registry: dict[str, dict] = {}


def init_dependencies(
    sandbox_manager: "SandboxManager",
    checkpoint_manager: "GitCheckpointManager",
    provisioner: "SandboxProvisioner",
    stream_publisher: "StreamPublisher",
) -> None:
    """Called once at worker startup to inject shared singletons."""
    global _sandbox_manager, _checkpoint_manager, _provisioner, _stream_publisher
    _sandbox_manager = sandbox_manager
    _checkpoint_manager = checkpoint_manager
    _provisioner = provisioner
    _stream_publisher = stream_publisher


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

@activity.defn
async def start_agent_task(node_config: dict) -> str:
    """Activity A: Create sandbox, provision, checkpoint, start OpenCode.

    Steps:
    1. Create an isolated sandbox container via SandboxManager.
    2. Provision the container (install OpenCode, inject config, init Git).
    3. Create a pre-execution Git checkpoint.
    4. Build the OpenCode CLI command and start it async.
    5. Record exec_id + container_id in _exec_registry.

    Returns:
        exec_id (str) for later status polling.
    """
    if _sandbox_manager is None:
        raise RuntimeError("SandboxManager not initialised – call init_dependencies() first")

    node_id: str = node_config.get("node_id", str(uuid4()))
    workspace_id = f"ws-{node_id}-{uuid4().hex[:8]}"

    # 1. Create sandbox container
    container_id = await _sandbox_manager.create(workspace_id)
    logger.info("Created sandbox %s for node %s", container_id[:12], node_id)

    # 2. Provision the container
    if _provisioner is not None:
        await _provisioner.provision(container_id, node_config)
    logger.info("Provisioned sandbox %s for node %s", container_id[:12], node_id)

    # 3. Pre-execution Git checkpoint
    commit_before: str | None = None
    if _checkpoint_manager is not None:
        commit_before = await _checkpoint_manager.auto_commit(
            container_id,
            message=f"before node [{node_id}]",
        )

    # 4. Build OpenCode command and start async
    from app.agents.opencode import build_opencode_command

    stream_file = "/workspace/.opencode/stream.jsonl"
    prompt = node_config.get("prompt", "")
    agent_type = node_config.get("agent_type", "build")
    model_provider = node_config.get("model_provider", "")
    model_id = node_config.get("model_id", "")

    cmd = build_opencode_command(
        agent=agent_type,
        prompt=prompt,
        log_path=stream_file,
        model_provider=model_provider,
        model_id=model_id,
    )

    exec_id = await _sandbox_manager.exec_async(container_id, cmd)
    logger.info("Started OpenCode exec %s in sandbox %s", exec_id[:12], container_id[:12])

    # 5. Record in registry
    _exec_registry[exec_id] = {
        "exec_id": exec_id,
        "container_id": container_id,
        "node_id": node_id,
        "workspace_id": workspace_id,
        "commit_before": commit_before,
        "state": "running",
    }

    return exec_id


@activity.defn
async def check_agent_status(exec_id: str) -> dict:
    """Activity B: Check if OpenCode process has completed.

    Looks up exec_id in _exec_registry, queries SandboxManager for process
    status, and if finished performs a post-execution auto_commit.

    Returns:
        dict with keys: state, exit_code, container_id, commit_after
    """
    if _sandbox_manager is None:
        raise RuntimeError("SandboxManager not initialised – call init_dependencies() first")

    entry = _exec_registry.get(exec_id)
    if entry is None:
        return {"state": "failed", "exit_code": -1, "error": f"exec_id {exec_id} not found"}

    container_id = entry["container_id"]
    node_id = entry["node_id"]

    # Check process status
    proc_info = await _sandbox_manager.get_process(exec_id)

    if proc_info.running:
        return {"state": "running", "exec_id": exec_id}

    # Process finished
    exit_code = proc_info.exit_code if proc_info.exit_code is not None else -1
    state = "completed" if exit_code == 0 else "failed"

    # Post-execution auto_commit
    commit_after: str | None = None
    if _checkpoint_manager is not None:
        try:
            commit_after = await _checkpoint_manager.auto_commit(
                container_id,
                message=f"after node [{node_id}] exit={exit_code}",
            )
        except Exception as exc:
            logger.warning("Post-execution auto_commit failed for %s: %s", exec_id[:12], exc)

    # Update registry
    entry["state"] = state
    entry["exit_code"] = exit_code
    entry["commit_after"] = commit_after

    return {
        "state": state,
        "exit_code": exit_code,
        "container_id": container_id,
        "node_id": node_id,
        "commit_before": entry.get("commit_before"),
        "commit_after": commit_after,
    }


# ---------------------------------------------------------------------------
# AgentNodeWorkflow — polling-based orchestrator
# ---------------------------------------------------------------------------

@workflow.defn
class AgentNodeWorkflow:
    """Orchestrates: start task -> poll status -> complete/fail.
    Uses async polling pattern to avoid blocking Temporal Activities."""

    @workflow.run
    async def run(self, node_config: dict) -> dict:
        # Step 1: Start task (Activity, retriable)
        exec_id = await workflow.execute_activity(
            start_agent_task,
            node_config,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # Step 2: Poll status (Workflow-level, survives Worker restart)
        while True:
            status = await workflow.execute_activity(
                check_agent_status,
                exec_id,
                start_to_close_timeout=timedelta(seconds=30),
            )
            if status["state"] in ("completed", "failed"):
                return status
            await workflow.sleep(timedelta(seconds=5))


# ---------------------------------------------------------------------------
# AgentNodeWorkflowV2 — signal-driven, zero polling
# ---------------------------------------------------------------------------

@workflow.defn
class AgentNodeWorkflowV2:
    """Phase 1 upgrade: Signal-driven, zero polling.
    Event History compressed from thousands to single digits."""

    def __init__(self):
        self._task_completed = False
        self._task_result = None

    @workflow.run
    async def run(self, node_config: dict) -> dict:
        exec_id = await workflow.execute_activity(
            start_agent_task,
            node_config,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # Zero polling: FileWatcher sends Signal when process exits
        await workflow.wait_condition(lambda: self._task_completed)
        return self._task_result

    @workflow.signal
    async def on_task_completed(self, result: dict):
        """Called by FileWatcher when OpenCode process exits."""
        self._task_result = result
        self._task_completed = True
