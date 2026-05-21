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
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import NodeExecution, Run, RunEvent
from app.models.schemas import NodeExecutionResponse, RunEventResponse, RunResponse, TriggerRunRequest

if TYPE_CHECKING:
    from app.core.director_loop import DirectorLoop

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

# Module-level engine singleton — initialised by main.py lifespan
_engine: DirectorLoop | None = None


def init_engine(engine: DirectorLoop) -> None:
    """Set the module-level director loop engine. Called once during app startup."""
    global _engine
    _engine = engine


def _require_engine() -> DirectorLoop:
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
    """Trigger workflow execution via the Director dispatch loop.

    Steps:
    1. Look up workflow from DB to get goal and workspace.
    2. Validate workspace exists.
    3. Start Director loop in the background via start_run().
    4. Persist run record to DB and return it.
    """

    engine = _require_engine()

    # 1. Fetch workflow
    from app.models.db import Workflow
    wf_result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = wf_result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workspace_directory = (workflow.workspace_directory or "").strip()
    goal = getattr(workflow, "goal", "") or ""

    # 2. Validate
    if not workspace_directory:
        raise HTTPException(status_code=400, detail="请先设置工作目录后再启动工作流。")

    workspace_path = Path(workspace_directory).expanduser()
    if not workspace_path.exists() or not workspace_path.is_dir():
        raise HTTPException(status_code=400, detail=f"工作目录不存在或不可用: {workspace_directory}")

    if not goal:
        raise HTTPException(status_code=400, detail="请先设置工作目标后再启动工作流。")

    payload = body or TriggerRunRequest()
    global_config = payload.config or {}
    run_id = uuid4()

    global_config["_goal"] = goal
    global_config["_workflow_id"] = str(workflow_id)
    dag_json = workflow.dag_json or {}

    # 3. Persist run record before starting background execution
    workflow.lifecycle_phase = "running"
    workflow.blockers_json = []
    run = Run(
        id=run_id,
        workflow_id=workflow_id,
        status="running",
        engine_workflow_id=str(run_id),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # 4. Start execution via DirectorLoop
    await engine.start_run(
        run_id=str(run_id),
        dag_json=dag_json,
        global_config=global_config,
        workspace_directory=workspace_directory,
    )

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


@router.get("/{run_id}/events", response_model=list[RunEventResponse])
async def list_run_events(
    run_id: UUID,
    limit: int = 5000,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Return persisted white-box event history for a run.

    The frontend uses this to restore LLM, shell, tool, communication, and
    timeline panels after refresh or after reconnecting to an in-progress run.
    """
    run_result = await db.execute(select(Run.id).where(Run.id == run_id))
    if run_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Run not found")

    query = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.created_at.asc(), RunEvent.id.asc())
        .offset(offset)
        .limit(min(max(limit, 1), 20000))
    )
    result = await db.execute(query)
    return result.scalars().all()


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

    # Cancel via DirectorLoop
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
    """Get Git diff for the run's shared sandbox.

    Returns the diff from the first checkpoint to the current HEAD.
    """
    engine = _require_engine()
    checkpoint = engine._checkpoint

    # Find the sandbox from engine run state
    run_state = engine._runs.get(str(run_id))
    if not run_state:
        return {"run_id": str(run_id), "diff": "", "error": "Run state not found"}

    workspace_directory = run_state.get("workspace_directory")
    if not workspace_directory:
        return {"run_id": str(run_id), "diff": ""}

    # Use the workspace directory to get diff
    try:
        import asyncio
        diff_text, _ = await engine._sandbox.exec(
            # Find the director sandbox for this run
            list(engine._sandbox._containers.keys())[-1] if engine._sandbox._containers else "",
            'git --git-dir="/sandbox-meta/.git" --work-tree="/workspace" diff HEAD~5 HEAD 2>/dev/null || git --git-dir="/sandbox-meta/.git" --work-tree="/workspace" diff 2>/dev/null || true',
        )
    except Exception as exc:
        logger.warning("Failed to get diff for run %s: %s", run_id, exc)
        diff_text = ""

    return {"run_id": str(run_id), "diff": diff_text}


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
    """Rollback a run's workspace to a specific commit.

    In the Director architecture, this rolls back the shared sandbox.
    """
    engine = _require_engine()

    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Get workspace directory from the run's workflow
    from app.models.db import Workflow
    wf_result = await db.execute(
        select(Workflow).where(Workflow.id == run.workflow_id)
    )
    workflow = wf_result.scalar_one_or_none()
    if not workflow or not workflow.workspace_directory:
        raise HTTPException(status_code=404, detail="Workflow or workspace not found")

    workspace_path = Path(workflow.workspace_directory).expanduser()

    # Use git to rollback in the workspace directly
    import asyncio
    try:
        commit_hash = body.node_id  # In new arch, node_id field reused for commit hash
        if not commit_hash:
            raise HTTPException(status_code=400, detail="Commit hash required")

        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", commit_hash,
            cwd=str(workspace_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(stderr.decode(errors="replace"))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Rollback failed: {exc}",
        ) from exc

    logger.info("Rolled back run %s to commit %s", run_id, commit_hash[:12])

    return {
        "status": "rolled_back",
        "run_id": str(run_id),
        "commit_hash": commit_hash,
        "reason": body.reason,
    }


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Resume a failed/interrupted run from its last checkpoint.

    The Director will pick up from the saved WorldModel state, skipping
    already-completed iterations and the initial scout phase.
    """
    engine = _require_engine()

    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in ("failed", "completed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot resume run in status '{run.status}'. "
                "Only failed/completed/cancelled runs can be resumed."
            ),
        )

    checkpoint = run.checkpoint_json
    if not checkpoint or not checkpoint.get("world_model_json"):
        raise HTTPException(
            status_code=400,
            detail="No checkpoint found for this run. Cannot resume.",
        )

    sandbox_id = checkpoint.get("sandbox_id")
    if sandbox_id:
        sandbox_dir = Path(engine._sandbox.root) / sandbox_id
        if not sandbox_dir.exists():
            raise HTTPException(
                status_code=400,
                detail="Sandbox directory no longer exists. Cannot resume from checkpoint.",
            )

    global_config = checkpoint.get("global_config", {})
    dag_json = checkpoint.get("dag_json", {})
    workspace_directory = checkpoint.get("workspace_directory", "")

    run.status = "running"
    run.completed_at = None
    await db.flush()

    await engine.start_run(
        run_id=str(run_id),
        dag_json=dag_json,
        global_config=global_config,
        workspace_directory=workspace_directory,
        resume_from=checkpoint,
    )

    logger.info(
        "Resumed run %s from checkpoint iteration %d",
        run_id, checkpoint.get("checkpoint_iteration", 0),
    )

    return {
        "status": "running",
        "run_id": str(run_id),
        "resumed_from_iteration": checkpoint.get("checkpoint_iteration", 0),
    }
