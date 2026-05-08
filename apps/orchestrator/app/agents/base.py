from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class AgentState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class NodeConfig:
    def __init__(
        self,
        agent_type: str,
        model_provider: str,
        model_id: str,
        prompt: str,
        permissions: dict[str, str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
    ):
        self.agent_type = agent_type
        self.model_provider = model_provider
        self.model_id = model_id
        self.prompt = prompt
        self.permissions = permissions or {}
        self.mcp_servers = mcp_servers or {}


class StreamEvent:
    def __init__(
        self,
        event_type: str,
        content: str,
        node_id: str,
        run_id: str,
        tool_name: Optional[str] = None,
        metadata: Optional[dict] = None,
        timestamp: Optional[str] = None,
    ):
        self.event_type = event_type
        self.content = content
        self.node_id = node_id
        self.run_id = run_id
        self.tool_name = tool_name
        self.metadata = metadata or {}
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        d = {
            "type": self.event_type,
            "content": self.content,
            "node_id": self.node_id,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
        }
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.metadata:
            d["metadata"] = self.metadata
        return d


class BaseAgentRuntime:
    def __init__(self, node_id: str, sandbox_id: str, config: NodeConfig):
        self.node_id = node_id
        self.sandbox_id = sandbox_id
        self.config = config
        self.state = AgentState.PENDING

    async def run(self, task_input: dict) -> dict:
        raise NotImplementedError

    async def stream_yield(self, token: str) -> None:
        raise NotImplementedError

    async def invoke_tool(self, tool_call: dict) -> dict:
        raise NotImplementedError

    async def pause_and_wait(self) -> None:
        raise NotImplementedError

    async def checkpoint(self) -> str:
        raise NotImplementedError
