"""Provisions sandbox containers: setup workspace directories, initialize Git.

Creates the base directory layout for the sandbox. Node-specific containers
are created separately by NodeRunner under .mas/containers/{node_id}/.

All operations use Python pathlib — no shell commands.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)


class SandboxProvisioner:
    """Provisions sandbox containers: setup workspace directories, initialize Git."""

    def __init__(self, sandbox_manager: LocalSandbox) -> None:
        self.sandbox = sandbox_manager

    async def provision(self, container_id: str, config: dict) -> None:
        """Provision a fresh sandbox container:
        1. Create base workspace directories (.workflow)
        2. Create .mas/containers directory for node containers
        3. Initialize separated Git repo (metadata in /sandbox-meta, work tree in /workspace)
        """
        # Create base workspace directories using Python
        workspace = self.sandbox.resolve_virtual_path(container_id, "/workspace")
        Path(workspace, ".workflow").mkdir(parents=True, exist_ok=True)
        Path(workspace, ".mas", "containers").mkdir(parents=True, exist_ok=True)

        # Create sandbox-meta directory
        meta = self.sandbox.resolve_virtual_path(container_id, "/sandbox-meta")
        Path(meta, ".git").mkdir(parents=True, exist_ok=True)

        # Initialize separated Git repo
        from app.sandbox.checkpoint import GitCheckpointManager

        checkpoint = GitCheckpointManager(self.sandbox)
        await checkpoint.init_repo(container_id)
