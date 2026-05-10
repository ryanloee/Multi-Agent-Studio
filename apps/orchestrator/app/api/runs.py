"""Run management endpoints.

POST   /{workflow_id}/run    - Trigger workflow execution
GET    /                     - List run records
GET    /{run_id}             - Get run status
POST   /{run_id}/cancel      - Cancel a running workflow
GET    /{run_id}/diff         - Get Git diff (Human-in-the-Loop)
GET    /{run_id}/nodes        - Get node execution details
POST   /{run_id}/approve     - Approve a paused run (Human-in-the-Loop)
POST   /{run_id}/reject      - Reject a paused run (Human-in-the-Loop)
POST   /{run_id}/rollback    - Rollback to a previous checkpoint
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Run, NodeExecution
from app.models.schemas import TriggerRunRequest, RunResponse, NodeExecutionResponse

if TYPE_CHECKING:
    from app.core.local_engine import LocalDAGExecutor

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level engine singleton — initialised by main.py lifespan
_engine: LocalDAGExecutor | None = None


def init_engine(engine: LocalDAGExecutor) -> None:
    """Set the module-level DAG executor.  Called once during app startup."""
    global _engine
    _engine = engine


def _require_engine() -> LocalDAGExecutor:
    if _engine is None:
        raise HTTPException(status_code=503, detail="Workflow engine not initialised")
    return _engine


# ---------------------------------------------------------------------------
# HITL approval state — in-memory store for pending approvals
# ---------------------------------------------------------------------------

# Maps run_id -> asyncio.Event that the engine waits on for approval
_approval_events: dict[str, asyncio.Event] = {}

# Maps run_id -> {"approved": bool, "reason": str}
_approval_results: dict[str, dict] = {}


def set_approval_event(run_id: str, event: asyncio.Event) -> None:
    """Register an approval wait event for a run (called by engine)."""
    _approval_events[run_id] = event


def clear_approval(run_id: str) -> None:
    """Clean up approval state after resolution."""
    _approval_events.pop(run_id, None)
    _approval_results.pop(run_id, None)


@router.post("/{workflow_id}/run", response_model=RunResponse, status_code=201)
async def trigger_run(
    workflow_id: UUID,
    body: TriggerRunRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Trigger workflow execution via the local DAG engine.

    Supports two modes:
    - "manual" (default): Compile the user-drawn DAG from dag_json and execute.
    - "auto": Build a synthetic Planner node from workflow.goal, run it first,
      then let the engine parse its output into a DAG and execute dynamically.

    Steps:
    1. Look up workflow from DB to get DAG JSON, mode, and goal.
    2. Route based on mode (auto vs manual).
    3. Compile DAG into execution layers via compiler.compile_dag().
    4. Convert layers into dicts for the LocalDAGExecutor.
    5. Start DAG execution in the background.
    6. Persist run record to DB and return it.
    """
    from app.workflows.compiler import compile_dag

    engine = _require_engine()

    # 1. Fetch workflow
    from app.models.db import Workflow
    wf_result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = wf_result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    payload = body or TriggerRunRequest()
    global_config = payload.config or {}
    workflow_mode = getattr(workflow, "mode", "manual") or "manual"

    # 2. Route based on mode
    if workflow_mode == "auto":
        # Auto mode: goal is required; build a synthetic planner node
        goal = getattr(workflow, "goal", "") or ""
        if not goal:
            raise HTTPException(
                status_code=400,
                detail="Auto mode requires a non-empty goal on the workflow",
            )

        planner_node = {
            "id": "planner",
            "type": "plan",
            "agent_type": "plan",
            "data": {
                "label": "Planner",
                "agentType": "plan",
                "prompt": goal,
            },
            "model_provider": "",
            "model_id": "",
            "prompt": goal,
        }
        layers_data = [[planner_node]]
        global_config["_mode"] = "auto"
        global_config["_goal"] = goal
    else:
        # Manual mode (default): compile the user-drawn DAG
        dag_json = payload.dag if payload.dag else (workflow.dag_json or {})
        if not dag_json:
            raise HTTPException(status_code=400, detail="No DAG definition found")

        # 3. Compile DAG into layers
        try:
            compiled_layers = compile_dag(dag_json)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 4. Serialise layers into dicts for the engine
        #    Frontend sends camelCase (agentType, modelProvider, modelId).
        #    Engine expects snake_case -- normalise here.
        layers_data: list[dict] = []
        for layer_nodes in compiled_layers:
            layer_list = []
            for node_def in layer_nodes:
                node_data = node_def.get("data", node_def)
                # Accept both camelCase (from frontend) and snake_case
                agent_type = (
                    node_data.get("agent_type")
                    or node_data.get("agentType")
                    or "coder"
                )
                model_provider = (
                    node_data.get("model_provider")
                    or node_data.get("modelProvider")
                    or ""
                )
                model_id = (
                    node_data.get("model_id")
                    or node_data.get("modelId")
                    or ""
                )
                prompt = node_data.get("prompt", "")
                layer_list.append({
                    "id": node_def.get("id", ""),
                    "agent_type": agent_type,
                    "model_provider": model_provider,
                    "model_id": model_id,
                    "prompt": prompt,
                })
            layers_data.append(layer_list)

    global_config["_edges"] = dag_json.get("edges", []) if dag_json else []

    # 5. Start DAG execution via LocalDAGExecutor
    run_id = uuid4()
    await engine.start_workflow(
        run_id=str(run_id),
        layers=layers_data,
        global_config=global_config,
        workspace_directory=workflow.workspace_directory,
    )

    # 6. Persist run record
    run = Run(
        id=run_id,
        workflow_id=workflow_id,
        status="running",
        engine_workflow_id=str(run_id),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)

    return run


