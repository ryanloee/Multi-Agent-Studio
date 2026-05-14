"""Parse structured plan output from planner agent.

Supports three strategies (tried in order):
1. plan.json lines -- written by the create_child_task tool into
   /workspace/.workflow/plan.json, one JSON object per line.
2. JSON blocks embedded in markdown or raw text (legacy from OpenCode era).
3. todowrite tool calls extracted from stream.jsonl lines (legacy fallback).
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

PLAN_SYSTEM_SUFFIX = """

You are the high-level orchestrator for a white-box multi-agent system. Your job is to understand the goal, decompose it into a precise DAG, assign independently executable work to workers, and make the resulting execution observable.

When your analysis is complete, output a Markdown explanation followed by a JSON DAG in this exact format at the end:
```json
{
  "tasks": [
    {
      "id": "step_1_explore_structure",
      "type": "explore",
      "prompt": "Search the codebase for X. Focus on: 1) file paths 2) key function signatures 3) existing patterns. Report all findings with file paths.",
      "depends_on": []
    },
    {
      "id": "step_2_implement_feature",
      "type": "coder",
      "prompt": "Based on exploration results, implement Y. Requirements:\\n1. Specific requirement\\n2. Another requirement\\n3. Error handling\\n\\nAcceptance criteria: X works correctly when Y.",
      "depends_on": ["step_1_explore_structure"]
    },
    {
      "id": "step_3_review",
      "type": "review",
      "prompt": "Review the implementation for: 1) correctness 2) edge cases 3) code style. List issues found with severity.",
      "depends_on": ["step_2_implement_feature"]
    }
  ]
}
```

## Task decomposition principles:
- **Single responsibility**: Each task should focus on ONE specific operation
- **Specify target files**: Include target file paths in the prompt when possible
- **Numbered requirements**: List implementation steps as numbered items
- **Acceptance criteria**: Each prompt should state how to verify completion
- **Avoid large tasks**: Split "implement feature X" into explore → implement → review → test
- **15-30 tasks**: Create enough fine-grained tasks for the project complexity

Each task must have: "id" (unique descriptive string like "step_1_explore_auth"), "type" (coder/explore/review/shell), "prompt" (detailed description with context, requirements, and acceptance criteria).
Optional: "depends_on" (list of task ID strings that must complete first).

## Orchestrator requirements:
- Parallelize independent tasks by leaving their "depends_on" empty or pointing only to real prerequisites.
- Do not create coarse tasks such as "develop frontend", "implement backend", "fix all bugs", or "write tests". Split them into file/module-level tasks a worker can complete independently.
- Every worker prompt must include: goal, context, inputs, ordered steps, output format, acceptance criteria, boundaries, escalation rules, peer collaboration rules, and artifact requirements.
- Workers may consult the planner by outputting `ESCALATE_TO_PLANNER: <question>`. Tell them to use this only for blockers or scope decisions.
- Workers may collaborate with related DAG peers by outputting `ASK_WORKER: <target_node_id>: <question>` or `BROADCAST_TO_PEERS: <message>`. Restrict this to direct upstream, direct downstream, or same-layer parallel peers unless the prompt explicitly authorizes broader communication.
- Every worker must produce an artifact summary. Use file_change for coder tasks, research_note for explore tasks, review_report for review tasks, test_result for shell/test tasks, decision for key decisions, and final_output for final delivery.
- Hidden reasoning is not an artifact. Ask workers to report observable findings, commands, files, diffs, decisions, and results.

