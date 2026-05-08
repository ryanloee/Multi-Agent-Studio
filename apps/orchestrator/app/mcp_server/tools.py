"""MCP Tool definitions for Workflow OS MCP Server."""

MCP_TOOLS = [
    {
        "name": "query_upstream",
        "description": "Query the output of an upstream node in the current workflow",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "ID of the upstream node"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "read_shared_kv",
        "description": "Read a value from the run-scoped key-value store",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to read"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "write_shared_kv",
        "description": "Write a value to the run-scoped key-value store",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to write"},
                "value": {"type": "string", "description": "Value to store (JSON string)"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "request_human_approval",
        "description": "Pause workflow and request human approval with a reason",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why human approval is needed"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "report_progress",
        "description": "Report current progress percentage",
        "inputSchema": {
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "description": "Progress percentage (0-100)"},
            },
            "required": ["percent"],
        },
    },
]
