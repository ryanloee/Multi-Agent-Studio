class SandboxProvisioner:
    """Provisions sandbox containers: install OpenCode, setup MCP, initialize Git."""

    def __init__(self, sandbox_manager: "app.sandbox.manager.SandboxManager"):
        self.sandbox = sandbox_manager

    async def provision(self, container_id: str, config: dict) -> None:
        """Provision a fresh sandbox container:
        1. Install OpenCode CLI (if not pre-baked in image)
        2. Inject OpenCode config (model/permissions/MCP with run_id)
        3. Initialize separated Git repo
        4. Create .workflow/ directory for inter-node context sharing
        """
        # Create workspace directories
        await self.sandbox.exec(container_id, "mkdir -p /workspace/.workflow")
        await self.sandbox.exec(container_id, "mkdir -p /workspace/.opencode")
        await self.sandbox.exec(container_id, "mkdir -p /sandbox-meta/.git")

        # Initialize separated Git repo
        from app.sandbox.checkpoint import GitCheckpointManager

        checkpoint = GitCheckpointManager(self.sandbox)
        await checkpoint.init_repo(container_id)

        # Inject OpenCode config
        import json
        await self.sandbox.write_file(
            container_id,
            "/root/.opencode/config.json",
            json.dumps(config),
        )
