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
    from app.core.local_engine import LocalDAGExecutor

logger = logging.getLogger("uvicorn.error")

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


def _extract_node_model(node: dict) -> str:
    data = node.get("data", {}) if isinstance(node, dict) else {}
    provider = (
        data.get("modelProvider")
        or data.get("model_provider")
        or node.get("model_provider")
        or ""
    )
    model_id = (
        data.get("modelId")
        or data.get("model_id")
        or node.get("model_id")
        or ""
    )
    if provider and model_id:
        return f"{provider}/{model_id}"
    return str(model_id or provider or "").strip()


def _workflow_has_model_fallback(workflow, agent_type: str) -> bool:
    dag_json = workflow.dag_json or {}
    metadata = dag_json.get("metadata", {}) if isinstance(dag_json, dict) else {}
    auto_map = metadata.get("auto_child_model_map", {}) if isinstance(metadata, dict) else {}
    if isinstance(auto_map, dict) and auto_map.get(agent_type):
        return True

    try:
        settings_path = Path(__file__).resolve().parents[3] / "data" / "settings.json"
        import json

        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        models = payload.get("models", [])
        has_model = isinstance(models, list) and any(
            isinstance(item, dict) and item.get("default_model")
            for item in models
        )
        logger.info(
            "Run model fallback lookup: agent_type=%s settings_path=%s has_model=%s",
            agent_type, settings_path, has_model,
        )
        return has_model
    except Exception:
        logger.exception("Run model fallback lookup failed for agent_type=%s", agent_type)
        return False


