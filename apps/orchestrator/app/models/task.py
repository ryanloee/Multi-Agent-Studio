"""Task and TaskMessage ORM models for the dynamic task board.

Tasks represent sub-work units created by the Planner and executed by Workers.
TaskMessages record the communication between Planner, Workers, and Users.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, String, Text, ForeignKey, Integer, Uuid, DateTime, func
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
    dependencies: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    artifacts: Mapped[list["Artifact"]] = relationship(
        "Artifact", back_populates="task", cascade="all, delete-orphan",
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
    )  # assignment | worker_question | worker_answer | planner_question | planner_answer | artifact_created | update | user_edit
    content: Mapped[str] = mapped_column(Text, nullable=False)
    target_node_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    artifact_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    # Relationships
    task: Mapped["Task"] = relationship("Task", back_populates="messages")


class Artifact(Base):
    """Structured artifact produced by a planner or worker task."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    node_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    task: Mapped[Optional["Task"]] = relationship("Task", back_populates="artifacts")
