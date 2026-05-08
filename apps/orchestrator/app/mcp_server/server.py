"""Workflow OS MCP Server.

Provides workflow-level tools that OpenCode agents can call via MCP protocol:
- query_upstream(node_id): Query upstream node results
- read_shared_kv(key): Read from run-scoped key-value store
- write_shared_kv(key, value): Write to run-scoped key-value store
- request_human_approval(reason): Trigger human approval flow
- report_progress(percent): Report node progress
"""


# TODO: Implement MCP Server using mcp-python SDK
# The server will:
# 1. Extract run_id from connection URL (namespace isolation)
# 2. Auto-scope all KV operations to WHERE run_id = ?
# 3. Expose tools listed above
