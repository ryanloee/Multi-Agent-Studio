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
ASK_WORKER_MARKER = "ASK_WORKER:"
BROADCAST_MARKER = "BROADCAST_TO_PEERS:"


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
        layer_results: dict[str, Any] | None = None,
        workspace_directory: str | None = None,
        upstream_context: str = "",
        sandbox_id: str | None = None,
        destroy_owned_sandbox: bool = True,
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

            runtime_context = await self._build_runtime_context(
                task.run_id, worker_node.get("id", ""),
            )
            execution_node = dict(worker_node)
            extra_context = upstream_context + runtime_context
            if extra_context:
                execution_node["prompt"] = worker_node.get("prompt", "") + extra_context

            # Execute the node
            shared_layer_results = layer_results if layer_results is not None else {}
            result = await self._execute_node(
                task.run_id, execution_node, shared_layer_results, global_config, cancel_event,
                workspace_directory=workspace_directory,
                upstream_context="",
                sandbox_id=sandbox_id,
                destroy_owned_sandbox=destroy_owned_sandbox,
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
                    "planner_question", escalation_question, db_session,
                )

                # Mark blocked
                await self.update_task_status(task, "blocked", db_session)
                await self._emit(
                    "task_blocked", task.run_id, worker_node.get("id", ""),
                    task_id=task.db_id,
                    content=escalation_question,
                )

                # Ask planner
                answer = await self._planner_answer(
                    escalation_question, task, planner_node,
                    task.run_id, global_config, cancel_event,
                    workspace_directory=workspace_directory,
                )

                # Record planner answer
                await self._record_message(
                    task, "planner", "planner",
                    "planner_answer", answer, db_session,
                )
                await self._emit(
                    "planner_guidance", task.run_id, worker_node.get("id", ""),
                    task_id=task.db_id,
                    content=answer,
                )
                await self.update_task_status(task, "running", db_session)
                await self._emit(
                    "task_unblocked", task.run_id, worker_node.get("id", ""),
                    task_id=task.db_id,
                    content="Planner guidance received",
                )
                worker_node = {
                    **worker_node,
                    "prompt": (
                        worker_node.get("prompt", "")
                        + "\n\n## Planner guidance for previous escalation\n"
                        + answer
                        + "\n\nContinue the task from this guidance. Do not repeat the escalation unless a new blocker appears.\n"
                    ),
                }

                # Resume — continue the loop
                continue

            for target_node_id, question in self._extract_worker_questions(raw_output):
                await self._handle_worker_question(
                    task=task,
                    source_node_id=worker_node.get("id", ""),
                    target_node_id=target_node_id,
                    question=question,
                    global_config=global_config,
                    db_session=db_session,
                )

            broadcast = self._extract_broadcast(raw_output)
            if broadcast:
                for target_node_id in self._broadcast_targets(
                    worker_node.get("id", ""), global_config,
                ):
                    await self._handle_worker_question(
                        task=task,
                        source_node_id=worker_node.get("id", ""),
                        target_node_id=target_node_id,
                        question=broadcast,
                        global_config=global_config,
                        db_session=db_session,
                    )

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

    async def get_task_dashboard(self, run_id: str) -> dict:
        """Aggregate all task statuses for a run into a dashboard summary.

        Returns: {"total": N, "completed": N, "failed": N, "running": N,
                  "pending": N, "blocked": N, "tasks": [{id, title, status, ...}]}
        """
        try:
            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel
            from sqlalchemy import select
            import uuid as _uuid
            async with async_session_factory() as session:
                result = await session.execute(
                    select(TaskModel).where(TaskModel.run_id == _uuid.UUID(run_id))
                )
                rows = result.scalars().all()
                counts = {"total": len(rows), "completed": 0, "failed": 0, "running": 0, "pending": 0, "blocked": 0, "assigned": 0}
                task_summaries = []
                for row in rows:
                    s = row.status or "pending"
                    counts[s] = counts.get(s, 0) + 1
                    task_summaries.append({
                        "id": str(row.id),
                        "title": row.title,
                        "status": s,
                        "progress": row.progress,
                        "assigned_node_id": row.assigned_node_id,
                    })
                return {**counts, "tasks": task_summaries}
        except Exception:
            logger.warning("Failed to get task dashboard for run %s", run_id, exc_info=True)
            return {"total": 0, "completed": 0, "failed": 0, "running": 0, "pending": 0, "blocked": 0, "assigned": 0, "tasks": []}

    async def _planner_answer(
        self,
        question: str,
        task: ManagedTask,
        planner_node: dict,
        run_id: str,
        global_config: dict,
        cancel_event: asyncio.Event,
        workspace_directory: str | None = None,
    ) -> str:
        """Ask the planner a question and return its text response.

        Creates a temporary worker-style execution with a focused prompt
        so the planner can answer without re-doing its full plan.
        """
        escalation_prompt = (
            f"A worker has escalated a question about task: {task.title}\n\n"
            f"Worker question: {question}\n\n"
            f"Task context: {task.description}\n\n"
        )

        # Inject task dashboard for global awareness
        dashboard = await self.get_task_dashboard(run_id)
        if dashboard["total"] > 0:
            escalation_prompt += (
                f"## 全局任务状态\n"
                f"总计: {dashboard['total']} | 完成: {dashboard['completed']} | "
                f"失败: {dashboard['failed']} | 运行中: {dashboard['running']} | "
                f"等待: {dashboard['pending']}\n\n"
            )

        escalation_prompt += "Please provide a concise answer to help the worker continue."

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
            workspace_directory=workspace_directory,
        )

        # Extract text from the answer
        raw = result.get("raw_output", "")
        return self._extract_answer_text(raw) or result.get("error", "No response from planner")

    # ------------------------------------------------------------------
    # Worker collaboration and prompt context
    # ------------------------------------------------------------------

    async def _build_runtime_context(self, run_id: str, node_id: str) -> str:
        """Build inbox + artifact context injected before a worker run."""
        if not node_id:
            return ""

        sections: list[str] = []
        try:
            import uuid as _uuid
            from app.core.database import async_session_factory
            from app.models.task import Artifact as ArtifactModel
            from app.models.task import Task as TaskModel
            from app.models.task import TaskMessage as TaskMessageModel
            from sqlalchemy import select

            async with async_session_factory() as session:
                task_result = await session.execute(
                    select(TaskModel).where(
                        TaskModel.run_id == _uuid.UUID(run_id),
                        TaskModel.assigned_node_id == node_id,
                    )
                )
                task_row = task_result.scalar_one_or_none()
                if task_row is not None:
                    msg_result = await session.execute(
                        select(TaskMessageModel)
                        .where(
                            TaskMessageModel.task_id == task_row.id,
                            TaskMessageModel.message_type.in_([
                                "worker_question", "worker_answer",
                                "planner_answer", "user_edit",
                            ]),
                        )
                        .order_by(TaskMessageModel.created_at.desc())
                        .limit(8)
                    )
                    messages = list(reversed(msg_result.scalars().all()))
                    if messages:
                        lines = [
                            f"- {msg.sender_type}:{msg.sender_id}"
                            + (f" -> {msg.target_node_id}" if msg.target_node_id else "")
                            + f": {msg.content[:500]}"
                            for msg in messages
                        ]
                        sections.append("## Worker inbox / guidance\n" + "\n".join(lines))

                artifact_result = await session.execute(
                    select(ArtifactModel)
                    .where(ArtifactModel.run_id == _uuid.UUID(run_id))
                    .order_by(ArtifactModel.created_at.desc())
                    .limit(12)
                )
                artifacts = list(reversed(artifact_result.scalars().all()))
                if artifacts:
                    lines = [
                        f"- [{a.type}] {a.node_id or 'run'}: {a.title}\n  {a.content[:600]}"
                        for a in artifacts
                    ]
                    sections.append("## Available artifact summaries\n" + "\n".join(lines))
        except Exception:
            logger.warning(
                "Failed to build runtime context for run=%s node=%s",
                run_id, node_id, exc_info=True,
            )

        if not sections:
            return ""
        return "\n\n---\n" + "\n\n".join(sections) + "\n"

    def _allowed_worker_targets(self, source_node_id: str, global_config: dict) -> set[str]:
        edges: list[dict] = global_config.get("_edges", [])
        upstream = {
            str(edge.get("source"))
            for edge in edges
            if edge.get("target") == source_node_id and edge.get("source")
        }
        downstream = {
            str(edge.get("target"))
            for edge in edges
            if edge.get("source") == source_node_id and edge.get("target")
        }
        same_layer: set[str] = set()
        for layer in global_config.get("_dag_layers", []):
            layer_ids = {str(nid) for nid in layer}
            if source_node_id in layer_ids:
                same_layer.update(layer_ids)
                break
        same_layer.discard(source_node_id)
        return upstream | downstream | same_layer

    def _broadcast_targets(self, source_node_id: str, global_config: dict) -> list[str]:
        return sorted(self._allowed_worker_targets(source_node_id, global_config))

    async def _task_for_node(self, run_id: str, node_id: str):
        try:
            import uuid as _uuid
            from app.core.database import async_session_factory
            from app.models.task import Task as TaskModel
            from sqlalchemy import select

            async with async_session_factory() as session:
                result = await session.execute(
                    select(TaskModel).where(
                        TaskModel.run_id == _uuid.UUID(run_id),
                        TaskModel.assigned_node_id == node_id,
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                return {
                    "id": str(row.id),
                    "title": row.title,
                    "status": row.status,
                    "result_summary": row.result_summary,
                }
        except Exception:
            logger.warning("Failed to find task for node %s", node_id, exc_info=True)
            return None

    async def _completed_worker_answer(self, run_id: str, target_node_id: str, target_task: dict) -> str:
        parts: list[str] = []
        if target_task.get("result_summary"):
            parts.append(f"Result summary: {target_task['result_summary']}")
        try:
            import uuid as _uuid
            from app.core.database import async_session_factory
            from app.models.task import Artifact as ArtifactModel
            from sqlalchemy import select

            async with async_session_factory() as session:
                result = await session.execute(
                    select(ArtifactModel)
                    .where(
                        ArtifactModel.run_id == _uuid.UUID(run_id),
                        ArtifactModel.node_id == target_node_id,
                    )
                    .order_by(ArtifactModel.created_at.desc())
                    .limit(3)
                )
                for artifact in result.scalars().all():
                    parts.append(
                        f"Artifact [{artifact.type}] {artifact.title}: {artifact.content[:700]}"
                    )
        except Exception:
            logger.warning("Failed to build completed worker answer", exc_info=True)

        if not parts:
            return "The target worker completed, but no summary or artifact is available."
        return "\n\n".join(parts)

    async def _record_message_by_task_id(
        self,
        run_id: str,
        task_id: str,
        sender_type: str,
        sender_id: str,
        message_type: str,
        content: str,
        target_node_id: str | None = None,
    ) -> None:
        dummy = ManagedTask(
            db_id=task_id,
            run_id=run_id,
            title="",
            description="",
        )
        await self._record_message(
            dummy,
            sender_type,
            sender_id,
            message_type,
            content,
            db_session=True,
            target_node_id=target_node_id,
        )

    async def _handle_worker_question(
        self,
        task: ManagedTask,
        source_node_id: str,
        target_node_id: str,
        question: str,
        global_config: dict,
        db_session=None,
    ) -> None:
        """Route a Worker->Worker question according to DAG permissions."""
        allowed = self._allowed_worker_targets(source_node_id, global_config)
        await self._record_message(
            task, "worker", source_node_id,
            "worker_question", question, db_session,
            target_node_id=target_node_id,
        )

        if target_node_id not in allowed:
            answer = (
                f"Worker communication to '{target_node_id}' was blocked because "
                "it is outside the allowed DAG scope. Escalate to the Planner for cross-scope coordination."
            )
            await self._record_message(
                task, "planner", "planner",
                "planner_answer", answer, db_session,
                target_node_id=source_node_id,
            )
            await self._emit(
                "worker_message", task.run_id, source_node_id,
                task_id=task.db_id,
                target_node_id=target_node_id,
                message_type="worker_question_rejected",
                content=answer,
            )
            return

        target_task = await self._task_for_node(task.run_id, target_node_id)
        if target_task is None:
            await self._emit(
                "worker_message", task.run_id, source_node_id,
                task_id=task.db_id,
                target_node_id=target_node_id,
                message_type="worker_question",
                content=question,
            )
            return

        await self._record_message_by_task_id(
            task.run_id,
            target_task["id"],
            "worker",
            source_node_id,
            "worker_question",
            question,
            target_node_id=target_node_id,
        )

        if target_task.get("status") == "completed":
            answer = await self._completed_worker_answer(
                task.run_id, target_node_id, target_task,
            )
            await self._record_message(
                task, "worker", target_node_id,
                "worker_answer", answer, db_session,
                target_node_id=source_node_id,
            )
            await self._emit(
                "worker_message", task.run_id, target_node_id,
                task_id=task.db_id,
                target_node_id=source_node_id,
                message_type="worker_answer",
                content=answer,
            )
        else:
            await self._emit(
                "worker_message", task.run_id, source_node_id,
                task_id=task.db_id,
                target_node_id=target_node_id,
                message_type="worker_question",
                content=question,
            )

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
                placeholder_questions = {
                    "<your question>",
                    "<your question or request>",
                    "<question>",
                    "<你的问题>",
                    "<你的问题或请求>",
                }
                if question.lower() in placeholder_questions:
                    continue
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
    def _extract_worker_questions(raw_output: str) -> list[tuple[str, str]]:
        questions: list[tuple[str, str]] = []
        if not raw_output:
            return questions
        for line in raw_output.split("\n"):
            line = line.strip()
            if ASK_WORKER_MARKER not in line:
                continue
            _, rest = line.split(ASK_WORKER_MARKER, 1)
            target, sep, question = rest.strip().partition(":")
            if sep and target.strip() and question.strip():
                questions.append((target.strip(), question.strip()))
        return questions

    @staticmethod
    def _extract_broadcast(raw_output: str) -> str | None:
        if not raw_output:
            return None
        for line in raw_output.split("\n"):
            line = line.strip()
            if BROADCAST_MARKER in line:
                message = line.split(BROADCAST_MARKER, 1)[1].strip()
                if message:
                    return message
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
        target_node_id: str | None = None,
        artifact_id: str | None = None,
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
                        target_node_id=target_node_id,
                        artifact_id=_uuid.UUID(artifact_id) if artifact_id else None,
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
            target_node_id=target_node_id or "",
            artifact_id=artifact_id or "",
        )