Alternatively, use todowrite to list your planned tasks — each todo item will be treated as a child task.
"""


# ---------------------------------------------------------------------------
# Strategy 1: plan.json lines (from create_child_task tool)
# ---------------------------------------------------------------------------

def parse_plan_json(raw_output: str) -> list[dict]:
    """Parse child task entries from plan.json lines.

    The create_child_task tool appends one JSON object per line to
    /workspace/.workflow/plan.json.  Each line has the shape:

        {"child_node_id": "...", "type": "coder", "prompt": "...", "title": "..."}

    This function scans *raw_output* (which may be the full stream.jsonl
    contents) for lines matching that schema.

    Returns a list of validated task dicts [{type, prompt, model?}].
    """
    tasks: list[dict] = []

    for line in raw_output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        # A plan.json line must have at least "type" and "prompt".
        # It may also have "child_node_id" and "title" which we ignore here
        # (the orchestrator assigns its own child node IDs).
        if not isinstance(obj, dict):
            continue
        if "type" not in obj or "prompt" not in obj:
            continue

        task = {
            "type": obj["type"],
            "prompt": obj["prompt"],
        }
        if obj.get("model"):
            task["model"] = obj["model"]
        if obj.get("title"):
            task["title"] = obj["title"]
        if obj.get("dependencies"):
            task["dependencies"] = obj["dependencies"]

        tasks.append(task)

    return _validate_tasks(tasks)


# ---------------------------------------------------------------------------
# Strategy 2 & 3: legacy parsing
# ---------------------------------------------------------------------------

def parse_plan_output(raw_output: str) -> list[dict]:
    """Parse planner output to extract child tasks.

    Strategy order:
    1. Look for JSON code blocks with "tasks" array (most specific)
    2. Look for raw JSON object with "tasks" array
    3. Look for plan.json lines (create_child_task tool output)
    4. Fall back to parsing todowrite tool calls from stream.jsonl lines
    """
    # Strategy 1: JSON in markdown code blocks (greedy — backticks are the boundary)
    code_block_pattern = r'```(?:json)?\s*(\{[\s\S]*\})\s*```'
    for match in re.finditer(code_block_pattern, raw_output):
        try:
            data = json.loads(match.group(1))
            if "tasks" in data and isinstance(data["tasks"], list):
                logger.info("parse_plan_output: found tasks in code block")
                return _validate_tasks(data["tasks"])
        except json.JSONDecodeError:
            continue

    # Strategy 2: raw JSON object with tasks
    brace_pattern = r'\{[^{}]*"tasks"\s*:\s*\[[\s\S]*?\]\s*\}'
    for match in re.finditer(brace_pattern, raw_output):
        try:
            data = json.loads(match.group(0))
            if "tasks" in data and isinstance(data["tasks"], list):
                logger.info("parse_plan_output: found tasks in raw JSON")
                return _validate_tasks(data["tasks"])
        except json.JSONDecodeError:
            continue

    # Strategy 3: plan.json lines (create_child_task tool output)
    plan_json_tasks = parse_plan_json(raw_output)
    if plan_json_tasks:
        logger.info(
            "parse_plan_output: found %d tasks via plan.json parsing",
            len(plan_json_tasks),
        )
        return plan_json_tasks

    # Strategy 4: todowrite tool calls
    todowrite_tasks = parse_todowrite_tasks(raw_output)
    if todowrite_tasks:
        return todowrite_tasks

    logger.warning("No valid plan JSON or todowrite tasks found in planner output")
    return []


def parse_todowrite_tasks(raw_output: str) -> list[dict]:
    """Parse todowrite tool calls from stream.jsonl lines to extract child tasks.

    OpenCode's plan agent uses todowrite to create task lists. Each todowrite call
    has input like: {"todos": [{"content": "...", "status": "in_progress", ...}]}
    """
    tasks = []
    seen_contents = set()

    for line in raw_output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        if ev.get("type") != "tool_use":
            continue

        part = ev.get("part", {})
        tool_name = part.get("tool", "")
        if tool_name != "todowrite":
            continue

        state = part.get("state", {})
        tool_input = state.get("input", {})
        todos = tool_input.get("todos", [])

        for todo in todos:
            if not isinstance(todo, dict):
                continue
            content = todo.get("content", "").strip()
            if not content or content in seen_contents:
                continue
            seen_contents.add(content)

            task_type = _infer_task_type(content)
            tasks.append({
                "type": task_type,
                "prompt": content,
            })

    return tasks


def _infer_task_type(content: str) -> str:
    """Infer the agent type from a task description."""
    lower = content.lower()
    if any(kw in lower for kw in ("merge", "integrate", "conflict", "resolve conflict", "合并", "冲突", "集成")):
        return "merge"
    if any(kw in lower for kw in ("search", "find", "explore", "look for", "investigate", "查找", "搜索")):
        return "explore"
    if any(kw in lower for kw in ("review", "check", "verify", "test", "审计", "检查", "验证")):
        return "review"
    if any(kw in lower for kw in ("run", "execute", "command", "script", "运行", "执行")):
        return "shell"
    return "coder"


def parse_plan_to_dag(plan_output: str) -> tuple[list[dict], list[dict]] | None:
    """Parse structured JSON plan output into (nodes, edges) for the DAG executor.

    Supports two formats:
    1. Planner chat format: {"nodes": [...], "edges": [...]}
    2. Tasks format: {"tasks": [{"id": ..., "type": ..., "prompt": ..., "depends_on": [...]}, ...]}

    Returns None if the output doesn't contain valid structured JSON.
    """
    # Step 1: Try to find a JSON code block (```json ... ``` or ```plan ... ```)
    json_block_pattern = r"```(?:json|plan)\s*(\{[\s\S]*?\})\s*```"
    match = re.search(json_block_pattern, plan_output)
    raw_json = None

    if match:
        raw_json = match.group(1)

    # Step 2: If no code block, try raw JSON (first { to last })
    if raw_json is None:
        first = plan_output.find("{")
        last = plan_output.rfind("}")
        if first != -1 and last > first:
            raw_json = plan_output[first : last + 1]

    if raw_json is None:
        return None

    # Step 3: Parse JSON
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    nodes: list[dict] = []
    edges: list[dict] = []

    # Format 1: Direct nodes/edges (from planner chat)
    if "nodes" in data and isinstance(data["nodes"], list):
        edge_keys: set[tuple[str, str]] = set()
        for node in data["nodes"]:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if not node_id:
                continue
            node_type = node.get("type", "coder")
            if node_type == "plan" and node_id != "planner":
                node_type = "design"
            # Convert to standard format
            nodes.append({
                "id": node_id,
                "type": node_type,
                "data": {
                    "label": node.get("label", node_id),
                    "agent_type": node_type,
                    "prompt": node.get("prompt", ""),
                },
            })

            depends_on = node.get("depends_on", [])
            if isinstance(depends_on, list):
                for dep in depends_on:
                    if not dep:
                        continue
                    edge_keys.add((str(dep), str(node_id)))

        # Parse edges
        if "edges" in data and isinstance(data["edges"], list):
            for edge in data["edges"]:
                if not isinstance(edge, dict):
                    continue
                source = edge.get("source")
                target = edge.get("target")
                if source and target:
                    edge_keys.add((str(source), str(target)))

        for source, target in edge_keys:
            edges.append({
                "id": f"e_{source}_{target}",
                "source": source,
                "target": target,
            })

        if nodes:
            return (nodes, edges)

    # Format 2: Tasks with dependencies (legacy format)
    if "tasks" in data and isinstance(data["tasks"], list):
        tasks = data["tasks"]
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if "id" not in task or "type" not in task or "prompt" not in task or "depends_on" not in task:
                return None

            task_type = task["type"]
            if task_type == "plan" and task["id"] != "planner":
                task_type = "design"
            nodes.append({
                "id": task["id"],
                "type": task_type,
                "data": {
                    "label": task["id"],
                    "agent_type": task_type,
                    "prompt": task["prompt"],
                },
            })

            # Parse depends_on as edges
            depends_on = task.get("depends_on", [])
            if isinstance(depends_on, list):
                for dep in depends_on:
                    edges.append({
                        "id": f"e_{dep}_{task['id']}",
                        "source": dep,
                        "target": task["id"],
                    })

        if nodes:
            return (nodes, edges)

    return None


def _validate_tasks(tasks: list) -> list[dict]:
    """Validate and normalize task definitions."""
    valid_types = {"coder", "explore", "merge", "review", "shell", "build", "plan", "design"}
    result = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_type = task.get("type", "")
        if task_type == "plan" and task.get("id") != "planner":
            task_type = "design"
        if task_type not in valid_types:
            task_type = "coder"
        # Accept "prompt" or "command" (shell tasks may use "command")
        prompt = task.get("prompt", "") or task.get("command", "")
        if not prompt:
            continue
        validated = {
            "type": task_type,
            "prompt": prompt,
        }
        if task.get("model"):
            validated["model"] = task["model"]
        # Structured task fields for the task board
        if task.get("title"):
            validated["title"] = task["title"]
        if task.get("dependencies"):
            validated["dependencies"] = task["dependencies"]
        result.append(validated)
    return result
