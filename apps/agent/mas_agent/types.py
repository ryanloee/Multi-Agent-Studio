"""Data types for the agent framework."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LoopConfig:
    run_id: str = ""
    node_id: str = ""
    agent_type: str = "coder"
    provider: str = ""
    model: str = ""
    provider_url: str | None = None
    provider_key: str | None = None
    prompt: str = ""
    max_turns: int = 50
    max_tokens: int = 4096
    context_window: int = 128000
    thinking_level: str = "high"
    workspace: str = "/workspace"
    stream_dir: str = "/workspace/.agent"


@dataclass
class Message:
    role: str
    content: str = ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    call_id: str
    name: str
    output: str
    is_error: bool = False
