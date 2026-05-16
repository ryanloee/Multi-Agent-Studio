"""Task board endpoints.

GET    /{run_id}/tasks              - List tasks for a run
GET    /{run_id}/tasks/{task_id}    - Get task details
POST   /{run_id}/tasks              - Create a new task (manual user creation)
PATCH  /{run_id}/tasks/{task_id}    - Update task (user edits)
POST   /{run_id}/tasks/{task_id}/restart  - Restart a failed task
POST   /{run_id}/tasks/{task_id}/assign    - Assign a task to a specific node and execute
POST   /{run_id}/tasks/{task_id}/messages  - Send a message to a task
GET    /{run_id}/tasks/{task_id}/messages  - Get task messages
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.task import Task, TaskMessage
from app.models.schemas import (
    TaskCreate,
    TaskResponse,
    TaskUpdate,
    TaskAssignRequest,
    TaskMessageCreate,
    TaskMessageResponse,
)

router = APIRouter()

# Module-level references set by main.py lifespan
_engine = None
_event_bus = None


def init_task_deps(engine, event_bus) -> None:
    """Set module-level references. Called once during app startup."""
    global _engine, _event_bus
    _engine = engine
    _event_bus = event_bus


@router.get("/{run_id}/tasks", response_model=list[TaskResponse])
async def list_tasks(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """List all tasks for a run, ordered by creation time."""
    result = await db.execute(
        select(Task)
        .where(Task.run_id == run_id)
        .order_by(Task.created_at)
    )
    return result.scalars().all()


@router.get("/{run_id}/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    run_id: UUID,
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a single task by ID."""
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/{run_id}/tasks", response_model=TaskResponse, status_code=201)
async def create_task(
    run_id: UUID,
    body: TaskCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new task manually (by the user).

    This allows the user to add tasks that the planner didn't generate,
    and assign them to specific workflow nodes.
    """
    from app.models.db import Run

    # Verify run exists
    run_result = await db.execute(select(Run).where(Run.id == run_id))
    run = run_result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    task = Task(
        id=uuid4(),
        run_id=run_id,
        parent_task_id=body.parent_task_id,
        title=body.title,
        description=body.description,
        status="pending",
        assigned_node_id=body.assigned_node_id,
        assigned_worker_label=body.assigned_worker_label,
    )
    db.add(task)

    # Record creation message
    msg = TaskMessage(
        id=uuid4(),
        task_id=task.id,
        sender_type="user",
        sender_id="user",
        message_type="assignment",
        content=f"User created task: {body.title}",
    )
    db.add(msg)

    await db.flush()
    await db.refresh(task)
    return task


@router.patch("/{run_id}/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    run_id: UUID,
    task_id: UUID,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    """User edits a task (title, description, status, assigned_node_id, etc.)."""
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)

    # Record the user edit as a task message
    if update_data:
        user_msg = TaskMessage(
            task_id=task_id,
            sender_type="user",
            sender_id="user",
            message_type="user_edit",
            content=f"User updated: {', '.join(update_data.keys())}",
        )
        db.add(user_msg)

    await db.flush()
    await db.refresh(task)
    return task


@router.post("/{run_id}/tasks/{task_id}/restart", response_model=TaskResponse)
async def restart_task(
    run_id: UUID,
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Restart a failed/completed task — resets status and re-executes on the same node."""
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status not in ("failed", "completed", "pending"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot restart task in status '{task.status}'. "
                   f"Task must be failed, completed, blocked, or pending.",
        )

    node_id = task.assigned_node_id
    task.status = "assigned" if node_id else "pending"
    task.progress = 0
    task.result_summary = ""

    # Record restart as a task message
    user_msg = TaskMessage(
        task_id=task_id,
        sender_type="user",
        sender_id="user",
        message_type="user_edit",
        content="Task restarted by user",
    )
    db.add(user_msg)

    await db.flush()
    await db.refresh(task)

    # If the task has an assigned node, re-execute it in the background
    if node_id and _engine and _event_bus:
        # Look up the node config from the workflow's dag_json
        from app.models.db import Run
        from sqlalchemy.orm import selectinload

        run_result = await db.execute(
            select(Run)
            .where(Run.id == run_id)
            .options(selectinload(Run.workflow))
        )
        run_obj = run_result.scalar_one_or_none()

        node_config = None
        if run_obj and run_obj.workflow and run_obj.workflow.dag_json:
            nodes_list = run_obj.workflow.dag_json.get("nodes", [])
            for n in nodes_list:
                if n.get("id") == node_id:
                    node_config = n
                    break

        if node_config:
            from app.core.task_scheduler import ManagedTask, PROGRESS_MARKER

            node_data = node_config.get("data", {}) or {}
            managed = ManagedTask(
                db_id=str(task.id),
                run_id=str(run_id),
                title=task.title,
                description=task.description,
                status="assigned",
                assigned_node_id=node_id,
                assigned_worker_label=task.assigned_worker_label or node_id,
            )

            # Build worker node from the stored node config
            worker_prompt = node_data.get("prompt", "") or task.description or task.title
            worker_prompt += (
                f"\n\n---\nTask ID: {node_id}\n"
                f"To report progress, output a line:\n"
                f"{PROGRESS_MARKER} <0-100>\n"
            )

            worker_node = {
                "id": node_id,
                "agent_type": node_data.get("agentType", "coder"),
                "model_provider": node_data.get("modelProvider", ""),
                "model_id": node_data.get("modelId", ""),
                "prompt": worker_prompt,
            }

            cancel_event = asyncio.Event()

            # Start execution in background
            async def _run_task():
                from app.core.task_scheduler import TaskScheduler
                scheduler = TaskScheduler(
                    sandbox=_engine._sandbox,
                    event_bus=_event_bus,
                    checkpoint=_engine._checkpoint,
                    provisioner=_engine._provisioner,
                    execute_node_fn=_engine._execute_node,
                    emit_fn=_engine._emit,
                )
                result = await scheduler.run_worker_task(
                    worker_node=worker_node,
                    task=managed,
                    global_config={"_workflow_id": str(run_obj.workflow_id) if run_obj else ""},
                    cancel_event=cancel_event,
                )
                if result.get("state") == "completed":
                    await _engine._create_artifact_for_task(
                        run_id=str(run_id),
                        workflow_id=await _engine._get_run_workflow_id(str(run_id)),
                        task_id=str(task.id),
                        node_id=node_id,
                        agent_type=worker_node.get("agent_type", "coder"),
                        title=task.title,
                        result=result,
                    )

            asyncio.create_task(_run_task(), name=f"restart-{task_id}")

    return task


@router.post("/{run_id}/tasks/{task_id}/assign", response_model=TaskResponse)
async def assign_task(
    run_id: UUID,
    task_id: UUID,
    body: TaskAssignRequest,
    db: AsyncSession = Depends(get_db),
):
    """Assign a pending/blocked/failed task to a specific workflow node and start execution.

    This allows:
    - Reassigning a task to a different agent node
    - Starting execution of a manually created task
    - Re-running a failed task on a different node
    """
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status not in ("pending", "failed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot assign task in status '{task.status}'. "
                   f"Task must be pending, blocked, failed, or completed.",
        )

    # Update task assignment
    task.assigned_node_id = body.node_id
    task.assigned_worker_label = body.node_label or body.node_id
    task.status = "assigned"
    task.progress = 0
    task.result_summary = ""

    # Record assignment message
    msg = TaskMessage(
        task_id=task_id,
        sender_type="user",
        sender_id="user",
        message_type="assignment",
        content=f"User assigned task to {body.node_label or body.node_id}",
    )
    db.add(msg)

    await db.flush()
    await db.refresh(task)

    # If the engine is available, start the task execution in the background
    if _engine and _event_bus and body.prompt:
        from app.core.task_scheduler import ManagedTask, PROGRESS_MARKER

        managed = ManagedTask(
            db_id=str(task.id),
            run_id=str(run_id),
            title=task.title,
            description=task.description,
            status="assigned",
            assigned_node_id=body.node_id,
            assigned_worker_label=body.node_label or body.node_id,
        )

        # Build worker node from the assignment request
        worker_prompt = body.prompt
        worker_prompt += (
            f"\n\n---\nTask ID: {body.node_id}\n"
            f"To report progress, output a line:\n"
            f"{PROGRESS_MARKER} <0-100>\n"
        )

        worker_node = {
            "id": body.node_id,
            "agent_type": body.agent_type or "coder",
            "model_provider": body.model_provider or "",
            "model_id": body.model_id or "",
            "prompt": worker_prompt,
        }

        cancel_event = asyncio.Event()

        # Start execution in background
        async def _run_task():
            from app.core.task_scheduler import TaskScheduler
            scheduler = TaskScheduler(
                sandbox=_engine._sandbox,
                event_bus=_event_bus,
                checkpoint=_engine._checkpoint,
                provisioner=_engine._provisioner,
                execute_node_fn=_engine._execute_node,
                emit_fn=_engine._emit,
            )
            workflow_id = await _engine._get_run_workflow_id(str(run_id))
            result = await scheduler.run_worker_task(
                worker_node=worker_node,
                task=managed,
                global_config={"_workflow_id": workflow_id or ""},
                cancel_event=cancel_event,
            )
            if result.get("state") == "completed":
                await _engine._create_artifact_for_task(
                    run_id=str(run_id),
                    workflow_id=workflow_id,
                    task_id=str(task.id),
                    node_id=body.node_id,
                    agent_type=worker_node.get("agent_type", "coder"),
                    title=task.title,
                    result=result,
                )

        asyncio.create_task(_run_task(), name=f"assign-{task_id}")

    return task


@router.post(
    "/{run_id}/tasks/{task_id}/messages",
    response_model=TaskMessageResponse,
    status_code=201,
)
async def send_task_message(
    run_id: UUID,
    task_id: UUID,
    body: TaskMessageCreate,
    db: AsyncSession = Depends(get_db),
):
    """Send a user message to a task (e.g., instructions, feedback)."""
    # Verify task exists
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    msg = TaskMessage(
        task_id=task_id,
        sender_type=body.sender_type,
        sender_id=body.sender_id,
        message_type=body.message_type,
        content=body.content,
        target_node_id=body.target_node_id,
        artifact_id=body.artifact_id,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)
    return msg


@router.get(
    "/{run_id}/tasks/{task_id}/messages",
    response_model=list[TaskMessageResponse],
)
async def list_task_messages(
    run_id: UUID,
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get all messages for a task, ordered by time."""
    # Verify task exists
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    msgs = await db.execute(
        select(TaskMessage)
        .where(TaskMessage.task_id == task_id)
        .order_by(TaskMessage.created_at)
    )
    return msgs.scalars().all()
