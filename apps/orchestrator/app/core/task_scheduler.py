"""Dynamic task scheduler — replaces rigid DAG execution with event-driven
task assignment.

Core flow:
  1. Planner node executes → output parsed into structured tasks
  2. Tasks are stored in DB and published as events
  3. Scheduler assigns pending tasks to idle workers
  4. Workers can escalate questions back to the Planner
  5. Scheduler monitors completion and re-plans if needed
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.local_bus import InProcessEventBus
from app.core.local_sandbox import LocalSandbox
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Escalation protocol markers injected into worker prompts
# ---------------------------------------------------------------------------

ESCALATION_MARKER = "ESCALATE_TO_PLANNER:"
PROGRESS_MARKER = "TASK_PROGRESS:"


@dataclass
class ManagedTask:
    """In-memory representation of a task being managed by the scheduler."""
    db_id: str
    run_id: str
    title: str
    description: str
    status: str = "pending"  # pending | assigned | running | blocked | completed | failed
    assigned_node_id: str | None = None
    assigned_worker_label: str | None = None
    worker_task: asyncio.Task | None = None
    result_summary: str = ""
    progress: int = 0


class TaskScheduler:
    """Event-driven task scheduler.

    Coordinates with LocalDAGExecutor for node execution and the DB for
    persistent task state.
    """

    def __init__(
        self,
        sandbox: LocalSandbox,
        event_bus: InProcessEventBus,
        checkpoint: GitCheckpointManager,
        provisioner: SandboxProvisioner,
        # Callbacks provided by the engine
        execute_node_fn=None,
        emit_fn=None,
        get_workflow_nodes_fn=None,
    ):
        self._sandbox = sandbox
        self._event_bus = event_bus
        self._checkpoint = checkpoint
        self._provisioner = provisioner
        self._execute_node = execute_node_fn
        self._emit = emit_fn
        self._get_workflow_nodes = get_workflow_nodes_fn

    # ------------------------------------------------------------------
    # Task state machine
    # ------------------------------------------------------------------

    async def update_task_status(
        self, task: ManagedTask, status: str, db_session=None, **kwargs
    ) -> None:
        """Update task status and persist to DB if session provided."""
        task.status = status
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)

        if db_session:
            try:
                import uuid as _uuid
                from app.core.database import async_session_factory
                from app.models.task import Task as TaskModel
                from sqlalchemy import select
                async with async_session_factory() as session:
                    result = await session.execute(
                        select(TaskModel).where(TaskModel.id == _uuid.UUID(task.db_id))
                    )
                    row = result.scalar_one_or_none()
                    if row:
                        row.status = status
                        if "progress" in kwargs:
                            row.progress = kwargs["progress"]
                        if "result_summary" in kwargs:
                            row.result_summary = kwargs["result_summary"]
                        if "assigned_node_id" in kwargs:
                            row.assigned_node_id = kwargs["assigned_node_id"]
                        if "assigned_worker_label" in kwargs:
                            row.assigned_worker_label = kwargs["assigned_worker_label"]
                        await session.commit()
            except Exception:
                logger.warning("Failed to update task %s in DB", task.db_id, exc_info=True)

        # Emit task_updated event
        await self._emit(
            "task_updated", task.run_id, "",
            task_id=task.db_id,
            task_title=task.title,
            status=task.status,
            progress=task.progress,
            assigned_node_id=task.assigned_node_id or "",
            assigned_worker_label=task.assigned_worker_label or "",
            result_summary=task.result_summary,
        )

    # ------------------------------------------------------------------
    # Worker execution with escalation support
    # ------------------------------------------------------------------

    async def run_worker_task(
        self,
        worker_node: dict,
        task: ManagedTask,
        global_config: dict,
        cancel_event: asyncio.Event,
        db_session=None,
        planner_node: dict | None = None,
        max_escalations: int = 3,
    ) -> dict:
        """Execute a worker node for a given task, handling escalations.

        The worker prompt is augmented with:
        - Task context (title + description)
        - Escalation protocol (ESCALATE_TO_PLANNER: marker)
        - Progress reporting (TASK_PROGRESS: marker)

        If the worker emits ESCALATE_TO_PLANNER:, the scheduler pauses
        the task, asks the Planner for guidance, and resumes the worker
        with the answer.
        """
        escalation_count = 0

        while escalation_count < max_escalations:
            if cancel_event.is_set():
                await self.update_task_status(task, "failed", db_session)
                return {"state": "failed", "error": "cancelled"}

            # Mark running
            if task.status != "running":
                await self.update_task_status(
                    task, "running", db_session,
                    assigned_node_id=worker_node.get("id", ""),
                    assigned_worker_label=self._worker_label(worker_node),
                )

            # Execute the node
            layer_results: dict[str, Any] = {}
            result = await self._execute_node(
                task.run_id, worker_node, layer_results, global_config, cancel_event,
            )

            state = result.get("state", "failed")
            raw_output = result.get("raw_output", "")

            logger.info(
                "Worker %s execution result: state=%s, exit_code=%s, error=%s",
                worker_node.get("id"), state,
                result.get("exit_code"), result.get("error", "")[:200],
            )

            # Parse escalation marker from output
            escalation_question = self._extract_escalation(raw_output)
            progress_pct = self._extract_progress(raw_output)

            if progress_pct is not None:
                await self.update_task_status(
                    task, task.status, db_session, progress=progress_pct,
                )

            if escalation_question and planner_node:
                escalation_count += 1
                logger.info(
                    "Worker %s escalated (count=%d/%d): %s",
                    worker_node.get("id"), escalation_count, max_escalations,
                    escalation_question[:100],
                )

                # Record escalation message
                await self._record_message(
                    task, "worker", worker_node.get("id", ""),
                    "escalation", escalation_question, db_session,
                )

                # Mark blocked
                await self.update_task_status(task, "blocked", db_session)

                # Ask planner
                answer = await self._planner_answer(
                    escalation_question, task, planner_node,
                    task.run_id, global_config, cancel_event,
                )

                # Record planner answer
                await self._record_message(
                    task, "planner", "planner",
                    "answer", answer, db_session,
                )

                # Resume — continue the loop
                continue

            # No escalation — task is done
            if state == "completed":
                await self.update_task_status(
                    task, "completed", db_session,
                    progress=100, result_summary=self._summarize(raw_output),
                )
            else:
                await self.update_task_status(
                    task, "failed", db_session,
                    result_summary=result.get("error", "execution failed"),
                )

            return result

        # Exceeded max escalations
        await self.update_task_status(
            task, "failed", db_session,
            result_summary=f"Exceeded maximum escalation limit ({max_escalations})",
        )
        return {"state": "failed", "error": "max_escalations_exceeded"}

    # ------------------------------------------------------------------
    # Planner interaction for escalation answers
    # ------------------------------------------------------------------

    async def _planner_answer(
        self,
        question: str,
        task: ManagedTask,
        planner_node: dict,
        run_id: str,
        global_config: dict,
        cancel_event: asyncio.Event,
    ) -> str:
        """Ask the planner a question and return its text response.

        Creates a temporary worker-style execution with a focused prompt
        so the planner can answer without re-doing its full plan.
        """
        escalation_prompt = (
            f"A worker has escalated a question about task: {task.title}\n\n"
            f"Worker question: {question}\n\n"
            f"Task context: {task.description}\n\n"
            f"Please provide a concise answer to help the worker continue."
        )

        # Build a temporary node for the planner to answer
        answer_node = {
            "id": f"{planner_node.get('id', 'planner')}_answer_{task.db_id}",
            "agent_type": "coder",  # Use coder for quick answers (lighter)
            "model_provider": planner_node.get("model_provider", "")
            or (planner_node.get("data", {}).get("modelProvider", "")
                if isinstance(planner_node.get("data"), dict) else ""),
            "model_id": planner_node.get("model_id", "")
            or (planner_node.get("data", {}).get("modelId", "")
                if isinstance(planner_node.get("data"), dict) else ""),
            "prompt": escalation_prompt,
        }

        result = await self._execute_node(
            run_id, answer_node, {}, global_config, cancel_event,
        )

        # Extract text from the answer
        raw = result.get("raw_output", "")
        return self._extract_answer_text(raw) or result.get("error", "No response from planner")

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_escalation(raw_output: str) -> str | None:
        """Check if worker output contains an escalation marker."""
        if not raw_output:
            return None
        for line in raw_output.split("\n"):
            line = line.strip()
            if ESCALATION_MARKER in line:
                idx = line.index(ESCALATION_MARKER)
                question = line[idx + len(ESCALATION_MARKER):].strip()
                if question:
                    return question
        return None

    @staticmethod
    def _extract_progress(raw_output: str) -> int | None:
        """Check if worker output contains a progress marker."""
        if not raw_output:
            return None
        import re
        pattern = rf"{re.escape(PROGRESS_MARKER)}\s*(\d{{1,3}})"
        for line in raw_output.split("\n"):
            m = re.search(pattern, line.strip())
            if m:
                pct = min(100, max(0, int(m.group(1))))
                return pct
        return None

    @staticmethod
    def _extract_answer_text(raw_output: str) -> str:
        """Extract plain text from stream.jsonl for planner answers."""
        import json
        parts: list[str] = []
        for line in raw_output.strip().split("\n"):
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

    @staticmethod
    def _summarize(raw_output: str, max_len: int = 200) -> str:
        """Create a short summary from raw output."""
        text = TaskScheduler._extract_answer_text(raw_output)
        text = text.strip()
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    @staticmethod
    def _worker_label(node: dict) -> str:
        """Get a human-readable label for a worker node."""
        data = node.get("data", {})
        if isinstance(data, dict):
            return data.get("label", "") or node.get("id", "worker")
        return node.get("id", "worker")

    async def _record_message(
        self,
        task: ManagedTask,
        sender_type: str,
        sender_id: str,
        message_type: str,
        content: str,
        db_session=None,
    ) -> None:
        """Record a task message in the DB and emit an event."""
        if db_session:
            try:
                import uuid as _uuid
                from app.core.database import async_session_factory
                from app.models.task import TaskMessage as TaskMessageModel
                from uuid import uuid4
                async with async_session_factory() as session:
                    msg = TaskMessageModel(
                        id=uuid4(),
                        task_id=_uuid.UUID(task.db_id),
                        sender_type=sender_type,
                        sender_id=sender_id,
                        message_type=message_type,
                        content=content,
                    )
                    session.add(msg)
                    await session.commit()
            except Exception:
                logger.warning("Failed to record task message", exc_info=True)

        await self._emit(
            "task_message", task.run_id, "",
            task_id=task.db_id,
            sender_type=sender_type,
            sender_id=sender_id,
            message_type=message_type,
            content=content,
        )
