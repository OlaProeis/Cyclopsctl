"""Shared task dataclasses used by all task backends."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NextTaskResult:
    """Parsed parent task from task backend ``next`` output."""

    task_id: str
    title: str
    status: str | None
    priority: str | None
    complexity: int | None
    tag: str | None
    description: str | None = None

    @property
    def numeric_id(self) -> int:
        """Parent task id as integer (for complexity report lookup)."""
        return int(self.task_id)


@dataclass(frozen=True)
class TaskShowDetail:
    """Full parent task fields from task backend ``show`` output."""

    task_id: str
    title: str
    description: str | None
    details: str | None
    test_strategy: str | None
    priority: str | None
    dependencies: tuple[str, ...]
    status: str | None
    complexity: int | None

    @property
    def numeric_id(self) -> int:
        return int(self.task_id)


@dataclass(frozen=True)
class NextTaskLookup:
    """Outcome of task backend ``next`` — task found or empty queue."""

    found: bool
    task: NextTaskResult | None = None
    tag: str | None = None

    @classmethod
    def from_task(cls, task: NextTaskResult, *, tag: str | None = None) -> NextTaskLookup:
        return cls(found=True, task=task, tag=tag if tag is not None else task.tag)

    @classmethod
    def empty(cls, *, tag: str | None = None) -> NextTaskLookup:
        return cls(found=False, task=None, tag=tag)
