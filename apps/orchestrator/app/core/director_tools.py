"""Tool schema for Planner review.

The Planner uses the `review` tool during worker output auditing.
Director no longer uses any LLM tools — it is a pure rule-based scheduler.
"""

REVIEW_TOOL_NAME = "review"


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
                "description": "pass: output meets requirements. reject: output needs revision.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Explanation of the review result. Required for both pass and reject."
                ),
            },
            "next_prompt": {
                "type": "string",
                "description": (
                    "When rejecting, specific guidance for the worker to fix the issues."
                ),
            },
        },
        "required": ["result", "reason"],
    },
}

REVIEW_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": REVIEW_TOOL_NAME,
        "description": REVIEW_TOOL["description"],
        "parameters": REVIEW_TOOL["input_schema"],
    },
}

REVIEW_TOOL_CHOICE_ANTHROPIC = {"type": "tool", "name": REVIEW_TOOL_NAME}
REVIEW_TOOL_CHOICE_OPENAI = {"type": "function", "function": {"name": REVIEW_TOOL_NAME}}

MAX_REVIEW_RETRIES = 3
