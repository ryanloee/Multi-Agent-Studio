"""Task board endpoints.

GET    /{run_id}/tasks              - List tasks for a run
GET    /{run_id}/tasks/{task_id}    - Get task details
PATCH  /{run_id}/tasks/{task_id}    - Update task (user edits)
POST   /{run_id}/tasks/{task_id}/restart  - Restart a failed task
POST   /{run_id}/tasks/{task_id}/messages  - Send a message to a task
GET    /{run_id}/tasks/{task_id}/messages  - Get task messages
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.task import Task, TaskMessage
from app.models.schemas import (
    TaskResponse,
    TaskUpdate,
    TaskMessageCreate,
    TaskMessageResponse,
)

router = APIRouter()


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


@router.patch("/{run_id}/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    run_id: UUID,
    task_id: UUID,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    """User edits a task (title, description, status, etc.)."""
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
    """Restart a failed/completed task — resets status to pending."""
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.run_id == run_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = "pending"
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
