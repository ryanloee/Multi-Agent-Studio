"""Workspace file browser API — list directory tree and read file contents.

All paths are scoped to the workflow's workspace_directory to prevent
path traversal outside the project root.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB read limit
MAX_DEPTH = 5
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".next", ".venv", "venv",
    ".idea", ".vscode", ".sandboxes", "dist", "build", ".cache",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


class FileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    children: list["FileEntry"] = Field(default_factory=list)


class ListTreeRequest(BaseModel):
    workspace: str = Field(..., description="Workspace root directory (absolute path)")
    subpath: str = Field(default="", description="Relative sub-path inside workspace")
    depth: int = Field(default=1, ge=0, le=MAX_DEPTH, description="Recursion depth (0=flat)")


class ListTreeResponse(BaseModel):
    root: str
    subpath: str
    entries: list[FileEntry] = Field(default_factory=list)
    error: str = ""


class ReadFileRequest(BaseModel):
    workspace: str = Field(..., description="Workspace root directory (absolute path)")
    path: str = Field(..., description="Relative file path inside workspace")


class ReadFileResponse(BaseModel):
    path: str
    content: str
    size: int
    mime_type: str
    truncated: bool = False
    error: str = ""


def _resolve_safe(workspace: str, subpath: str) -> Path:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace root does not exist: {root}")
    target = (root / subpath).resolve() if subpath else root
    if not str(target).startswith(str(root)):
        raise ValueError("Path traversal detected")
    return root, target


def _should_ignore(name: str) -> bool:
    return name in IGNORED_DIRS or name.startswith(".")


def _build_tree(dir_path: Path, root: Path, depth: int) -> list[FileEntry]:
    entries: list[FileEntry] = []
    try:
        children = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return entries
    for child in children:
        if _should_ignore(child.name):
            continue
        try:
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            continue
        entry = FileEntry(
            name=child.name,
            path=str(child.relative_to(root)),
            is_dir=is_dir,
            size=size,
        )
        if is_dir and depth > 0:
            entry.children = _build_tree(child, root, depth - 1)
        entries.append(entry)
    return entries


@router.post("/tree", response_model=ListTreeResponse)
async def list_tree(body: ListTreeRequest) -> ListTreeResponse:
    try:
        root, target = _resolve_safe(body.workspace, body.subpath)
    except ValueError as exc:
        return ListTreeResponse(
            root=body.workspace, subpath=body.subpath, entries=[], error=str(exc)
        )

    if not target.exists():
        return ListTreeResponse(
            root=str(root), subpath=body.subpath, entries=[], error="Path does not exist"
        )
    if not target.is_dir():
        return ListTreeResponse(
            root=str(root), subpath=body.subpath, entries=[], error="Path is not a directory"
        )

    entries = _build_tree(target, root, body.depth)
    return ListTreeResponse(
        root=str(root),
        subpath=body.subpath,
        entries=entries,
    )


@router.post("/read", response_model=ReadFileResponse)
async def read_file(body: ReadFileRequest) -> ReadFileResponse:
    try:
        root, target = _resolve_safe(body.workspace, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory, not a file")

    size = target.stat().st_size
    mime_type, _ = mimetypes.guess_type(str(target)) or "text/plain"
    truncated = size > MAX_FILE_SIZE

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        if truncated:
            content = content[:MAX_FILE_SIZE]
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}") from exc

    return ReadFileResponse(
        path=body.path,
        content=content,
        size=size,
        mime_type=mime_type or "text/plain",
        truncated=truncated,
    )
