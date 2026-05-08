class GitCheckpointManager:
    """Git-based checkpoint with directory separation for anti-suicide protection.

    .git metadata is stored in /sandbox-meta/.git (Agent cannot access).
    Work tree is /workspace (Agent can read/write, but cannot destroy checkpoints).
    """

    GIT_DIR = "/sandbox-meta/.git"
    WORK_TREE = "/workspace"

    def __init__(self, sandbox_manager: "app.sandbox.manager.SandboxManager"):
        self.sandbox = sandbox_manager

    def _git_cmd(self, *args: str) -> str:
        return f"git --git-dir={self.GIT_DIR} --work-tree={self.WORK_TREE} {' '.join(args)}"

    async def init_repo(self, sandbox_id: str) -> None:
        """Initialize separated Git repo in sandbox."""
        await self.sandbox.exec(sandbox_id, f"mkdir -p {self.GIT_DIR}")
        await self.sandbox.exec(sandbox_id, self._git_cmd("init"))
        await self.sandbox.exec(sandbox_id, self._git_cmd("config user.email 'orchestrator@mas.local'"))
        await self.sandbox.exec(sandbox_id, self._git_cmd("config user.name 'MAS Orchestrator'"))

    async def auto_commit(self, sandbox_id: str, message: str) -> str:
        """Auto-commit before node execution. Agent is unaware of this."""
        await self.sandbox.exec(sandbox_id, self._git_cmd("add -A"))
        await self.sandbox.exec(
            sandbox_id, self._git_cmd(f'commit -m "{message}" --allow-empty')
        )
        stdout, _ = await self.sandbox.exec(
            sandbox_id, self._git_cmd("rev-parse HEAD")
        )
        return stdout.strip()

    async def rollback(self, sandbox_id: str, commit_hash: str) -> None:
        """Rollback to checkpoint on failure or Review Reject."""
        await self.sandbox.exec(
            sandbox_id, self._git_cmd(f"reset --hard {commit_hash}")
        )

    async def get_diff(self, sandbox_id: str, from_hash: str) -> str:
        """Get diff for Human-in-the-Loop approval panel."""
        stdout, _ = await self.sandbox.exec(
            sandbox_id, self._git_cmd(f"diff {from_hash} HEAD")
        )
        return stdout
