"""Workspace sharing for inter-node context passing.

Instead of stuffing upstream output into downstream prompt,
nodes write to .workflow/ directory and downstream reads via OpenCode's read tool.
This converts large model context into local file retrieval.
"""


WORKFLOW_DIR = "/workspace/.workflow"


def get_shared_file_path(key: str) -> str:
    """Get the file path for a shared context key."""
    return f"{WORKFLOW_DIR}/{key}"


def build_downstream_prompt(upstream_node_id: str, task_description: str) -> str:
    """Build a concise prompt that references upstream output via file reading.

    Instead of: "Here is a 500-line plan: [paste entire plan]"
    We generate: "Read .workflow/plan-output.md and implement the code described in it."
    """
    return (
        f"Read the file at {WORKFLOW_DIR}/{upstream_node_id}-output.md first, "
        f"then {task_description}"
    )
