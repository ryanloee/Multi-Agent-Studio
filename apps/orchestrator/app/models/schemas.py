"""Pydantic request / response schemas for the REST API."""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has UTC timezone info for correct JSON serialization.

    SQLite stores datetimes as naive (no tz), but they are actually UTC.
    This prevents the 8-hour offset bug when the frontend interprets them.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Workflow schemas
# ---------------------------------------------------------------------------

class CreateWorkflowRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    dag_json: Optional[dict[str, Any]] = None
    workspace_directory: Optional[str] = None
    mode: Optional[str] = Field("auto", pattern="^(auto|manual)$")
    goal: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class UpdateWorkflowRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    dag_json: Optional[dict[str, Any]] = None
    workspace_directory: Optional[str] = None
    mode: Optional[str] = Field(None, pattern="^(auto|manual)$")
    goal: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    lifecycle_phase: Optional[str] = Field(
        None,
        pattern="^(draft|assessing|planning|ready|running|blocked|review)$",
    )
    blockers: Optional[list[dict[str, Any]]] = None
    project_summary: Optional[dict[str, Any]] = None
    nodes: Optional[list[dict[str, Any]]] = None
    edges: Optional[list[dict[str, Any]]] = None

    @model_validator(mode="after")
    def build_dag_json(self):
        """If nodes/edges are provided, pack them into dag_json for storage."""
        if self.nodes is not None or self.edges is not None:
            self.dag_json = {
                "nodes": self.nodes or [],
                "edges": self.edges or [],
            }
            if self.metadata is not None:
                self.dag_json["metadata"] = self.metadata
        return self


class WorkflowResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    dag_json: Optional[dict[str, Any]] = None
    workspace_directory: Optional[str] = None
    mode: str = "auto"
    goal: Optional[str] = None
    lifecycle_phase: str = "draft"
    blockers_json: Optional[list[dict[str, Any]]] = None
    project_summary_json: Optional[dict[str, Any]] = None
    project_summary_artifact_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    @computed_field
    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self.dag_json.get("nodes", []) if self.dag_json else []

    @computed_field
    @property
    def edges(self) -> list[dict[str, Any]]:
        return self.dag_json.get("edges", []) if self.dag_json else []

    @computed_field
    @property
    def metadata(self) -> dict[str, Any]:
        return self.dag_json.get("metadata", {}) if self.dag_json else {}

    @computed_field
    @property
    def blockers(self) -> list[dict[str, Any]]:
        return self.blockers_json or []

    @computed_field
    @property
    def project_summary(self) -> dict[str, Any]:
        return self.project_summary_json or {}

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Run schemas
# ---------------------------------------------------------------------------

class TriggerRunRequest(BaseModel):
    dag: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None


class RunResponse(BaseModel):
    id: UUID
    workflow_id: UUID
    status: str
    engine_workflow_id: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    @field_validator("created_at", "completed_at", mode="before")
    @classmethod
    def _utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _ensure_utc(v) if v is not None else None

    model_config = {"from_attributes": True}


class NodeExecutionResponse(BaseModel):
    id: UUID
    run_id: UUID
    node_id: str
    agent_type: str
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    error_message: Optional[str] = None

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _ensure_utc(v) if v is not None else None

    model_config = {"from_attributes": True}


class RunEventResponse(BaseModel):
    id: UUID
    run_id: UUID
    event_type: str
    node_id: str
    payload: dict[str, Any]
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Task schemas
# ---------------------------------------------------------------------------

class TaskCreate(BaseModel):
    run_id: UUID
    parent_task_id: Optional[UUID] = None
    title: str = Field(..., min_length=1, max_length=512)
    description: str = ""
    assigned_node_id: Optional[str] = None
    assigned_worker_label: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = None
    status: Optional[str] = None
    assigned_node_id: Optional[str] = None
    assigned_worker_label: Optional[str] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    result_summary: Optional[str] = None
    dependencies: Optional[str] = None


class TaskAssignRequest(BaseModel):
    """Request body for assigning a task to a specific workflow node."""
    node_id: str = Field(..., min_length=1)
    node_label: Optional[str] = None
    agent_type: Optional[str] = "coder"
    model_provider: Optional[str] = ""
    model_id: Optional[str] = ""
    prompt: Optional[str] = ""


class TaskResponse(BaseModel):
    id: UUID
    run_id: UUID
    parent_task_id: Optional[UUID] = None
    title: str
    description: str
    status: str
    assigned_node_id: Optional[str] = None
    assigned_worker_label: Optional[str] = None
    progress: int
    result_summary: str
    dependencies: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# TaskMessage schemas
# ---------------------------------------------------------------------------

class TaskMessageCreate(BaseModel):
    sender_type: str = Field(..., pattern="^(planner|worker|user)$")
    sender_id: str
    message_type: str = Field(
        ...,
        pattern="^(assignment|question|answer|escalation|update|user_edit|worker_question|worker_answer|planner_question|planner_answer|artifact_created)$",
    )
    content: str
    target_node_id: Optional[str] = None
    artifact_id: Optional[UUID] = None


class TaskMessageResponse(BaseModel):
    id: UUID
    task_id: UUID
    sender_type: str
    sender_id: str
    message_type: str
    content: str
    target_node_id: Optional[str] = None
    artifact_id: Optional[UUID] = None
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Artifact schemas
# ---------------------------------------------------------------------------

class ArtifactCreate(BaseModel):
    workflow_id: UUID
    task_id: Optional[UUID] = None
    node_id: Optional[str] = None
    type: str = Field(
        ...,
        pattern="^(file_change|research_note|test_result|review_report|merge_report|decision|final_output|project_summary)$",
    )
    title: str = Field(..., min_length=1, max_length=512)
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"


class ArtifactUpdate(BaseModel):
    type: Optional[str] = Field(
        None,
        pattern="^(file_change|research_note|test_result|review_report|merge_report|decision|final_output|project_summary)$",
    )
    title: Optional[str] = Field(None, min_length=1, max_length=512)
    content: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ArtifactResponse(BaseModel):
    id: UUID
    run_id: UUID
    workflow_id: UUID
    task_id: Optional[UUID] = None
    node_id: Optional[str] = None
    type: str
    title: str
    content: str
    metadata_json: Optional[dict[str, Any]] = None
    created_by: str
    created_at: datetime

    @computed_field
    @property
    def metadata(self) -> dict[str, Any]:
        return self.metadata_json or {}

    @field_validator("created_at", mode="before")
    @classmethod
    def _artifact_utc_aware(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    model_config = {"from_attributes": True}
