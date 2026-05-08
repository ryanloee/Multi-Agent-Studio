import json
from typing import Any

from app.agents.base import NodeConfig


def generate_opencode_config(
    config: NodeConfig,
    run_id: str,
    node_id: str,
) -> str:
    """Generate OpenCode config with run_id namespace isolation in MCP URL."""

    mcp_servers = {}
    for name, server_config in config.mcp_servers.items():
        url = server_config.get("url", "")
        if "?" in url:
            url += f"&run_id={run_id}&node_id={node_id}"
        else:
            url += f"?run_id={run_id}&node_id={node_id}"
        mcp_servers[name] = {**server_config, "url": url}

    return json.dumps({
        "model": {
            "provider": config.model_provider,
            "id": config.model_id,
        },
        "agents": {
            config.agent_type: {
                "tools": _get_tools(config.agent_type),
                "permissions": config.permissions,
            }
        },
        "mcp": {"servers": mcp_servers},
    })


def _get_tools(agent_type: str) -> list[str]:
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
    return tool_map.get(agent_type, ["read"])
