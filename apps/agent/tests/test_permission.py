"""Tests for the permission checker module."""
from __future__ import annotations

from pathlib import Path

import pytest

from mas_agent.events import StreamWriter
from mas_agent.permission import (
    PermissionAction,
    PermissionChecker,
    PermissionRule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stream(tmp_path: Path) -> StreamWriter:
    stream_dir = str(tmp_path / ".agent" / "streams")
    return StreamWriter(stream_dir, run_id="test-run", node_id="test-node")


def _make_checker(
    tmp_path: Path,
    rules: list[PermissionRule] | None = None,
) -> PermissionChecker:
    stream = _make_stream(tmp_path)
    workspace = str(tmp_path)
    return PermissionChecker(stream, workspace, rules=rules)


# ---------------------------------------------------------------------------
# 1. ALLOW rule — normal file write, verify passes through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allow_normal_write(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("write", {"path": "hello.py", "content": "print('hi')"})
    assert action == PermissionAction.ALLOW


# ---------------------------------------------------------------------------
# 2. DENY rule — write to .lock file, verify denied
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deny_lock_file(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("write", {"path": "package.lock", "content": "{}"})
    assert action == PermissionAction.DENY


# ---------------------------------------------------------------------------
# 3. Autonomous env writes — .env creation must not block workflow runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_env_file_write_is_allowed_by_default(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("write", {"path": ".env", "content": "KEY=val"})
    assert action == PermissionAction.ALLOW


# ---------------------------------------------------------------------------
# 4. Wildcard matching — verify `rm *` matches `rm -rf /tmp/test`
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wildcard_rm_matches(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("shell", {"command": "rm -rf /tmp/test"})
    assert action == PermissionAction.DENY


@pytest.mark.asyncio
async def test_wildcard_rm_matches_exact(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("shell", {"command": "rm something"})
    assert action == PermissionAction.DENY


# ---------------------------------------------------------------------------
# 5. Default ALLOW — no matching rule defaults to allow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_allow(tmp_path: Path) -> None:
    # Use an empty rule set — everything should default to ALLOW
    checker = _make_checker(tmp_path, rules=[])
    action = await checker.check("shell", {"command": "ls -la"})
    assert action == PermissionAction.ALLOW

    action = await checker.check("write", {"path": "anything.txt", "content": "hi"})
    assert action == PermissionAction.ALLOW

    action = await checker.check("read", {"path": "foo.py"})
    assert action == PermissionAction.ALLOW


# ---------------------------------------------------------------------------
# 6. ASK timeout — simulate no response, verify defaults to DENY
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_timeout_defaults_deny(tmp_path: Path) -> None:
    # Use a checker with a very short timeout by patching the module constants
    import mas_agent.permission as perm_mod

    original_timeout = perm_mod._ASK_TIMEOUT_SECONDS
    original_interval = perm_mod._POLL_INTERVAL_SECONDS
    try:
        perm_mod._ASK_TIMEOUT_SECONDS = 0.2  # 200ms timeout
        perm_mod._POLL_INTERVAL_SECONDS = 0.05

        checker = _make_checker(
            tmp_path,
            rules=[PermissionRule("shell", "rm *", PermissionAction.ASK)],
        )
        action = await checker.check("shell", {"command": "rm -rf /some/path"})
        assert action == PermissionAction.ASK

        # Do NOT write any response file — should time out
        approved = await checker.wait_for_approval("shell", {"command": "rm -rf /some/path"})
        assert approved is False
    finally:
        perm_mod._ASK_TIMEOUT_SECONDS = original_timeout
        perm_mod._POLL_INTERVAL_SECONDS = original_interval
