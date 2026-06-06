"""Shared pytest helpers."""

from __future__ import annotations

from pathlib import Path

from cyclopsctl.tasks.store import save_tag_tasks

DEFAULT_LOOP_TASK_IDS: tuple[int, ...] = (3, 5, 8, 9, 10, 16, 17, 21, 22)


def seed_native_tasks(
    root: Path,
    task_ids: tuple[int, ...] = DEFAULT_LOOP_TASK_IDS,
    *,
    tag: str = "master",
) -> None:
    """Seed native ``.cyclopsctl/tasks/tasks.json`` for loop integration tests."""
    tasks = [
        {
            "id": task_id,
            "title": f"Task {task_id}",
            "status": "pending",
            "dependencies": [],
        }
        for task_id in task_ids
    ]
    path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(path, tasks, tag=tag, merge=False)