@router.get("", response_model=list[RunResponse])
async def list_runs(
    workflow_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List run records, optionally filtered by workflow_id."""
    query = select(Run).order_by(Run.created_at.desc()).offset(offset).limit(limit)
    if workflow_id is not None:
        query = query.where(Run.workflow_id == workflow_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get run status and details."""
    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Enrich with engine status if the run is still running
    if run.status == "running" and _engine is not None:
        try:
            engine_status = await _engine.get_status(str(run.id))
            engine_state = engine_status.get("status", "")
            if engine_state in ("completed", "failed", "cancelled"):
                run.status = engine_state
        except Exception:
            pass  # Engine not available, return DB status

    return run


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Cancel a running workflow."""
    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel run in status '{run.status}'")

    engine = _require_engine()

    # Cancel via LocalDAGExecutor
    await engine.cancel(str(run.id))

    # Update DB
    run.status = "cancelling"
    await db.flush()

    return {"status": "cancelling", "run_id": str(run_id)}


@router.get("/{run_id}/diff")
async def get_run_diff(
    run_id: UUID,
    node_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get Git diff for Human-in-the-Loop approval panel.

    Queries the GitCheckpointManager for the diff between the initial
    checkpoint (before the run started) and the current HEAD of the
    sandbox used by the specified node (or the last node that executed).
    """
    engine = _require_engine()

    # Find the relevant node to get its sandbox_id and commit history
    result = await db.execute(
        select(NodeExecution)
        .where(NodeExecution.run_id == run_id)
        .order_by(NodeExecution.started_at.desc())
    )
    nodes = result.scalars().all()
    if not nodes:
        return {"run_id": str(run_id), "diff": "", "error": "No executed nodes found"}

    # Find the target node or use the most recent one
    target_node = None
    if node_id:
        target_node = next((n for n in nodes if n.node_id == node_id), None)
    if target_node is None:
        target_node = nodes[0]

    # Get the commit map from the engine to find the initial and current commits
    run_state = engine._runs.get(str(run_id))
    commit_map: dict = {}
    sandbox_map: dict = {}
    if run_state and isinstance(run_state, dict):
        commit_map = run_state.get("_commit_map", {})
        sandbox_map = run_state.get("_sandbox_map", {})

    # Find the sandbox_id for the target node
    sandbox_id = sandbox_map.get(target_node.node_id)
    if not sandbox_id:
        return {"run_id": str(run_id), "diff": "", "node_id": target_node.node_id}

    # Get the initial commit (first checkpoint) for this run
    initial_hash = commit_map.get("_initial")
    if not initial_hash:
        # Fallback: get the first commit in the log
        try:
            from app.sandbox.checkpoint import GitCheckpointManager
            log = await engine._checkpoint.get_log(sandbox_id, max_entries=50) if hasattr(engine._checkpoint, 'get_log') else []
            if log:
                initial_hash = log[-1]["hash"]  # oldest commit
        except Exception:
            pass

    if not initial_hash:
        return {"run_id": str(run_id), "diff": "", "node_id": target_node.node_id}

    # Get diff from initial commit to HEAD
    try:
        diff_text = await engine._checkpoint.get_diff(sandbox_id, initial_hash)
    except Exception as exc:
        logger.warning("Failed to get diff for run %s: %s", run_id, exc)
        diff_text = ""

    return {"run_id": str(run_id), "diff": diff_text, "node_id": target_node.node_id}


@router.get("/{run_id}/nodes", response_model=list[NodeExecutionResponse])
async def get_run_nodes(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get node execution details for a run."""
    result = await db.execute(
        select(NodeExecution)
        .where(NodeExecution.run_id == run_id)
        .order_by(NodeExecution.started_at)
    )
    return result.scalars().all()


class RollbackRequest(BaseModel):
    """Request body for rollback endpoint."""
    node_id: str = Field(..., description="Node ID to rollback to (its commit will be restored)")
    reason: str = Field("", description="Reason for rollback")


@router.post("/{run_id}/approve")
async def approve_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Human-in-the-Loop approval — resume a paused run."""
    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != "paused":
        raise HTTPException(
            status_code=400,
            detail=f"Run is in status '{run.status}', expected 'paused'",
        )

    # Signal approval to the engine
    _approval_results[str(run_id)] = {"approved": True, "reason": ""}
    event = _approval_events.get(str(run_id))
    if event:
        event.set()

    # Update DB
    run.status = "running"
    await db.flush()

    return {"status": "approved", "run_id": str(run_id)}


@router.post("/{run_id}/reject")
async def reject_run(
    run_id: UUID,
    reason: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Human-in-the-Loop rejection — mark run as rejected/failed."""
    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != "paused":
        raise HTTPException(
            status_code=400,
            detail=f"Run is in status '{run.status}', expected 'paused'",
        )

    # Signal rejection to the engine
    _approval_results[str(run_id)] = {"approved": False, "reason": reason}
    event = _approval_events.get(str(run_id))
    if event:
        event.set()

    # Update DB
    run.status = "failed"
    await db.flush()

    return {"status": "rejected", "run_id": str(run_id), "reason": reason}


@router.post("/{run_id}/rollback")
async def rollback_run(
    run_id: UUID,
    body: RollbackRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rollback a run's sandbox to the checkpoint of a specific node.

    Uses GitCheckpointManager.rollback() to restore the workspace
    files to the state they were in after *node_id* executed.
    """
    engine = _require_engine()

    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Get the commit_map and sandbox_map from the engine's run state
    run_state = engine._runs.get(str(run_id))
    if not run_state:
        raise HTTPException(status_code=404, detail="Run execution state not found")

    commit_map: dict = run_state.get("_commit_map", {})
    sandbox_map: dict = run_state.get("_sandbox_map", {})

    # Find the commit hash and sandbox for the target node
    commit_hash = commit_map.get(body.node_id)
    sandbox_id = sandbox_map.get(body.node_id)

    if not commit_hash:
        raise HTTPException(
            status_code=404,
            detail=f"No checkpoint found for node '{body.node_id}'",
        )
    if not sandbox_id:
        raise HTTPException(
            status_code=404,
            detail=f"No sandbox found for node '{body.node_id}'",
        )

    # Execute rollback
    try:
        await engine._checkpoint.rollback(sandbox_id, commit_hash)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Rollback failed: {exc}",
        ) from exc

    logger.info(
        "Rolled back run %s to node %s (commit %s)",
        run_id, body.node_id, commit_hash[:12],
    )

    return {
        "status": "rolled_back",
        "run_id": str(run_id),
        "node_id": body.node_id,
        "commit_hash": commit_hash,
        "reason": body.reason,
    }
