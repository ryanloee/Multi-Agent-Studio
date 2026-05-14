"""Permission checker — gate tool execution on security-sensitive operations."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mas_agent.events import StreamWriter

logger = logging.getLogger(__name__)


class PermissionAction(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # needs external confirmation


@dataclass
class PermissionRule:
    permission: str  # "shell", "edit", "write", "read"
    pattern: str     # glob pattern, e.g. "*.env", "rm *"
    action: PermissionAction


# Default rule set (built-in, can be overridden)
DEFAULT_RULES: list[PermissionRule] = [
    # Autonomous workflow nodes do not have a reliable interactive approval
    # loop. High-risk operations must fail fast instead of blocking the agent.
    PermissionRule(permission="shell", pattern="rm *", action=PermissionAction.DENY),
    PermissionRule(permission="write", pattern="*.lock", action=PermissionAction.DENY),
]

# How long to poll for an ASK response before defaulting to DENY
_ASK_TIMEOUT_SECONDS = 300  # 5 minutes
_POLL_INTERVAL_SECONDS = 0.5


def _map_tool_to_permission(tool_name: str) -> str:
    """Map a tool name to a permission category."""
    mapping = {
        "shell": "shell",
        "write": "write",
        "edit": "edit",
        "read": "read",
        "glob": "read",
        "grep": "read",
    }
    return mapping.get(tool_name, tool_name)


def _extract_target(tool_name: str, arguments: dict[str, Any]) -> str:
    """Extract the primary target string from tool arguments for pattern matching."""
    if tool_name == "shell":
        return arguments.get("command", "")
    # For file-oriented tools, use the path argument
    return arguments.get("path", "")


class PermissionChecker:
    """Checks tool invocations against permission rules.

    Parameters
    ----------
    stream_writer:
        Used to emit ``permission_request`` events so an external UI can
        display a confirmation prompt.
    workspace:
        The agent workspace path.  ASK responses are read from
        ``{workspace}/.agent/permission_responses/{request_id}.json``.
    rules:
        Optional override of the default rule set.
    """

    def __init__(
        self,
        stream_writer: StreamWriter,
        workspace: str,
        rules: list[PermissionRule] | None = None,
    ) -> None:
        self._stream = stream_writer
        self._workspace = workspace
        self._rules = rules if rules is not None else list(DEFAULT_RULES)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, tool_name: str, arguments: dict[str, Any]) -> PermissionAction:
        """Decide whether a tool invocation is allowed.

        Returns **ALLOW**, **DENY**, or **ASK**.  When the result is ASK
        the caller should call :meth:`wait_for_approval` to get the final
        verdict.
        """
        permission = _map_tool_to_permission(tool_name)
        target = _extract_target(tool_name, arguments)

        for rule in self._rules:
            if rule.permission != permission:
                continue
            if fnmatch.fnmatch(target, rule.pattern):
                return rule.action

        return PermissionAction.ALLOW

    async def wait_for_approval(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Emit a ``permission_request`` event and wait for an external response.

        Returns ``True`` if approved, ``False`` if denied (including timeout).
        """
        request_id = uuid.uuid4().hex
        target = _extract_target(tool_name, arguments)
        permission = _map_tool_to_permission(tool_name)

        # Emit the request event
        self._stream._write({
            "type": "permission_request",
            "request_id": request_id,
            "permission": permission,
            "target": target,
            "tool_name": tool_name,
            "arguments": arguments,
        })

        # Prepare the response directory
        resp_dir = os.path.join(self._workspace, ".agent", "permission_responses")
        os.makedirs(resp_dir, exist_ok=True)
        resp_path = os.path.join(resp_dir, f"{request_id}.json")

        deadline = time.monotonic() + _ASK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if os.path.exists(resp_path):
                try:
                    with open(resp_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    approved = data.get("approved", False)
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read permission response %s: %s", resp_path, exc)
                    approved = False
                finally:
                    # Clean up the response file
                    try:
                        os.remove(resp_path)
                    except OSError:
                        pass
                return approved

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        # Timeout — default to DENY
        logger.warning("Permission request %s timed out, defaulting to DENY", request_id)
        return False
