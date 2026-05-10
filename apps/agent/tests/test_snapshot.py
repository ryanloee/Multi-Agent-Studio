"""Tests for SnapshotManager — automatic git snapshots."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from mas_agent.snapshot import SnapshotManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(workspace: str) -> SnapshotManager:
    """Create a SnapshotManager without a StreamWriter (None is fine)."""
    return SnapshotManager(workspace)


def _git(*args: str, cwd: str) -> str:
    """Run a git command synchronously (for test setup/assertions)."""
    import subprocess

    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_workspace(tmp_path: Path) -> str:
    """Return a temporary workspace with git initialized and one committed file."""
    ws = str(tmp_path)
    _git("init", cwd=ws)
    _git("config", "user.email", "test@test.com", cwd=ws)
    _git("config", "user.name", "Test", cwd=ws)
    (tmp_path / "hello.txt").write_text("initial content\n")
    _git("add", "-A", cwd=ws)
    _git("commit", "-m", "initial commit", cwd=ws)
    return ws


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAutoCommit:
    """auto_commit creates a commit when files have changed."""

    @pytest.mark.asyncio()
    async def test_auto_commit_records_change(self, git_workspace: str) -> None:
        """Modify a file, call auto_commit, verify git log has new commit."""
        # Modify a file
        Path(git_workspace, "hello.txt").write_text("modified content\n")

        snap = _make_snapshot(git_workspace)
        commit_hash = await snap.auto_commit("edit", "hello.txt")

        assert commit_hash is not None, "auto_commit should return a hash"

        # Verify the commit is in the log
        log_msg = _git("log", "--format=%s", "-1", cwd=git_workspace)
        assert "agent: edit" in log_msg
        assert "hello.txt" in log_msg

    @pytest.mark.asyncio()
    async def test_commit_message_format(self, git_workspace: str) -> None:
        """Commit message includes tool name and description."""
        Path(git_workspace, "hello.txt").write_text("another change\n")

        snap = _make_snapshot(git_workspace)
        await snap.auto_commit("write", "hello.txt")

        log_msg = _git("log", "--format=%s", "-1", cwd=git_workspace)
        assert log_msg == "agent: write - hello.txt"

    @pytest.mark.asyncio()
    async def test_git_init_in_non_git_dir(self, tmp_path: Path) -> None:
        """In a non-git directory, auto_commit initialises git automatically."""
        ws = str(tmp_path)
        # No git init — completely fresh directory
        (tmp_path / "newfile.txt").write_text("hello\n")

        snap = _make_snapshot(ws)
        commit_hash = await snap.auto_commit("write", "newfile.txt")

        assert commit_hash is not None, "auto_commit should init git and commit"

        # Verify git was initialized
        assert os.path.isdir(os.path.join(ws, ".git"))

        # Verify agent config was set (local to repo)
        email = _git("config", "--local", "user.email", cwd=ws)
        assert email == "agent@mas-agent.local"
        name = _git("config", "--local", "user.name", cwd=ws)
        assert name == "MAS Agent"

    @pytest.mark.asyncio()
    async def test_no_changes_no_error(self, git_workspace: str) -> None:
        """Calling auto_commit with no file changes returns None without error."""
        snap = _make_snapshot(git_workspace)
        result = await snap.auto_commit("edit", "nothing changed")

        assert result is None, "Nothing to commit should return None"


class TestGetLog:
    """get_log returns commit history in order."""

    @pytest.mark.asyncio()
    async def test_get_log_returns_commits(self, git_workspace: str) -> None:
        """Make several commits, verify get_log returns them in order."""
        snap = _make_snapshot(git_workspace)

        # Make 3 additional commits
        for i in range(3):
            Path(git_workspace, "hello.txt").write_text(f"version {i}\n")
            await snap.auto_commit("edit", f"hello.txt v{i}")

        log = await snap.get_log(max_entries=10)

        # Should have the 3 new commits plus the initial commit = 4
        assert len(log) == 4

        # Most recent commit first
        assert "v2" in log[0]["message"]

        # Each entry has the expected keys
        for entry in log:
            assert "hash" in entry
            assert "message" in entry
            assert "date" in entry

    @pytest.mark.asyncio()
    async def test_get_log_respects_max_entries(self, git_workspace: str) -> None:
        """max_entries limits the number of returned entries."""
        snap = _make_snapshot(git_workspace)

        for i in range(5):
            Path(git_workspace, "hello.txt").write_text(f"version {i}\n")
            await snap.auto_commit("edit", f"hello.txt v{i}")

        log = await snap.get_log(max_entries=3)
        assert len(log) == 3


class TestDiff:
    """diff returns the difference between two commits."""

    @pytest.mark.asyncio()
    async def test_diff_between_commits(self, git_workspace: str) -> None:
        """Make changes, get diff between first and current commit."""
        snap = _make_snapshot(git_workspace)

        initial_hash = _git("rev-parse", "HEAD", cwd=git_workspace)

        Path(git_workspace, "hello.txt").write_text("changed content\n")
        await snap.auto_commit("edit", "hello.txt")

        diff_output = await snap.diff(initial_hash)
        assert "-initial content" in diff_output
        assert "+changed content" in diff_output


class TestRollback:
    """rollback restores workspace files to a prior commit."""

    @pytest.mark.asyncio()
    async def test_rollback_restores_files(self, git_workspace: str) -> None:
        """Make changes, rollback, verify file is restored."""
        snap = _make_snapshot(git_workspace)

        # Record the initial state hash
        initial_hash = _git("rev-parse", "HEAD", cwd=git_workspace)
        initial_content = Path(git_workspace, "hello.txt").read_text()

        # Make a change and commit it
        Path(git_workspace, "hello.txt").write_text("modified content\n")
        await snap.auto_commit("edit", "hello.txt")

        # Verify the file changed
        assert Path(git_workspace, "hello.txt").read_text() == "modified content\n"

        # Rollback to the initial commit
        result = await snap.rollback(initial_hash)
        assert "Rolled back" in result

        # Verify file was restored
        assert Path(git_workspace, "hello.txt").read_text() == initial_content

    @pytest.mark.asyncio()
    async def test_rollback_preserves_history(self, git_workspace: str) -> None:
        """After rollback, git history should still contain all commits."""
        snap = _make_snapshot(git_workspace)
        initial_hash = _git("rev-parse", "HEAD", cwd=git_workspace)

        Path(git_workspace, "hello.txt").write_text("modified content\n")
        await snap.auto_commit("edit", "hello.txt")

        await snap.rollback(initial_hash)

        # History should still have both commits
        log = await snap.get_log()
        assert len(log) == 2, "History should be preserved after rollback"
