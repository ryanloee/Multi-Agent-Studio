"""Shared project document API — one document per workflow.

The shared document is a collaborative markdown scratchpad that both the
user and the AI agents (planner / workers) can read and write.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import SharedDocument, Workflow

logger = logging.getLogger(__name__)

router = APIRouter()


class SharedDocResponse(BaseModel):
    id: str
    workflow_id: str
    content: str
    updated_by: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class UpdateSharedDocRequest(BaseModel):
    content: str = Field(..., max_length=100_000)
    updated_by: str = Field("user", pattern=r"^(user|planner|worker)$")


@router.get("/{workflow_id}/shared-doc", response_model=SharedDocResponse)
async def get_shared_doc(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get or create the shared document for a workflow."""
    wf_id = uuid.UUID(workflow_id)

    result = await db.execute(
        select(SharedDocument).where(SharedDocument.workflow_id == wf_id)
    )
    doc = result.scalar_one_or_none()

    if doc is None:
        # Auto-create
        doc = SharedDocument(workflow_id=wf_id, content="", updated_by="user")
        db.add(doc)
        await db.commit()
        await db.refresh(doc)

    return SharedDocResponse(
        id=str(doc.id),
        workflow_id=str(doc.workflow_id),
        content=doc.content,
        updated_by=doc.updated_by,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


@router.put("/{workflow_id}/shared-doc", response_model=SharedDocResponse)
async def update_shared_doc(
    workflow_id: str,
    body: UpdateSharedDocRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update the shared document content."""
    wf_id = uuid.UUID(workflow_id)

    result = await db.execute(
        select(SharedDocument).where(SharedDocument.workflow_id == wf_id)
    )
    doc = result.scalar_one_or_none()

    if doc is None:
        doc = SharedDocument(workflow_id=wf_id, content=body.content, updated_by=body.updated_by)
        db.add(doc)
    else:
        doc.content = body.content
        doc.updated_by = body.updated_by

    await db.commit()
    await db.refresh(doc)

    return SharedDocResponse(
        id=str(doc.id),
        workflow_id=str(doc.workflow_id),
        content=doc.content,
        updated_by=doc.updated_by,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )
