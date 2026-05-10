"""Workflow CRUD endpoints.

POST   /           - Create workflow
GET    /           - List workflows
GET    /{id}       - Get workflow detail
PUT    /{id}       - Update workflow (save DAG)
DELETE /{id}       - Delete workflow
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Workflow
from app.models.schemas import (
    CreateWorkflowRequest,
    UpdateWorkflowRequest,
    WorkflowResponse,
)
from app.workflows.compiler import compile_dag

router = APIRouter()


@router.post("", response_model=WorkflowResponse, status_code=201)
async def create_workflow(
    body: CreateWorkflowRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create workflow from React Flow JSON."""
    # Validate DAG if provided
    if body.dag_json is not None:
        try:
            compile_dag(body.dag_json)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    workflow = Workflow(
        name=body.name,
        description=body.description,
        dag_json=body.dag_json,
        workspace_directory=body.workspace_directory,
        mode=body.mode or "manual",
        goal=body.goal,
    )

    # Auto mode: initialize with a default Planner node
    if (body.mode or "manual") == "auto" and body.dag_json is None:
        workflow.dag_json = {
            "nodes": [
                {
                    "id": "planner",
                    "type": "plan",
                    "position": {"x": 300, "y": 200},
                    "data": {
                        "label": "Planner",
                        "agentType": "plan",
                        "modelProvider": "",
                        "modelId": "",
                        "prompt": body.goal or "",
                        "permissions": {},
                        "command": "",
                        "description": "",
                    },
                }
            ],
            "edges": [],
        }

    db.add(workflow)
    await db.flush()
    await db.refresh(workflow)
    return workflow


@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    db: AsyncSession = Depends(get_db),
):
    """List all workflows."""
    result = await db.execute(
        select(Workflow).order_by(Workflow.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get workflow detail."""
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.put("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: UUID,
    body: UpdateWorkflowRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update workflow definition (save DAG)."""
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Validate DAG if being updated
    new_dag = body.dag_json if body.dag_json is not None else workflow.dag_json
    if new_dag is not None:
        try:
            compile_dag(new_dag)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.name is not None:
        workflow.name = body.name
    if body.description is not None:
        workflow.description = body.description
    if body.dag_json is not None:
        workflow.dag_json = body.dag_json
    if body.workspace_directory is not None:
        workflow.workspace_directory = body.workspace_directory
    if body.mode is not None:
        workflow.mode = body.mode
    if body.goal is not None:
        workflow.goal = body.goal

    await db.flush()
    await db.refresh(workflow)
    return workflow


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete workflow and all associated runs."""
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    await db.delete(workflow)
    await db.flush()
    return None
