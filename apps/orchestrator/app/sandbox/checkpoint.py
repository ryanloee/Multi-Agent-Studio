"""Git-based checkpoint with directory separation for anti-suicide protection.

.git metadata is stored in /sandbox-meta/.git (Agent cannot access).
Work tree is /workspace (Agent can read/write, but cannot destroy checkpoints).

Node containers are isolated under /workspace/.mas/containers/{node_id}/.
After node completion, changes are committed and merged to the main workspace.

All git operations use subprocess.run([...]) directly — no shell invocations.
File operations use Python pathlib/shutil — no bash commands.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)


class GitCheckpointManager:
    """Git-based checkpoint with directory separation for anti-suicide protection."""

    GIT_DIR = "/sandbox-meta/.git"
    WORK_TREE = "/workspace"

    def __init__(self, sandbox_manager: LocalSandbox) -> None:
        self.sandbox = sandbox_manager

    def _resolve_paths(self, sandbox_id: str) -> tuple[Path, Path]:
        """Resolve virtual paths to real host paths.

        Prefers separated git dir (/sandbox-meta/.git) when it exists
        (created by SandboxProvisioner). Falls back to /workspace/.git
        when the sandbox was created without provisioning (e.g. DirectorLoop).
        """
        work_tree = Path(self.sandbox.resolve_virtual_path(sandbox_id, self.WORK_TREE))
        git_dir = Path(self.sandbox.resolve_virtual_path(sandbox_id, self.GIT_DIR))
        if not git_dir.exists():
            # Fallback: sandbox was created without provision()
            git_dir = work_tree / ".git"
        return git_dir, work_tree

    async def _run_git(self, sandbox_id: str, *args: str) -> subprocess.CompletedProcess:
        """Run a git command directly via subprocess (no shell).

        Args are passed as-is to git, e.g. ("add", "-A"), ("commit", "-m", "msg").
        """
        git_dir, work_tree = self._resolve_paths(sandbox_id)
        cmd = [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={work_tree}",
            *args,
        ]

        def _run():
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(work_tree),
            )

        result = await asyncio.to_thread(_run)
        if result.returncode != 0:
            logger.debug(
                "git %s failed (exit=%d): %s",
                " ".join(args[:3]),
                result.returncode,
                result.stderr[:300],
            )
        return result

    async def init_repo(self, sandbox_id: str) -> None:
        """Initialize separated Git repo in sandbox."""
        git_dir, work_tree = self._resolve_paths(sandbox_id)
        git_dir.mkdir(parents=True, exist_ok=True)

        await self._run_git(sandbox_id, "init")
        await self._run_git(sandbox_id, "config", "user.email", "orchestrator@mas.local")
        await self._run_git(sandbox_id, "config", "user.name", "MAS Orchestrator")

    async def auto_commit(self, sandbox_id: str, message: str) -> str:
        """Auto-commit before node execution. Agent is unaware of this."""
        await self._run_git(
            sandbox_id,
            "add", "-A", "--", ".",
            ":(exclude).agent/**",
            ":(exclude).workflow/**",
            ":(exclude).mas/**",
        )
        await self._run_git(
            sandbox_id,
            "commit", "-m", message, "--allow-empty",
        )
        result = await self._run_git(sandbox_id, "rev-parse", "HEAD")
        return result.stdout.strip()

    async def rollback(self, sandbox_id: str, commit_hash: str) -> None:
        """Rollback to checkpoint on failure or Review Reject."""
        await self._run_git(sandbox_id, "reset", "--hard", commit_hash)

    async def get_diff(self, sandbox_id: str, from_hash: str) -> str:
        """Get diff for Human-in-the-Loop approval panel."""
        result = await self._run_git(sandbox_id, "diff", from_hash, "HEAD")
        return result.stdout

    # ------------------------------------------------------------------
    # Node container operations
    # ------------------------------------------------------------------

    async def commit_node_changes(self, sandbox_id: str, node_id: str, message: str) -> str:
        """Commit changes in a node container.

        This creates a snapshot of the node's work that can be merged later.
        Returns the commit hash.
        """
        container_path = f"/workspace/.mas/containers/{node_id}"

        await self._run_git(
            sandbox_id,
            "add", f"{container_path}/", "--",
            f"{container_path}/",
            f":(exclude){container_path}/.agent/**",
        )
        await self._run_git(
            sandbox_id,
            "commit", "-m", f"node:{node_id} - {message}", "--allow-empty",
        )
        result = await self._run_git(sandbox_id, "rev-parse", "HEAD")
        return result.stdout.strip()

    async def merge_node_to_main(self, sandbox_id: str, node_id: str) -> bool:
        """Merge node container changes to the main workspace.

        Strategy: copy changed files from container to workspace root using
        Python shutil (no shell commands). Then commit the merged changes.
        Returns True if successful.
        """
        container_host = self.sandbox.resolve_virtual_path(
            sandbox_id, f"/workspace/.mas/containers/{node_id}",
        )
        container_path = Path(container_host)
        _, work_tree = self._resolve_paths(sandbox_id)

        if not container_path.is_dir():
            return True

        # Copy files from container to workspace root (excluding .agent and prompt.txt)
        skip_names = {".agent"}
        skip_files = {"prompt.txt"}

        try:
            for item in container_path.rglob("*"):
                if not item.is_file():
                    continue
                # Skip .agent directory
                rel = item.relative_to(container_path)
                if any(part in skip_names for part in rel.parts):
                    continue
                if item.name in skip_files:
                    continue

                dst = work_tree / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(dst))
        except Exception as exc:
            logger.warning("merge_node_to_main copy failed for node %s: %s", node_id, exc)
            return False

        # Commit the merged changes
        await self._run_git(
            sandbox_id,
            "add", "-A", "--", ".",
            ":(exclude).agent/**",
            ":(exclude).workflow/**",
            ":(exclude).mas/**",
        )
        await self._run_git(
            sandbox_id,
            "commit", "-m", f"merge:node:{node_id}", "--allow-empty",
        )

        return True

    async def get_node_diff(self, sandbox_id: str, node_id: str) -> str:
        """Get diff of changes in a node container."""
        container_path = f"/workspace/.mas/containers/{node_id}"
        result = await self._run_git(sandbox_id, "diff", "HEAD", "--", f"{container_path}/")
        return result.stdout
