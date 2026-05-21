"""Tools for the Planner dynamic decision loop.

The Planner uses two tools:
  - `decide`: choose the next action (explore/coder/shell/done/failed)
  - `review`: review worker output (pass/reject)
"""

REVIEW_TOOL_NAME = "review"
DECIDE_TOOL_NAME = "decide"

DECIDE_TOOL = {
    "name": DECIDE_TOOL_NAME,
    "description": (
        "Choose the next action for the workflow. You MUST call this tool "
        "every turn to decide what to do next."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["explore", "coder", "shell", "done", "failed"],
                "description": (
                    "explore: investigate codebase. "
                    "coder: write/modify code. "
                    "shell: run tests/commands. "
                    "done: goal achieved. "
                    "failed: blocked."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Why this action was chosen.",
            },
            "prompt": {
                "type": "string",
                "description": "Instruction for the sub-agent.",
            },
            "task_id": {
                "type": "string",
                "description": "Short identifier for this step.",
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files the sub-agent should focus on.",
            },
        },
        "required": ["action", "reasoning", "prompt", "task_id"],
    },
}

REVIEW_TOOL = {
    "name": REVIEW_TOOL_NAME,
    "description": (
        "Review a Worker agent's output and give a pass or reject assessment. "
        "You MUST call this tool when reviewing worker output."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "enum": ["pass", "reject"],
                "description": "pass: output meets requirements. reject: needs revision.",
            },
            "reason": {
                "type": "string",
                "description": "Explanation of the review result.",
            },
            "next_prompt": {
                "type": "string",
                "description": "When rejecting, guidance for the worker to fix issues.",
            },
        },
        "required": ["result", "reason"],
    },
}

PLANNER_TOOLS = [DECIDE_TOOL, REVIEW_TOOL]

DECIDE_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": DECIDE_TOOL["name"],
        "description": DECIDE_TOOL["description"],
        "parameters": DECIDE_TOOL["input_schema"],
    },
}

REVIEW_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": REVIEW_TOOL["name"],
        "description": REVIEW_TOOL["description"],
        "parameters": REVIEW_TOOL["input_schema"],
    },
}

PLANNER_TOOLS_OPENAI = [DECIDE_TOOL_OPENAI, REVIEW_TOOL_OPENAI]

MAX_REVIEW_RETRIES = 3
MAX_NO_DECISION = 3