def _validate_run_request(
    workflow,
    dag_json: dict | None,
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    workspace_directory = (workflow.workspace_directory or "").strip()
    if not workspace_directory:
        blockers.append({
            "code": "workspace_missing",
            "message": "请先设置工作目录后再启动工作流。",
        })
    else:
        workspace_path = Path(workspace_directory).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            blockers.append({
                "code": "workspace_missing",
                "message": f"工作目录不存在或不可用: {workspace_directory}",
            })

    nodes = [
        node for node in (dag_json or {}).get("nodes", [])
        if isinstance(node, dict)
    ]
    executable_nodes = [node for node in nodes if node.get("id") != "planner"]
    logger.info(
        "Run preflight: workflow=%s workspace=%s nodes=%d executable_nodes=%d edges=%d",
        getattr(workflow, "id", ""),
        workspace_directory or "<missing>",
        len(nodes),
        len(executable_nodes),
        len((dag_json or {}).get("edges", []) or []),
    )
    if not executable_nodes:
        blockers.append({
            "code": "dag_empty",
            "message": "当前画布还没有可执行节点，不能启动运行。",
        })

    edges = [
        edge for edge in (dag_json or {}).get("edges", [])
        if isinstance(edge, dict)
    ]
    incoming: dict[str, int] = {}
    for edge in edges:
        target = str(edge.get("target") or "")
        if target:
            incoming[target] = incoming.get(target, 0) + 1

    for node in executable_nodes:
        agent_type = (
            node.get("type")
            or (node.get("data", {}) if isinstance(node.get("data"), dict) else {}).get("agentType")
            or "coder"
        )
        node_id = str(node.get("id") or "")
        if agent_type == "plan" and node_id != "planner":
            agent_type = "design"
        node_model = _extract_node_model(node)
        has_fallback = _workflow_has_model_fallback(workflow, agent_type)
        logger.info(
            "Run preflight node: workflow=%s node=%s type=%s model=%s fallback=%s",
            getattr(workflow, "id", ""),
            node_id,
            agent_type,
            node_model or "<missing>",
            has_fallback,
        )
        if agent_type == "merge" and incoming.get(node_id, 0) == 0:
            blockers.append({
                "code": "merge_missing_inputs",
                "message": f"Merge 节点 {node_id} 没有上游依赖，无法执行。",
            })
        if (
            agent_type in {"design", "plan", "coder", "explore", "merge", "review"}
            and not node_model
            and not has_fallback
        ):
            blockers.append({
                "code": "model_missing",
                "message": f"节点 {node_id} 缺少可解析模型，请在节点或模型策略中补齐。",
            })
    if blockers:
        logger.warning(
            "Run preflight blocked: workflow=%s blockers=%s",
            getattr(workflow, "id", ""),
            blockers,
        )
    else:
        logger.info("Run preflight passed: workflow=%s", getattr(workflow, "id", ""))
    return blockers


def _strip_internal_planner_node(dag_json: dict | None) -> dict:
    """Remove the top-level planner sentinel from executable auto DAGs."""
    if not isinstance(dag_json, dict):
        return {"nodes": [], "edges": []}

    nodes = [
        node for node in dag_json.get("nodes", [])
        if isinstance(node, dict) and node.get("id") != "planner"
    ]
    node_ids = {str(node.get("id")) for node in nodes if node.get("id")}
    edges = [
        edge for edge in dag_json.get("edges", [])
        if (
            isinstance(edge, dict)
            and str(edge.get("source") or "") in node_ids
            and str(edge.get("target") or "") in node_ids
        )
    ]
    metadata = dag_json.get("metadata", {}) if isinstance(dag_json.get("metadata"), dict) else {}
    return {"nodes": nodes, "edges": edges, "metadata": metadata}


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

    workspace_directory = (workflow.workspace_directory or "").strip()

    payload = body or TriggerRunRequest()
    global_config = payload.config or {}
    workflow_mode = "auto"
    dag_json: dict | None = None
    run_id = uuid4()
    start_as_task_dag = False
    workflow_metadata = (
        workflow.dag_json.get("metadata", {})
        if isinstance(workflow.dag_json, dict)
        else {}
    )

    # 2. Route based on mode
    if workflow_mode == "auto":
        goal = getattr(workflow, "goal", "") or ""
        saved_dag = payload.dag if payload.dag else (workflow.dag_json or {})
        saved_nodes = [
            node for node in saved_dag.get("nodes", [])
            if isinstance(node, dict) and node.get("id") != "planner"
        ]
        if not saved_nodes:
            from app.api.planner_chat import _extract_dag_from_text
            from app.models.db import ChatMessage as ChatMessageORM

            saw_unparsed_plan = False
            chat_result = await db.execute(
                select(ChatMessageORM)
                .where(
                    ChatMessageORM.workflow_id == workflow_id,
                    ChatMessageORM.node_id == "planner",
                    ChatMessageORM.role == "assistant",
                )
                .order_by(ChatMessageORM.created_at.desc())
            )
            for msg in chat_result.scalars().all():
                if "```plan" in (msg.content or ""):
                    saw_unparsed_plan = True
                recovered_dag = _extract_dag_from_text(msg.content or "")
                recovered_nodes = [
                    node for node in (recovered_dag or {}).get("nodes", [])
                    if isinstance(node, dict) and node.get("id") != "planner"
                ]
                if recovered_nodes:
                    saved_dag = recovered_dag or {}
                    saved_nodes = recovered_nodes
                    workflow.dag_json = saved_dag
                    db.add(workflow)
                    await db.flush()
                    logger.info(
                        "Recovered auto DAG for workflow %s from planner chat history (%d nodes)",
                        workflow_id, len(saved_nodes),
                    )
                    break
            if not saved_nodes and saw_unparsed_plan:
                workflow.lifecycle_phase = "blocked"
                workflow.blockers_json = [{
                    "code": "planner_dag_parse_failed",
                    "message": "Planner 曾输出结构化计划，但 JSON 不完整或无效，系统无法恢复画布节点。请让 Planner 重新生成更简洁的 DAG。",
                }]
                await db.commit()
                logger.warning(
                    "Run blocked by unrecoverable planner DAG: workflow=%s",
                    workflow_id,
                )
                raise HTTPException(status_code=400, detail=workflow.blockers_json[0])

        blockers = _validate_run_request(workflow, saved_dag)
        if blockers:
            workflow.lifecycle_phase = "blocked"
            workflow.blockers_json = blockers
            await db.commit()
            raise HTTPException(status_code=400, detail=blockers[0]["message"])
        if saved_nodes:
            try:
                compile_dag(_strip_internal_planner_node(saved_dag))
            except ValueError as exc:
                workflow.lifecycle_phase = "blocked"
                workflow.blockers_json = [{
                    "code": "dag_invalid",
                    "message": str(exc),
                }]
                await db.commit()
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        if saved_nodes:
            # Planner chat already produced a concrete DAG. Execute that
            # saved team directly and mirror every node to the task board.
            dag_json = _strip_internal_planner_node(saved_dag)
            start_as_task_dag = True
            global_config["_mode"] = "auto"
            global_config["_goal"] = goal
        else:
            workflow.lifecycle_phase = "blocked"
            workflow.blockers_json = [{
                "code": "dag_empty",
                "message": "当前只有 Planner 规划，没有形成可执行 DAG。请先在 Planner 中完成方案并进入 Ready。",
            }]
            await db.commit()
            raise HTTPException(status_code=400, detail=workflow.blockers_json[0]["message"])
    else:
        # Manual mode (default): compile the user-drawn DAG
        dag_json = payload.dag if payload.dag else (workflow.dag_json or {})
        if not dag_json:
            raise HTTPException(status_code=400, detail="No DAG definition found")
        blockers = _validate_run_request(workflow, dag_json)
        if blockers:
            workflow.lifecycle_phase = "blocked"
            workflow.blockers_json = blockers
            await db.commit()
            raise HTTPException(status_code=400, detail=blockers[0]["message"])

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
                if agent_type == "plan" and node_def.get("id", "") != "planner":
                    agent_type = "design"
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

    global_config["_edges"] = (dag_json or workflow.dag_json or {}).get("edges", [])
    global_config["_workflow_id"] = str(workflow_id)
    global_config["_auto_child_model_map"] = workflow_metadata.get("auto_child_model_map", {})

    # 5. Persist run record before starting background execution. Dynamic
    # tasks are inserted from a separate DB session and need this FK visible.
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

    # 6. Start execution via LocalDAGExecutor
    if start_as_task_dag:
        await engine.start_task_dag(
            run_id=str(run_id),
            dag_json=dag_json or {},
            global_config=global_config,
            workspace_directory=workspace_directory,
        )
    else:
        await engine.start_workflow(
            run_id=str(run_id),
            layers=layers_data,
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
            if not hasattr(engine._checkpoint, 'get_log'):
                log = []
            else:
                log = await engine._checkpoint.get_log(
                    sandbox_id, max_entries=50,
                )
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
