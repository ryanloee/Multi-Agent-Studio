"""Artifact endpoints for run-scoped structured outputs."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Run
from app.models.schemas import ArtifactCreate, ArtifactResponse, ArtifactUpdate
from app.models.task import Artifact, TaskMessage

router = APIRouter()


@router.get("/{run_id}/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Artifact)
        .where(Artifact.run_id == run_id)
        .order_by(Artifact.created_at)
    )
    return result.scalars().all()


@router.get("/{run_id}/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(
    run_id: UUID,
    artifact_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.run_id == run_id)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.post("/{run_id}/artifacts", response_model=ArtifactResponse, status_code=201)
async def create_artifact(
    run_id: UUID,
    body: ArtifactCreate,
    db: AsyncSession = Depends(get_db),
):
    run_result = await db.execute(select(Run).where(Run.id == run_id))
    run = run_result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.workflow_id != body.workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id does not match run")

    artifact = Artifact(
        id=uuid4(),
        run_id=run_id,
        workflow_id=body.workflow_id,
        task_id=body.task_id,
        node_id=body.node_id,
        type=body.type,
        title=body.title,
        content=body.content,
        metadata_json=body.metadata,
        created_by=body.created_by,
    )
    db.add(artifact)

    if body.task_id:
        db.add(
            TaskMessage(
                task_id=body.task_id,
                sender_type="worker" if body.created_by != "planner" else "planner",
                sender_id=body.node_id or body.created_by,
                message_type="artifact_created",
                content=f"Artifact created: {body.title}",
                artifact_id=artifact.id,
            )
        )

    await db.flush()
    await db.refresh(artifact)
    return artifact


@router.patch("/{run_id}/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def update_artifact(
    run_id: UUID,
    artifact_id: UUID,
    body: ArtifactUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.run_id == run_id)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    data = body.model_dump(exclude_unset=True)
    if "metadata" in data:
        artifact.metadata_json = data.pop("metadata")
    for key, value in data.items():
        setattr(artifact, key, value)

    await db.flush()
    await db.refresh(artifact)
    return artifact
