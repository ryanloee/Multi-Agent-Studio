"""Run management endpoints.

POST   /{workflow_id}/run    - Trigger workflow execution
GET    /                     - List run records
GET    /{run_id}             - Get run status
POST   /{run_id}/cancel      - Cancel a running workflow
GET    /{run_id}/diff         - Get Git diff (Human-in-the-Loop)
GET    /{run_id}/nodes        - Get node execution details
"""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.config import settings
from app.core.database import get_db
from app.models.db import Run, NodeExecution
from app.models.schemas import TriggerRunRequest, RunResponse, NodeExecutionResponse

router = APIRouter()

# Lazy Temporal client singleton
_temporal_client: Client | None = None


async def _get_temporal_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
    return _temporal_client


@router.post("/{workflow_id}/run", response_model=RunResponse, status_code=201)
async def trigger_run(
    workflow_id: UUID,
    body: TriggerRunRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Trigger workflow execution via Temporal DAGWorkflow.

    Steps:
    1. Look up workflow from DB to get DAG JSON.
    2. Override DAG with request body if provided.
    3. Compile DAG into execution layers via compiler.compile_dag().
    4. Convert layers into DAGParams.
    5. Start DAGWorkflow as a Temporal workflow.
    6. Persist run record to DB and return it.
    """
    from app.workflows.compiler import compile_dag
    from app.workflows.dag_workflow import DAGParams, DAGLayer, DAGNode, DAGWorkflow

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

    # 4. Convert to DAGParams
    run_id = uuid4()
    layers: list[DAGLayer] = []
    for layer_nodes in compiled_layers:
        dag_nodes = []
        for node_def in layer_nodes:
            node_data = node_def.get("data", node_def)
            dag_nodes.append(DAGNode(
                id=node_def.get("id", ""),
                agent_type=node_data.get("agent_type", "build"),
                model_provider=node_data.get("model_provider", ""),
                model_id=node_data.get("model_id", ""),
                prompt=node_data.get("prompt", ""),
                upstream_ids=node_data.get("upstream_ids", []),
                extra=node_data.get("extra", {}),
            ))
        layers.append(DAGLayer(nodes=dag_nodes))

    params = DAGParams(
        run_id=str(run_id),
        layers=layers,
        global_config=global_config,
    )

    # 5. Start DAGWorkflow via Temporal
    client = await _get_temporal_client()
    handle = await client.start_workflow(
        DAGWorkflow.run,
        params,
        id=f"dag-{workflow_id}-{run_id}",
        task_queue=settings.temporal_task_queue,
    )

    # 6. Persist run record
    run = Run(
        id=run_id,
        workflow_id=workflow_id,
        status="running",
        temporal_workflow_id=handle.id,
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

    # Optionally enrich with Temporal status
    if run.temporal_workflow_id and run.status == "running":
        try:
            client = await _get_temporal_client()
            handle = client.get_workflow_handle(run.temporal_workflow_id)
            descr = await handle.describe()
            if descr.status:
                temporal_status = str(descr.status.name).lower()
                if temporal_status == "completed":
                    run.status = "completed"
                elif temporal_status == "failed":
                    run.status = "failed"
                elif temporal_status == "cancelled":
                    run.status = "cancelled"
        except Exception:
            pass  # Temporal not available, return DB status

    return run


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Cancel a running workflow via Temporal."""
    result = await db.execute(
        select(Run).where(Run.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel run in status '{run.status}'")

    if not run.temporal_workflow_id:
        raise HTTPException(status_code=400, detail="No Temporal workflow associated")

    # Cancel via Temporal
    client = await _get_temporal_client()
    handle = client.get_workflow_handle(run.temporal_workflow_id)
    await handle.cancel()

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
    """Human-in-the-Loop approval via Temporal Signal."""
    # TODO: Send Temporal Signal for HITL approval
    return {"status": "approved", "run_id": run_id}
