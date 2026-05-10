"""System prompt loading for different agent types."""
from __future__ import annotations

from typing import Any

DEFAULT_SYSTEM = "You are a helpful AI assistant working in a software project."

PLAN_SUFFIX = """

You are a Planner agent. Your job is to:
1. Analyze the user's task
2. Break it into concrete, executable subtasks
3. For each subtask, specify the agent type (coder, explore, shell, review) and a clear prompt
4. Output your plan as a structured list

Format your plan as:
## Plan
1. [agent_type: coder] <task description>
   Prompt: <detailed prompt for this subtask>
2. [agent_type: explore] <task description>
   Prompt: <detailed prompt for this subtask>
"""

AGENT_PROMPTS = {
    "coder": "You are a Coder agent. Write and modify code files. Read existing code before making changes.",
    "plan": "You are a Planner agent. Analyze tasks and create execution plans.",
    "explore": "You are an Explorer agent. Search the codebase and gather information.",
    "review": "You are a Reviewer agent. Review code changes and provide feedback.",
    "shell": "You are a Shell agent. Execute shell commands to accomplish tasks.",
    "human": "You are a Human-in-the-Loop agent.",
}


def load_prompt(agent_type: str, **kwargs: Any) -> str:
    base = AGENT_PROMPTS.get(agent_type, DEFAULT_SYSTEM)
    workspace = kwargs.get("workspace", "/workspace")
    return f"{base}\n\nWorking directory: {workspace}"
