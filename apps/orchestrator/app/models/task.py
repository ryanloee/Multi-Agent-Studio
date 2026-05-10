"""Task and TaskMessage ORM models for the dynamic task board.

Tasks represent sub-work units created by the Planner and executed by Workers.
TaskMessages record the communication between Planner, Workers, and Users.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, ForeignKey, Integer, Uuid, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.db import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        server_default="pending",
    )
    assigned_node_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    assigned_worker_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=datetime.utcnow,
    )

    # Relationships
    run: Mapped["Run"] = relationship("Run", back_populates="tasks")
    messages: Mapped[list["TaskMessage"]] = relationship(
        "TaskMessage", back_populates="task", cascade="all, delete-orphan",
    )
    sub_tasks: Mapped[list["Task"]] = relationship(
        "Task", back_populates="parent_task",
        foreign_keys="[Task.parent_task_id]",
    )
    parent_task: Mapped[Optional["Task"]] = relationship(
        "Task", back_populates="sub_tasks",
        remote_side="[Task.id]",
        foreign_keys="[Task.parent_task_id]",
    )


class TaskMessage(Base):
    __tablename__ = "task_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
    )  # "planner" | "worker" | "user"
    sender_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
    )  # node_id or "planner"
    message_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
    )  # "assignment" | "question" | "answer" | "escalation" | "update" | "user_edit"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    # Relationships
    task: Mapped["Task"] = relationship("Task", back_populates="messages")
