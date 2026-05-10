class SandboxProvisioner:
    """Provisions sandbox containers: setup workspace directories, initialize Git.

    Creates the directory layout expected by the Python agent framework (mas_agent)
    and the orchestrator's workflow inter-node context sharing.
    """

    def __init__(self, sandbox_manager: "app.core.local_sandbox.LocalSandbox"):
        self.sandbox = sandbox_manager

    async def provision(self, container_id: str, config: dict) -> None:
        """Provision a fresh sandbox container:
        1. Create workspace directories (.agent, .workflow)
        2. Initialize separated Git repo (metadata in /sandbox-meta, work tree in /workspace)
        """
        # Create workspace directories for the Python agent
        await self.sandbox.exec(container_id, "mkdir -p /workspace/.agent")
        await self.sandbox.exec(container_id, "mkdir -p /workspace/.workflow")
        await self.sandbox.exec(container_id, "mkdir -p /sandbox-meta/.git")

        # Initialize separated Git repo
        from app.sandbox.checkpoint import GitCheckpointManager

        checkpoint = GitCheckpointManager(self.sandbox)
        await checkpoint.init_repo(container_id)
