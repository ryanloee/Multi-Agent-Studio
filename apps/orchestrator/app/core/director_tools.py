"""Director tool-use schema for the dispatch loop.

The Director agent uses tool-use to output structured decisions.
Each turn it calls the `decide` tool with an action, reasoning, and
a prompt for the sub-agent it wants to dispatch.
"""

DIRECTOR_TOOLS = [{
    "name": "decide",
    "description": (
        "Choose the next action for the workflow. You MUST call this tool "
        "every turn — never output free-text decisions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["scout", "worker", "test", "done", "failed"],
                "description": (
                    "scout: investigate codebase and report findings. "
                    "worker: write/modify code. "
                    "test: run tests or validation commands. "
                    "done: goal achieved, stop the loop. "
                    "failed: blocked, cannot proceed."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of why this action was chosen.",
            },
            "prompt": {
                "type": "string",
                "description": "Precise instruction for the sub-agent (scout/worker/test).",
            },
            "task_id": {
                "type": "string",
                "description": "A short identifier for this dispatch step.",
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files the sub-agent should focus on.",
            },
        },
        "required": ["action", "reasoning", "prompt", "task_id"],
    },
}]


# Tool choice constraint — forces the model to call `decide` every turn.
DIRECTOR_TOOL_CHOICE = {"type": "function", "function": {"name": "decide"}}
