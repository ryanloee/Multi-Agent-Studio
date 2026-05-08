from app.agents.base import NodeConfig
from app.agents.opencode import OpenCodeAgent
from app.sandbox.manager import SandboxManager
from app.sandbox.checkpoint import GitCheckpointManager
from app.streaming.publisher import StreamPublisher


def create_agent(
    agent_type: str,
    node_id: str,
    sandbox_id: str,
    config: NodeConfig,
    sandbox_manager: SandboxManager,
    checkpoint_manager: GitCheckpointManager,
    stream_publisher: StreamPublisher,
) -> OpenCodeAgent:
    if agent_type in ("build", "coder", "plan", "explore", "@explore", "general", "@general", "shell", "review"):
        return OpenCodeAgent(
            node_id=node_id,
            sandbox_id=sandbox_id,
            config=config,
            sandbox_manager=sandbox_manager,
            checkpoint_manager=checkpoint_manager,
            stream_publisher=stream_publisher,
        )
    raise ValueError(f"Unknown agent type: {agent_type}")
