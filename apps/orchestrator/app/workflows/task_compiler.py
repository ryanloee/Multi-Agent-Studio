"""Task-to-DAG Compiler: Converts semantic task list to React Flow DAG JSON.

The LLM outputs a simple task list with depends_on references.
This compiler:
1. Validates the task list (id uniqueness, type validity, missing refs)
2. Generates edges from depends_on
3. Auto-inserts structural nodes (merge after parallel coders, review, shell)
4. Builds standard DAG format for frontend consumption
5. Delegates to compile_dag() for topological sort verification
"""

from __future__ import annotations

import logging
import re

from app.workflows.compiler import compile_dag

logger = logging.getLogger(__name__)

VALID_TYPES = {"explore", "design", "coder", "merge", "review", "shell"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_task_list_to_dag(
    tasks: list[dict],
    title: str = "",
    objective: str = "",
    *,
    auto_add_structural: bool = True,
) -> tuple[dict, list[dict]]:
    """Convert a semantic task list into a full DAG dict.

    Args:
        tasks: List of task dicts with id, type, label, prompt, depends_on.
        title: Task title for auto-generated prompts.
        objective: Task objective for auto-generated prompts.
        auto_add_structural: Whether to auto-add merge/review/shell nodes.

    Returns:
        (dag_dict, blockers_list) where dag_dict has {nodes, edges, metadata}.
    """
    # Step 1: Validate and normalize tasks
    normalized = _validate_and_normalize(tasks)
    if not normalized:
        return {"nodes": [], "edges": [], "metadata": {}}, [
            {"code": "no_valid_tasks", "message": "No valid tasks found in planner output."}
        ]

    # Step 2: Check missing dependency references
    task_ids = {t["id"] for t in normalized}
    missing_deps: list[str] = []
    for task in normalized:
        for dep in task.get("depends_on", []):
            if dep not in task_ids:
                missing_deps.append(f"{task['id']} depends on missing '{dep}'")

    # Step 3: Auto-insert structural nodes only for complex task lists
    if auto_add_structural and len(normalized) >= 5:
        _ensure_merge_after_parallel(normalized)
        if not any(t["type"] == "shell" for t in normalized):
            normalized = _auto_add_shell_node(normalized, title, objective)

    # Step 4: Build edges from depends_on
    edges: list[dict] = []
    for task in normalized:
        for dep in task.get("depends_on", []):
            edges.append({"source": dep, "target": task["id"]})

    # Step 5: Build nodes in standard format
    nodes: list[dict] = []
    for task in normalized:
        node_type = task["type"]
        node: dict = {
            "id": task["id"],
            "type": node_type,
            "label": task.get("label", task["id"]),
            "prompt": task.get("prompt", ""),
            "depends_on": task.get("depends_on", []),
            "data": {
                "label": task.get("label", task["id"]),
                "agentType": node_type,
                "prompt": task.get("prompt", ""),
            },
        }
        # Preserve rich context fields
        for key in ("target_files", "interface_contract", "context_summary"):
            if task.get(key):
                node[key] = task[key]
                node["data"][key] = task[key]
        nodes.append(node)

    dag = {"nodes": nodes, "edges": edges, "metadata": {"source": "task_compiler"}}

    # Step 6: Verify with topological sort (cycle detection)
    try:
        compile_dag(dag)
    except ValueError as exc:
        logger.warning("Task compiler: cycle detected: %s", exc)
        return dag, [{"code": "dag_cycle", "message": str(exc)}]

    blockers: list[dict] = []
    if missing_deps:
        blockers.append({
            "code": "missing_dependencies",
            "message": "; ".join(missing_deps),
        })

    logger.info(
        "Task compiler: %d nodes, %d edges, %d blockers",
        len(nodes), len(edges), len(blockers),
    )
    return dag, blockers


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_and_normalize(tasks: list[dict]) -> list[dict]:
    """Validate tasks and return normalized list."""
    if not isinstance(tasks, list):
        return []
    result: list[dict] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("id") or "").strip()
        task_type = str(raw.get("type") or "").strip().lower()
        label = str(raw.get("label") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()

        if not task_id:
            task_id = f"task_{i + 1}"
        # Deduplicate ids
        base_id = task_id
        suffix = 2
        while task_id in seen_ids:
            task_id = f"{base_id}_{suffix}"
            suffix += 1
        seen_ids.add(task_id)

        # Validate type, default to coder
        if task_type not in VALID_TYPES:
            logger.warning("Task '%s' has invalid type '%s', defaulting to 'coder'", task_id, task_type)
            task_type = "coder"

        if not label:
            label = task_id.replace("_", " ").title()
        if not prompt:
            prompt = f"Execute task: {label}"

        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        depends_on = [str(d) for d in depends_on if d]

        normalized_task: dict = {
            "id": task_id,
            "type": task_type,
            "label": label,
            "prompt": prompt,
            "depends_on": depends_on,
        }
        for key in ("target_files", "interface_contract", "context_summary"):
            if raw.get(key):
                normalized_task[key] = raw[key]
        result.append(normalized_task)
    return result


# ---------------------------------------------------------------------------
# Structural node auto-insertion
# ---------------------------------------------------------------------------


def _ensure_merge_after_parallel(tasks: list[dict]) -> None:
    """Only insert a merge node if 2+ parallel coders share the same deps AND
    no existing node (of any type) depends on all of them.  Mutates tasks in-place."""
    coder_tasks = [t for t in tasks if t["type"] == "coder"]
    if len(coder_tasks) < 2:
        return

    dep_groups: dict[tuple[str, ...], list[dict]] = {}
    for coder in coder_tasks:
        dep_key = tuple(sorted(coder.get("depends_on", [])))
        dep_groups.setdefault(dep_key, []).append(coder)

    for dep_key, group in dep_groups.items():
        if len(group) < 2:
            continue
        coder_ids = {t["id"] for t in group}
        # Already have something that collects all parallel outputs? Skip.
        already_collected = any(
            coder_ids.issubset(set(t.get("depends_on", [])))
            for t in tasks
        )
        if already_collected:
            continue
        merge_id = _unique_id(tasks, f"merge_{'_'.join(sorted(coder_ids)[:3])}")
        tasks.append({
            "id": merge_id,
            "type": "merge",
            "label": "合并并行实现改动",
            "prompt": (
                "目标：合并所有并行 coder 节点的改动。\n"
                "具体要求：读取上游 diff/report/commit 信息，处理冲突，形成集成工作区。\n"
                "产出格式：merge_report。\n"
                "验收标准：所有上游改动已集成或明确列出阻塞冲突。"
            ),
            "depends_on": sorted(coder_ids),
        })



def _auto_add_shell_node(tasks: list[dict], title: str, objective: str) -> list[dict]:
    """Add a shell/test node at the end if none exists."""
    has_shell = any(t["type"] == "shell" for t in tasks)
    if has_shell:
        return tasks

    terminal_ids = _find_terminal_ids(tasks)
    # Depend on review or last terminal nodes
    shell_deps: list[str] = []
    for t in tasks:
        if t["id"] in terminal_ids and t["type"] == "review":
            shell_deps.append(t["id"])
    if not shell_deps:
        shell_deps = sorted(terminal_ids)[:3]

    shell_id = _unique_id(tasks, "run_tests")
    shell_task: dict = {
        "id": shell_id,
        "type": "shell",
        "label": "运行集成验证",
        "prompt": (
            f"目标：为「{title}」运行项目可用的构建、lint、测试或启动验证命令。\n"
            f"背景：{objective}\n"
            "产出格式：test_result。\n"
            "验收标准：报告命令、通过/失败数量和失败原因。"
        ),
        "depends_on": shell_deps,
    }
    return tasks + [shell_task]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_terminal_ids(tasks: list[dict]) -> set[str]:
    """Find task IDs that no other task depends on."""
    all_ids = {t["id"] for t in tasks}
    depended_on: set[str] = set()
    for t in tasks:
        for dep in t.get("depends_on", []):
            depended_on.add(dep)
    return all_ids - depended_on


def _unique_id(tasks: list[dict], base: str) -> str:
    """Generate a unique ID based on base, avoiding collisions with existing tasks."""
    existing = {t["id"] for t in tasks}
    if base not in existing:
        return base
    suffix = 2
    while f"{base}_{suffix}" in existing:
        suffix += 1
    return f"{base}_{suffix}"
