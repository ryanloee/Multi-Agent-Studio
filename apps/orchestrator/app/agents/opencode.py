import json
import shlex

from app.agents.base import BaseAgentRuntime, NodeConfig, StreamEvent
from app.sandbox.manager import SandboxManager
from app.sandbox.checkpoint import GitCheckpointManager
from app.streaming.publisher import StreamPublisher

# Mapping from internal agent types to OpenCode v1.14.41 agent names.
# Available OpenCode agents: build, compaction, explore, general, plan, summary, title
AGENT_TYPE_MAP = {
    "build": "build",
    "coder": "build",
    "plan": "plan",
    "explore": "explore",
    "@explore": "explore",
    "general": "general",
    "@general": "general",
    "shell": "build",
    "review": "build",
}


def build_opencode_command(
    agent: str,
    prompt: str,
    log_path: str,
    model_provider: str = "",
    model_id: str = "",
) -> str:
    """Build OpenCode CLI command for v1.14.41+.

    Actual interface:
        opencode run --agent <agent> -m provider/model --format json \
            --dangerously-skip-permissions "prompt text" > log_path 2>&1

    Args:
        agent: Internal agent type (coder, plan, explore, shell, review, etc.).
               Mapped to OpenCode agent name via AGENT_TYPE_MAP.
        prompt: Task prompt text (passed as positional argument).
        log_path: Path to redirect stdout/stderr to inside the container.
        model_provider: LLM provider name (e.g. "anthropic").
        model_id: LLM model ID (e.g. "claude-sonnet-4-20250514").

    Returns:
        Shell command string ready for sandbox exec.
    """
    oc_agent = AGENT_TYPE_MAP.get(agent, agent)
    safe_prompt = shlex.quote(prompt)

    cmd = f"mkdir -p $(dirname {shlex.quote(log_path)}) && opencode run"
    cmd += f" --agent {shlex.quote(oc_agent)}"

    if model_provider and model_id:
        cmd += f" -m {shlex.quote(model_provider)}/{shlex.quote(model_id)}"

    cmd += " --format json"
    cmd += " --dangerously-skip-permissions"
    cmd += f" {safe_prompt}"
    cmd += f" > {shlex.quote(log_path)} 2>&1"

    return cmd


class OpenCodeAgent(BaseAgentRuntime):
    """Wraps OpenCode CLI as a workflow node execution engine.
    Uses file channel (stream.jsonl) for structured output capture."""

    STREAM_FILE = "/workspace/.opencode/stream.jsonl"
    GIT_DIR = "/sandbox-meta/.git"
    WORK_TREE = "/workspace"

    def __init__(
        self,
        node_id: str,
        sandbox_id: str,
        config: NodeConfig,
        sandbox_manager: SandboxManager,
        checkpoint_manager: GitCheckpointManager,
        stream_publisher: StreamPublisher,
    ):
        super().__init__(node_id, sandbox_id, config)
        self.sandbox = sandbox_manager
        self.checkpoint = checkpoint_manager
        self.publisher = stream_publisher

    async def run(self, task_input: dict) -> dict:
        # 1. Git Checkpoint before execution
        commit_hash = await self.checkpoint.auto_commit(
            self.sandbox_id,
            message=f"before node [{self.node_id}]",
        )

        # 2. Generate and inject OpenCode config
        config_json = self._generate_config()
        await self.sandbox.write_file(
            self.sandbox_id,
            "/root/.opencode/config.json",
            config_json,
        )

        # 3. Build command (opencode run --format json, stdout redirect)
        cmd = self._build_command(task_input)
        exec_id = await self.sandbox.exec_async(self.sandbox_id, cmd)

        # 4. Watch stream.jsonl (with 50MB Log Bomb defense)
        from app.streaming.file_watcher import FileWatcher

        watcher = FileWatcher(
            sandbox_manager=self.sandbox,
            sandbox_id=self.sandbox_id,
            file_path=self.STREAM_FILE,
            run_id=task_input.get("run_id", ""),
            node_id=self.node_id,
            publisher=self.publisher,
        )
        await watcher.start()

        # 5. Wait for OpenCode process to exit
        exit_code = await self.sandbox.wait_process(exec_id)
        await watcher.stop()

        return {
            "exit_code": exit_code,
            "status": "completed" if exit_code == 0 else "failed",
            "git_commit_before": commit_hash,
        }

    def _generate_config(self) -> str:
        from app.agents.config import generate_opencode_config
        return generate_opencode_config(self.config, "", self.node_id)

    def _build_command(self, task_input: dict) -> str:
        prompt = self.config.prompt.format(**task_input)
        return build_opencode_command(
            agent=self.config.agent_type,
            prompt=prompt,
            log_path=self.STREAM_FILE,
            model_provider=self.config.model_provider,
            model_id=self.config.model_id,
        )

    def _get_tools_for_agent(self) -> list[str]:
        tool_map = {
            "build": ["read", "edit", "write", "bash", "glob", "grep"],
            "coder": ["read", "edit", "write", "bash", "glob", "grep"],
            "plan": ["read", "glob", "grep", "codesearch"],
            "explore": ["read", "glob", "grep", "codesearch"],
            "@explore": ["read", "glob", "grep", "codesearch"],
            "general": ["read", "edit", "write", "bash", "glob", "grep"],
            "@general": ["read", "edit", "write", "bash", "glob", "grep"],
            "shell": ["bash"],
            "review": ["read", "glob", "grep", "codesearch"],
        }
        return tool_map.get(self.config.agent_type, ["read"])
