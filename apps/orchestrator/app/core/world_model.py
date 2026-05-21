"""World model for Director execution state tracking.

Tracks node execution progress, Planner review context, and project state.
Designed for checkpoint persistence (resume from interruption).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class TaskSummary:
    task_id: str
    action: str
    summary: str
    files_changed: list[str] = field(default_factory=list)
    success: bool = True


@dataclass
class FailureRecord:
    task_id: str
    action: str
    error: str
    prompt_hint: str = ""


@dataclass
class ReviewRecord:
    task_id: str
    passed: bool
    reason: str
    attempt: int
    next_prompt: str = ""


@dataclass
class WorldModel:
    goal: str
    project_structure: str = ""
    completed_tasks: list[TaskSummary] = field(default_factory=list)
    failed_attempts: list[FailureRecord] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)
    current_file_snapshot: str = ""

    current_node_index: int = 0
    iteration: int = 0
    last_node_id: str = ""
    node_queue: list[dict] = field(default_factory=list)
    node_statuses: dict[str, str] = field(default_factory=dict)

    planner_review_messages: list[dict] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        lines: list[str] = []
        lines.append(f"## Goal\n{self.goal}")

        if self.project_structure:
            lines.append(f"\n## Project Structure\n{self.project_structure}")

        if self.current_file_snapshot:
            lines.append(f"\n## Current Changes (git diff --stat)\n{self.current_file_snapshot}")

        if self.completed_tasks:
            lines.append("\n## Completed Steps")
            for t in self.completed_tasks[-15:]:
                icon = "+" if t.success else "-"
                changed = f" -> {', '.join(t.files_changed[:5])}" if t.files_changed else ""
                lines.append(f"  [{icon}] {t.task_id} ({t.action}): {t.summary}{changed}")

        if self.failed_attempts:
            lines.append("\n## Failed Attempts")
            for f in self.failed_attempts[-8:]:
                lines.append(f"  [!] {f.task_id} ({f.action}): {f.error[:120]}")
                if f.prompt_hint:
                    lines.append(f"      prompt was: {f.prompt_hint[:80]}")

        if self.reviews:
            lines.append("\n## Review History")
            for r in self.reviews[-10:]:
                icon = "PASS" if r.passed else "REJECT"
                lines.append(f"  [{icon}] {r.task_id} (attempt {r.attempt}): {r.reason[:120]}")

        return "\n".join(lines)

    def to_json(self) -> str:
        payload = {
            "goal": self.goal,
            "project_structure": self.project_structure,
            "completed_tasks": [asdict(t) for t in self.completed_tasks],
            "failed_attempts": [asdict(f) for f in self.failed_attempts],
            "reviews": [asdict(r) for r in self.reviews],
            "current_file_snapshot": self.current_file_snapshot,
            "current_node_index": self.current_node_index,
            "iteration": self.iteration,
            "last_node_id": self.last_node_id,
            "node_queue": self.node_queue,
            "node_statuses": self.node_statuses,
            "planner_review_messages": self.planner_review_messages,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str) -> WorldModel:
        raw = json.loads(data)
        completed_tasks = [
            TaskSummary(
                task_id=t.get("task_id", ""),
                action=t.get("action", ""),
                summary=t.get("summary", ""),
                files_changed=t.get("files_changed", []),
                success=t.get("success", True),
            )
            for t in raw.get("completed_tasks", [])
        ]
        failed_attempts = [
            FailureRecord(
                task_id=f.get("task_id", ""),
                action=f.get("action", ""),
                error=f.get("error", ""),
                prompt_hint=f.get("prompt_hint", ""),
            )
            for f in raw.get("failed_attempts", [])
        ]
        reviews = [
            ReviewRecord(
                task_id=r.get("task_id", ""),
                passed=r.get("passed", False),
                reason=r.get("reason", ""),
                attempt=r.get("attempt", 0),
                next_prompt=r.get("next_prompt", ""),
            )
            for r in raw.get("reviews", [])
        ]
        model = cls(
            goal=raw.get("goal", ""),
            project_structure=raw.get("project_structure", ""),
            current_file_snapshot=raw.get("current_file_snapshot", ""),
            current_node_index=raw.get("current_node_index", 0),
            iteration=raw.get("iteration", 0),
            last_node_id=raw.get("last_node_id", ""),
            node_queue=raw.get("node_queue", []),
            node_statuses=raw.get("node_statuses", {}),
            planner_review_messages=raw.get("planner_review_messages", []),
            completed_tasks=completed_tasks,
            failed_attempts=failed_attempts,
            reviews=reviews,
        )
        return model

    def record_success(
        self,
        task_id: str,
        action: str,
        summary: str,
        files_changed: list[str] | None = None,
    ) -> None:
        self.completed_tasks.append(TaskSummary(
            task_id=task_id,
            action=action,
            summary=summary[:300],
            files_changed=files_changed or [],
            success=True,
        ))
        self.node_statuses[task_id] = "completed"

    def record_failure(self, task_id: str, action: str, error: str, prompt_hint: str = "") -> None:
        self.failed_attempts.append(FailureRecord(
            task_id=task_id,
            action=action,
            error=error[:300],
            prompt_hint=prompt_hint[:150],
        ))
        self.node_statuses[task_id] = "failed"

    def record_review(
        self, task_id: str, passed: bool, reason: str, attempt: int, next_prompt: str = "",
    ) -> None:
        self.reviews.append(ReviewRecord(
            task_id=task_id,
            passed=passed,
            reason=reason[:300],
            attempt=attempt,
            next_prompt=next_prompt[:300],
        ))
