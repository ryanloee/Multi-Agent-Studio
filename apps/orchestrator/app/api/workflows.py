"""Workflow CRUD endpoints.

POST   /           - Create workflow
GET    /           - List workflows
GET    /{id}       - Get workflow detail
PUT    /{id}       - Update workflow (save DAG)
POST   /{id}/assess - Build lightweight project summary
DELETE /{id}       - Delete workflow
"""

import json
from pathlib import Path
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

router = APIRouter()


def _read_text(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _lightweight_project_summary(workspace: Path) -> dict[str, object]:
    files = {
        "package.json": workspace / "package.json",
        "pyproject.toml": workspace / "pyproject.toml",
        "requirements.txt": workspace / "requirements.txt",
        "go.mod": workspace / "go.mod",
        "Cargo.toml": workspace / "Cargo.toml",
        "README.md": workspace / "README.md",
        "README": workspace / "README",
        "Makefile": workspace / "Makefile",
        "docker-compose.yml": workspace / "docker-compose.yml",
    }
    key_dirs = [
        name for name in ("src", "app", "apps", "backend", "frontend", "services", "packages", "tests", "docs")
        if (workspace / name).exists()
    ]
    stack: list[str] = []
    project_type = "unknown"
    startup = []
    build = []
    tests = []

    package_json = files["package.json"]
    if package_json.exists():
        project_type = "node"
        stack.append("Node.js")
        try:
            payload = json.loads(_read_text(package_json, limit=12000) or "{}")
        except json.JSONDecodeError:
            payload = {}
        deps = {
            **(payload.get("dependencies") or {}),
            **(payload.get("devDependencies") or {}),
        }
        for key in ("next", "react", "vite", "express", "tailwindcss", "typescript", "prisma"):
            if key in deps:
                stack.append(key)
        scripts = payload.get("scripts") or {}
        if isinstance(scripts, dict):
            if scripts.get("dev"):
                startup.append(f"npm run dev -> {scripts['dev']}")
            if scripts.get("build"):
                build.append(f"npm run build -> {scripts['build']}")
            for key in ("test", "lint"):
                if scripts.get(key):
                    tests.append(f"npm run {key} -> {scripts[key]}")

    pyproject = files["pyproject.toml"]
    requirements = files["requirements.txt"]
    if pyproject.exists() or requirements.exists():
        project_type = "python" if project_type == "unknown" else project_type
        stack.append("Python")
        pyproject_text = _read_text(pyproject, limit=12000)
        requirements_text = _read_text(requirements, limit=12000)
        for key in ("fastapi", "django", "flask", "pytest", "sqlalchemy", "pydantic"):
            if key in pyproject_text or key in requirements_text:
                stack.append(key)
        if "pytest" in pyproject_text or "pytest" in requirements_text:
            tests.append("pytest")

    if files["go.mod"].exists():
        project_type = "go" if project_type == "unknown" else project_type
        stack.extend(["Go", "go.mod"])
    if files["Cargo.toml"].exists():
        project_type = "rust" if project_type == "unknown" else project_type
        stack.extend(["Rust", "cargo"])

    readme_path = files["README.md"] if files["README.md"].exists() else files["README"]
    readme_excerpt = _read_text(readme_path, limit=1200).strip()

    risk_points = []
    if not readme_excerpt:
        risk_points.append("缺少 README 或启动说明，首次接手成本较高")
    if not startup:
        risk_points.append("未自动识别到明确启动命令，需要进一步探索")
    if not tests:
        risk_points.append("未识别到明显测试入口，回归验证成本较高")

    return {
        "project_type": project_type,
        "tech_stack": sorted({item for item in stack if item}),
        "startup": startup,
        "build": build,
        "tests": tests,
        "key_directories": key_dirs,
        "readme_excerpt": readme_excerpt,
        "risk_points": risk_points,
        "entry_points": {
            "package_json": str(package_json.relative_to(workspace)) if package_json.exists() else None,
            "pyproject": str(pyproject.relative_to(workspace)) if pyproject.exists() else None,
            "requirements": str(requirements.relative_to(workspace)) if requirements.exists() else None,
        },
        "suggested_next_steps": [
            "先基于项目摘要确认真实目标和约束",
            "让 Planner 在现状摘要基础上生成或收缩 DAG",
            "运行前补齐工作目录、模型策略和关键阻塞项",
        ],
    }


def _default_workspace_directory() -> str:
    """Read the configured default workspace without failing workflow creation."""
    try:
        from app.api.settings import _read_settings

        settings = _read_settings()
        general = settings.get("general", {}) if isinstance(settings, dict) else {}
        return str(general.get("default_workspace") or "").strip()
    except Exception:
        return ""


@router.post("", response_model=WorkflowResponse, status_code=201)
async def create_workflow(
    body: CreateWorkflowRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create workflow from React Flow JSON."""
    workflow = Workflow(
        name=body.name,
        description=body.description,
        dag_json=body.dag_json,
        workspace_directory=body.workspace_directory or _default_workspace_directory() or None,
        mode="auto",
        goal=body.goal,
        lifecycle_phase="draft",
        blockers_json=[],
    )

    # Auto mode uses Planner chat as an internal planning service. Do not store
    # that top-level Planner as an executable canvas node.
    if body.dag_json is None:
        workflow.dag_json = {
            "nodes": [],
            "edges": [],
            "metadata": body.metadata or {},
        }
    elif body.metadata is not None:
        workflow.dag_json = {
            **(workflow.dag_json or {}),
            "metadata": body.metadata,
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

    if body.name is not None:
        workflow.name = body.name
    if body.description is not None:
        workflow.description = body.description
    existing_metadata = (
        workflow.dag_json.get("metadata", {})
        if isinstance(workflow.dag_json, dict) and isinstance(workflow.dag_json.get("metadata"), dict)
        else {}
    )
    if body.dag_json is not None:
        next_dag = body.dag_json
        if isinstance(next_dag, dict):
            incoming_metadata = next_dag.get("metadata", {}) if isinstance(next_dag.get("metadata"), dict) else {}
            incoming_node_count = len(next_dag.get("nodes") or []) if isinstance(next_dag.get("nodes"), list) else 0
            existing_node_count = (
                len(workflow.dag_json.get("nodes") or [])
                if isinstance(workflow.dag_json, dict) and isinstance(workflow.dag_json.get("nodes"), list)
                else 0
            )
            if (
                workflow.mode == "auto"
                and incoming_node_count == 0
                and existing_node_count > 0
                and workflow.lifecycle_phase in {"planning", "ready", "running"}
            ):
                next_dag = {
                    **(workflow.dag_json or {"nodes": [], "edges": []}),
                    "metadata": {**existing_metadata, **incoming_metadata},
                }
            else:
                next_dag = {
                    **next_dag,
                    "metadata": {**existing_metadata, **incoming_metadata},
                }
        workflow.dag_json = next_dag
    if body.metadata is not None:
        current_metadata = (
            workflow.dag_json.get("metadata", {})
            if isinstance(workflow.dag_json, dict) and isinstance(workflow.dag_json.get("metadata"), dict)
            else {}
        )
        workflow.dag_json = {
            **(workflow.dag_json or {"nodes": [], "edges": []}),
            "metadata": {**current_metadata, **body.metadata},
        }
    if body.workspace_directory is not None:
        workflow.workspace_directory = body.workspace_directory
    if body.mode is not None:
        workflow.mode = "auto"
    if body.goal is not None:
        workflow.goal = body.goal
    if body.lifecycle_phase is not None:
        workflow.lifecycle_phase = body.lifecycle_phase
    if body.blockers is not None:
        workflow.blockers_json = body.blockers
    if body.project_summary is not None:
        workflow.project_summary_json = body.project_summary

    await db.flush()
    await db.refresh(workflow)
    return workflow


@router.post("/{workflow_id}/assess", response_model=WorkflowResponse)
async def assess_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workspace_directory = (workflow.workspace_directory or "").strip()
    if not workspace_directory:
        workflow.lifecycle_phase = "review"
        workflow.blockers_json = [{
            "code": "workspace_missing",
            "message": "未设置工作目录，无法进行项目评估。",
        }]
        await db.commit()
        raise HTTPException(status_code=400, detail="请先设置工作目录后再执行项目评估")

    workspace = Path(workspace_directory).expanduser()
    if not workspace.exists() or not workspace.is_dir():
        workflow.lifecycle_phase = "review"
        workflow.blockers_json = [{
            "code": "workspace_missing",
            "message": f"工作目录不存在或不可用: {workspace_directory}",
        }]
        await db.commit()
        raise HTTPException(status_code=400, detail=f"工作目录不存在或不可用: {workspace_directory}")

    workflow.lifecycle_phase = "assessing"
    workflow.blockers_json = []
    await db.flush()

    summary = _lightweight_project_summary(workspace)
    workflow.project_summary_json = summary
    workflow.project_summary_artifact_id = f"project-summary:{workflow.id}"
    workflow.lifecycle_phase = "planning"
    await db.commit()
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
