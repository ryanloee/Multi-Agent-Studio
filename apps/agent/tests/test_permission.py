"""Tests for the permission checker module."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from mas_agent.events import StreamWriter
from mas_agent.permission import (
    DEFAULT_RULES,
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
# 3. ASK rule — write to .env file, verify permission_request event emitted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_env_file_emits_event(tmp_path: Path) -> None:
    stream = _make_stream(tmp_path)
    workspace = str(tmp_path)
    checker = PermissionChecker(stream, workspace)

    action = await checker.check("write", {"path": ".env", "content": "KEY=val"})
    assert action == PermissionAction.ASK

    # The event is emitted during wait_for_approval, not during check.
    # Launch wait_for_approval with a background writer to approve quickly.
    resp_dir = os.path.join(workspace, ".agent", "permission_responses")

    async def _approve_later() -> None:
        # Wait until the response directory is created and the request event exists
        for _ in range(50):
            await asyncio.sleep(0.05)
            if os.path.isdir(resp_dir):
                break
        # Find the request_id from stream.jsonl
        stream_path = os.path.join(str(tmp_path / ".agent" / "streams"), "stream.jsonl")
        for _ in range(50):
            await asyncio.sleep(0.05)
            if os.path.exists(stream_path):
                break
        request_id = None
        with open(stream_path, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                if data.get("type") == "permission_request":
                    request_id = data["request_id"]
                    break
        assert request_id is not None, "permission_request event not found in stream"
        # Write approval
        resp_path = os.path.join(resp_dir, f"{request_id}.json")
        with open(resp_path, "w", encoding="utf-8") as f:
            json.dump({"approved": True}, f)

    approval_task = asyncio.create_task(_approve_later())
    approved = await checker.wait_for_approval("write", {"path": ".env", "content": "KEY=val"})
    await approval_task

    assert approved is True

    # Verify the event was actually written
    stream_path = os.path.join(str(tmp_path / ".agent" / "streams"), "stream.jsonl")
    found = False
    with open(stream_path, "r", encoding="utf-8") as f:
        for line in f:
            data = json.loads(line)
            if data.get("type") == "permission_request":
                assert data["permission"] == "write"
                assert data["target"] == ".env"
                assert "request_id" in data
                found = True
    assert found, "permission_request event not emitted"


# ---------------------------------------------------------------------------
# 4. Wildcard matching — verify `rm *` matches `rm -rf /tmp/test`
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wildcard_rm_matches(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("shell", {"command": "rm -rf /tmp/test"})
    assert action == PermissionAction.ASK


@pytest.mark.asyncio
async def test_wildcard_rm_matches_exact(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    action = await checker.check("shell", {"command": "rm something"})
    assert action == PermissionAction.ASK


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

        checker = _make_checker(tmp_path)
        action = await checker.check("shell", {"command": "rm -rf /some/path"})
        assert action == PermissionAction.ASK

        # Do NOT write any response file — should time out
        approved = await checker.wait_for_approval("shell", {"command": "rm -rf /some/path"})
        assert approved is False
    finally:
        perm_mod._ASK_TIMEOUT_SECONDS = original_timeout
        perm_mod._POLL_INTERVAL_SECONDS = original_interval
