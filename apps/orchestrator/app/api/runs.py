"""Run management endpoints.

POST   /{workflow_id}/run    - Trigger workflow execution
GET    /                     - List run records
GET    /{run_id}             - Get run status
POST   /{run_id}/cancel      - Cancel a running workflow
GET    /{run_id}/diff         - Get Git diff (Human-in-the-Loop)
GET    /{run_id}/nodes        - Get node execution details
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Run, NodeExecution
from app.models.schemas import TriggerRunRequest, RunResponse, NodeExecutionResponse

if TYPE_CHECKING:
    from app.core.local_engine import LocalDAGExecutor

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


@router.post("/{workflow_id}/run", response_model=RunResponse, status_code=201)
async def trigger_run(
    workflow_id: UUID,
    body: TriggerRunRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Trigger workflow execution via the local DAG engine.

    Steps:
    1. Look up workflow from DB to get DAG JSON.
    2. Override DAG with request body if provided.
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

    # 2. Merge DAG: request override > stored DAG
    payload = body or TriggerRunRequest()
    dag_json = payload.dag if payload.dag else (workflow.dag_json or {})
    global_config = payload.config or {}

    if not dag_json:
        raise HTTPException(status_code=400, detail="No DAG definition found")

    # 3. Compile DAG into layers
    try:
        compiled_layers = compile_dag(dag_json)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 4. Serialise layers into dicts for the engine
    #    Frontend sends camelCase (agentType, modelProvider, modelId).
    #    Engine expects snake_case — normalise here.
    run_id = uuid4()
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

    # 5. Start DAG execution via LocalDAGExecutor
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
async def get_run_diff(run_id: str):
    """Get Git diff for Human-in-the-Loop approval panel.

    This queries the GitCheckpointManager for the latest checkpoint diff.
    """
    # TODO: Integrate with GitCheckpointManager once sandbox tracking is available
    return {"run_id": run_id, "diff": ""}


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


@router.post("/{run_id}/approve")
async def approve_run(run_id: str):
    """Human-in-the-Loop approval."""
    # TODO: Implement HITL approval via engine
    return {"status": "approved", "run_id": run_id}
