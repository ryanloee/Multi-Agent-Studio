"""SQLAlchemy ORM models for the orchestrator database.

Uses SQLAlchemy 2.0 style with Mapped / mapped_column.
All models use UUID primary keys and async-compatible column types.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, ForeignKey, Integer, Uuid, JSON, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dag_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    workspace_directory: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto", server_default="auto")
    goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lifecycle_phase: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", server_default="draft",
    )
    blockers_json: Mapped[Optional[list[dict]]] = mapped_column(JSON, nullable=True)
    project_summary_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    project_summary_artifact_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=datetime.utcnow,
    )

    # Relationships
    runs: Mapped[list["Run"]] = relationship(
        "Run", back_populates="workflow", cascade="all, delete-orphan",
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="workflow", cascade="all, delete-orphan",
    )
    shared_document: Mapped[Optional["SharedDocument"]] = relationship(
        "SharedDocument", back_populates="workflow", uselist=False, cascade="all, delete-orphan",
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        server_default="pending",
    )
    engine_workflow_id: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Relationships
    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="runs")
    node_executions: Mapped[list["NodeExecution"]] = relationship(
        "NodeExecution", back_populates="run", cascade="all, delete-orphan",
    )
    tasks: Mapped[list["Task"]] = relationship(
        "Task", back_populates="run", cascade="all, delete-orphan",
    )
    events: Mapped[list["RunEvent"]] = relationship(
        "RunEvent", back_populates="run", cascade="all, delete-orphan",
    )


class RunEvent(Base):
    """Persisted white-box stream event for a workflow run.

    WebSocket delivery is best-effort and page-local. This table is the durable
    history used to restore LLM, shell, tool, communication, and timeline panels
    after refresh or after the user closes the browser.
    """

    __tablename__ = "run_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    node_id: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )

    run: Mapped["Run"] = relationship("Run", back_populates="events")


class NodeExecution(Base):
    __tablename__ = "node_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        server_default="pending",
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    run: Mapped["Run"] = relationship("Run", back_populates="node_executions")


class ChatMessage(Base):
    """Persisted chat messages for planner/node conversations in a workflow.

    Supports resuming conversations after page reload or app restart.
    Each message is associated with a workflow and optionally a specific node.
    """

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="planner",
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )

    # Relationships
    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="chat_messages")


class SharedDocument(Base):
    """Project-level shared document accessible to planner, workers, and user."""

    __tablename__ = "shared_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4,
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(
        String(50), nullable=False, default="user",
        server_default="user",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=datetime.utcnow,
    )

    # Relationships
    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="shared_document")
