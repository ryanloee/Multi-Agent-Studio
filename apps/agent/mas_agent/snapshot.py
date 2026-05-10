"""SnapshotManager — automatic git commits for agent workspace changes."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mas_agent.events import StreamWriter

logger = logging.getLogger(__name__)


class SnapshotManager:
    """Manages automatic git snapshots of the workspace.

    Each mutating tool call (edit, write) triggers an auto-commit so that
    the workspace can be rolled back to any prior state.
    """

    def __init__(
        self,
        workspace: str,
        stream_writer: "StreamWriter | None" = None,
    ) -> None:
        self.workspace = workspace
        self.stream = stream_writer

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_git(self, *args: str) -> tuple[int, str, str]:
        """Run a git command in the workspace directory.

        Returns (return_code, stdout, stderr).
        """
        cmd = ["git", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    async def _ensure_git_initialized(self) -> bool:
        """Make sure the workspace has a git repo.

        Runs ``git init`` if needed, and configures ``user.email`` /
        ``user.name`` for the agent if not already set.

        Returns True on success, False on failure.
        """
        git_dir = os.path.join(self.workspace, ".git")

        # Initialize repo if .git does not exist
        if not os.path.isdir(git_dir):
            rc, _, _ = await self._run_git("init")
            if rc != 0:
                logger.warning("git init failed in %s", self.workspace)
                return False

        # Configure agent identity if not already set (local to this repo).
        # Use --local to only check/set the repo-level config so we don't
        # rely on (or overwrite) the user's global git identity.
        rc, out, _ = await self._run_git("config", "--local", "user.email")
        if rc != 0 or not out:
            await self._run_git("config", "--local", "user.email", "agent@mas-agent.local")

        rc, out, _ = await self._run_git("config", "--local", "user.name")
        if rc != 0 or not out:
            await self._run_git("config", "--local", "user.name", "MAS Agent")

        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def auto_commit(
        self, tool_name: str, description: str = ""
    ) -> str | None:
        """Create an automatic snapshot commit.

        Stages all changes (``git add -A``) and commits with a descriptive
        message.  If the workspace has no changes, the commit is silently
        skipped (not an error).

        Returns the commit hash on success, or ``None`` on failure /
        nothing-to-commit.
        """
        try:
            ok = await self._ensure_git_initialized()
            if not ok:
                return None

            # Stage everything
            await self._run_git("add", "-A")

            # Build commit message
            msg = f"agent: {tool_name}"
            if description:
                msg += f" - {description}"

            rc, stdout, stderr = await self._run_git(
                "commit", "-m", msg
            )

            if rc == 0:
                # Retrieve the short hash of the commit we just made
                rc2, hash_out, _ = await self._run_git("rev-parse", "HEAD")
                if rc2 == 0 and hash_out:
                    return hash_out
                return None

            # exit code 1 typically means "nothing to commit" — not an error
            if rc == 1:
                return None

            logger.warning(
                "git commit failed (rc=%d): %s %s", rc, stdout, stderr
            )
            return None

        except Exception as exc:
            logger.warning("auto_commit raised: %s", exc)
            return None

    async def get_log(self, max_entries: int = 20) -> list[dict]:
        """Return recent commit log entries.

        Each entry is a dict with keys ``hash``, ``message``, and ``date``.
        """
        try:
            rc, stdout, _ = await self._run_git(
                "log", f"--format=%H|%s|%ci", f"-n{max_entries}"
            )
            if rc != 0 or not stdout:
                return []

            entries: list[dict] = []
            for line in stdout.splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3:
                    entries.append({
                        "hash": parts[0],
                        "message": parts[1],
                        "date": parts[2],
                    })
            return entries

        except Exception as exc:
            logger.warning("get_log raised: %s", exc)
            return []

    async def diff(self, from_hash: str, to_hash: str = "HEAD") -> str:
        """Return the diff between two commits."""
        try:
            rc, stdout, _ = await self._run_git(
                "diff", f"{from_hash}..{to_hash}"
            )
            if rc != 0:
                return ""
            return stdout
        except Exception as exc:
            logger.warning("diff raised: %s", exc)
            return ""

    async def rollback(self, to_hash: str) -> str:
        """Restore workspace files to the state at *to_hash*.

        Uses ``git checkout <hash> -- .`` so that history is preserved
        (unlike ``git reset`` which would rewrite it).
        """
        try:
            rc, _, stderr = await self._run_git(
                "checkout", to_hash, "--", "."
            )
            if rc != 0:
                return f"Rollback failed: {stderr}"
            return f"Rolled back to {to_hash}"
        except Exception as exc:
            return f"Rollback error: {exc}"
