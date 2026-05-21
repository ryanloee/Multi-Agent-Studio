"""Compressed world model for the Director dispatch loop.

The world model tracks project state across iterations, providing the Director
with enough context to make informed decisions without bloating its prompt.
Target size: 2-4 KB when serialized.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class TaskSummary:
    """One-line summary of a completed dispatch step."""

    task_id: str
    action: str  # scout | worker | test
    summary: str
    files_changed: list[str] = field(default_factory=list)
    success: bool = True


@dataclass
class FailureRecord:
    """Record of a failed dispatch to help the Director avoid repeating mistakes."""

    task_id: str
    action: str
    error: str
    prompt_hint: str = ""


@dataclass
class ReviewRecord:
    """Record of a Director review on a worker's output."""

    task_id: str
    passed: bool
    reason: str
    attempt: int
    next_prompt: str = ""


@dataclass
class WorldModel:
    """Compressed project state carried across Director iterations."""

    goal: str
    project_structure: str = ""
    completed_tasks: list[TaskSummary] = field(default_factory=list)
    failed_attempts: list[FailureRecord] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)
    current_file_snapshot: str = ""
    iteration: int = 0
    max_iterations: int = 9999  # Effectively unlimited; relies on time limits

    def to_prompt_context(self) -> str:
        """Serialize to a compact text block for the Director prompt.

        Keeps output to ~2-4 KB. Does NOT include raw code — only summaries
        and file paths so the Director knows what exists and what changed.
        """
        lines: list[str] = []

        lines.append(f"## Goal\n{self.goal}")
        lines.append(f"\n## Iteration: {self.iteration}")

        if self.project_structure:
            lines.append(f"\n## Project Structure\n{self.project_structure}")

        if self.current_file_snapshot:
            lines.append(f"\n## Current Changes (git diff --stat)\n{self.current_file_snapshot}")

        if self.completed_tasks:
            lines.append("\n## Completed Steps")
            for t in self.completed_tasks[-15:]:  # keep last 15
                icon = "+" if t.success else "-"
                changed = f" → {', '.join(t.files_changed[:5])}" if t.files_changed else ""
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
            "completed_tasks": [
                {
                    "task_id": t.task_id,
                    "action": t.action,
                    "summary": t.summary,
                    "files_changed": t.files_changed,
                    "success": t.success,
                }
                for t in self.completed_tasks
            ],
            "failed_attempts": [
                {
                    "task_id": f.task_id,
                    "action": f.action,
                    "error": f.error,
                    "prompt_hint": f.prompt_hint,
                }
                for f in self.failed_attempts
            ],
            "reviews": [
                {
                    "task_id": r.task_id,
                    "passed": r.passed,
                    "reason": r.reason,
                    "attempt": r.attempt,
                    "next_prompt": r.next_prompt,
                }
                for r in self.reviews
            ],
            "current_file_snapshot": self.current_file_snapshot,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str) -> WorldModel:
        raw = json.loads(data)
        model = cls(
            goal=raw.get("goal", ""),
            project_structure=raw.get("project_structure", ""),
            current_file_snapshot=raw.get("current_file_snapshot", ""),
            iteration=raw.get("iteration", 0),
            max_iterations=raw.get("max_iterations", 9999),
        )
        for t in raw.get("completed_tasks", []):
            model.completed_tasks.append(TaskSummary(
                task_id=t.get("task_id", ""),
                action=t.get("action", ""),
                summary=t.get("summary", ""),
                files_changed=t.get("files_changed", []),
                success=t.get("success", True),
            ))
        for f in raw.get("failed_attempts", []):
            model.failed_attempts.append(FailureRecord(
                task_id=f.get("task_id", ""),
                action=f.get("action", ""),
                error=f.get("error", ""),
                prompt_hint=f.get("prompt_hint", ""),
            ))
        for r in raw.get("reviews", []):
            model.reviews.append(ReviewRecord(
                task_id=r.get("task_id", ""),
                passed=r.get("passed", False),
                reason=r.get("reason", ""),
                attempt=r.get("attempt", 0),
                next_prompt=r.get("next_prompt", ""),
            ))
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

    def record_failure(self, task_id: str, action: str, error: str, prompt_hint: str = "") -> None:
        self.failed_attempts.append(FailureRecord(
            task_id=task_id,
            action=action,
            error=error[:300],
            prompt_hint=prompt_hint[:150],
        ))

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
