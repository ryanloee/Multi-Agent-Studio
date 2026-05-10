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


class UpdateWorkflowRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    dag_json: Optional[dict[str, Any]] = None
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
        return self


class WorkflowResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    dag_json: Optional[dict[str, Any]] = None
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
