"""Tests for task 16: configurable cycle task selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.config import ConfigError, build_run_config, normalize_task_source
from cyclopsctl.task_selection import (
    TaskSelectionState,
    format_selection_mismatch_message,
    resolve_cycle_task,
)
from cyclopsctl.tasks.cli import get_task_by_id, list_pending_tasks
from cyclopsctl.tasks.store import save_tag_tasks
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.task_selection import TaskSelectionError


def _task(task_id: int, *, status: str = "pending") -> NextTaskResult:
    return NextTaskResult(
        task_id=str(task_id),
        title=f"Task {task_id}",
        status=status,
        priority="high",
        complexity=5,
        tag="master",
    )


def _lookup(task_id: int, *, status: str = "pending") -> NextTaskLookup:
    return NextTaskLookup.from_task(_task(task_id, status=status))


def _base_cli(root: Path) -> dict:
    return {
        "cycles": 1,
        "project_root": root,
        "first_prompt": root / "prompts" / "first.md",
        "current_handover": root / "current-handover-prompt.md",
        "update_handover": root / "update-handover-prompt.md",
    }


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / "prompts" / "first.md").write_text("# First\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 16\n\nImplement task 16.\n",
        encoding="utf-8",
    )
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        "{}", encoding="utf-8"
    )
    return root.resolve()


def test_normalize_task_source_accepts_valid_modes():
    assert normalize_task_source("handover") == "handover"
    assert normalize_task_source("SEQUENTIAL") == "sequential"
    assert normalize_task_source("handover") == "handover"


def test_normalize_task_source_rejects_unknown_mode():
    with pytest.raises(ValueError, match="task-source must be one of"):
        normalize_task_source("priority")


def test_build_run_config_defaults_task_source_handover(project_tree: Path):
    cfg = build_run_config(_base_cli(project_tree))
    assert cfg.task_source == "handover"
    assert cfg.pinned_task_id is None


def test_build_run_config_task_source_and_task_id(project_tree: Path):
    cfg = build_run_config(
        {
            **_base_cli(project_tree),
            "task_source": "sequential",
            "pinned_task_id": 7,
        }
    )
    assert cfg.task_source == "sequential"
    assert cfg.pinned_task_id == 7


def test_build_run_config_invalid_task_source_raises(project_tree: Path):
    with pytest.raises(ConfigError, match="task-source must be one of"):
        build_run_config({**_base_cli(project_tree), "task_source": "bogus"})


def test_build_run_config_invalid_task_id_raises(project_tree: Path):
    with pytest.raises(ConfigError, match="task-id must be a positive integer"):
        build_run_config({**_base_cli(project_tree), "pinned_task_id": "nope"})


def test_sequential_picks_lowest_pending_id(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "task_source": "sequential"})

    def fake_list(_root: Path, *, tag: str | None = None) -> list[NextTaskResult]:
        return [_task(7), _task(2), _task(10)]

    lookup = resolve_cycle_task(
        cfg,
        list_pending_tasks_fn=fake_list,
        get_next_task_fn=lambda *_a, **_k: _lookup(99),
    )

    assert lookup.found is True
    assert lookup.task is not None
    assert lookup.task.numeric_id == 2


def test_handover_mode_routes_by_handover_id(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "task_source": "handover"})

    def fake_show(_root: Path, task_id: int | str, *, tag: str | None = None):
        assert task_id == 16
        return _lookup(16)

    lookup = resolve_cycle_task(
        cfg,
        get_task_by_id_fn=fake_show,
        get_next_task_fn=lambda *_a, **_k: _lookup(8),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 16


def test_handover_mode_falls_back_when_handover_missing(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "task_source": "handover"})
    (project_tree / "current-handover-prompt.md").unlink()

    lookup = resolve_cycle_task(
        cfg,
        allow_missing_handover=True,
        list_pending_tasks_fn=lambda *_a, **_k: [],
        get_next_task_fn=lambda *_a, **_k: _lookup(8),
        get_task_by_id_fn=lambda *_a, **_k: _lookup(99),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 8


def test_handover_mode_falls_back_when_task_id_zero(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "task_source": "handover"})
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 0\n\nQueue complete.\n",
        encoding="utf-8",
    )

    lookup = resolve_cycle_task(
        cfg,
        list_pending_tasks_fn=lambda *_a, **_k: [],
        get_next_task_fn=lambda *_a, **_k: _lookup(17),
        get_task_by_id_fn=lambda *_a, **_k: NextTaskLookup.empty(),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 17


def test_handover_mode_falls_back_when_task_not_in_queue(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "task_source": "handover"})
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 99\n\nStale handover.\n",
        encoding="utf-8",
    )

    lookup = resolve_cycle_task(
        cfg,
        list_pending_tasks_fn=lambda *_a, **_k: [],
        get_next_task_fn=lambda *_a, **_k: _lookup(17),
        get_task_by_id_fn=lambda *_a, **_k: NextTaskLookup.empty(),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 17


def test_handover_mode_falls_back_when_task_not_pending(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "task_source": "handover"})
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 5\n\nDone task.\n",
        encoding="utf-8",
    )

    lookup = resolve_cycle_task(
        cfg,
        list_pending_tasks_fn=lambda *_a, **_k: [],
        get_next_task_fn=lambda *_a, **_k: _lookup(17),
        get_task_by_id_fn=lambda *_a, **_k: _lookup(5, status="done"),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 17


def test_handover_selection_fallback_reason_detects_task_zero(project_tree: Path):
    from cyclopsctl.task_selection import handover_selection_fallback_reason

    cfg = build_run_config({**_base_cli(project_tree), "task_source": "handover"})
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 0\n\nQueue complete.\n",
        encoding="utf-8",
    )

    reason = handover_selection_fallback_reason(
        cfg,
        tag=None,
        allow_missing_handover=False,
        get_task_by_id_fn=lambda *_a, **_k: NextTaskLookup.empty(),
    )

    assert reason is not None
    assert "queue-complete" in reason


def test_handover_mode_can_use_backend_next_fallback(project_tree: Path):
    cfg = build_run_config(
        {**_base_cli(project_tree), "task_source": "handover"}
    )
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 99\n\nStale handover.\n",
        encoding="utf-8",
    )

    lookup = resolve_cycle_task(
        cfg,
        list_pending_tasks_fn=lambda *_a, **_k: [],
        get_next_task_fn=lambda *_a, **_k: _lookup(7),
        get_task_by_id_fn=lambda *_a, **_k: NextTaskLookup.empty(),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 7


def test_pinned_task_id_validates_pending(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "pinned_task_id": 5})
    state = TaskSelectionState(pinned_task_id=5)

    lookup = resolve_cycle_task(
        cfg,
        selection_state=state,
        get_task_by_id_fn=lambda *_a, **_k: _lookup(5, status="pending"),
    )

    assert lookup.task is not None
    assert lookup.task.numeric_id == 5
    assert state.pin_consumed is True


def test_pinned_task_id_rejects_non_pending(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "pinned_task_id": 5})

    with pytest.raises(TaskSelectionError, match="not pending"):
        resolve_cycle_task(
            cfg,
            get_task_by_id_fn=lambda *_a, **_k: _lookup(5, status="done"),
        )


def test_pinned_task_id_rejects_missing_task(project_tree: Path):
    cfg = build_run_config({**_base_cli(project_tree), "pinned_task_id": 5})

    with pytest.raises(TaskSelectionError, match="not found"):
        resolve_cycle_task(
            cfg,
            get_task_by_id_fn=lambda *_a, **_k: NextTaskLookup.empty(),
        )


def test_pinned_task_id_only_applies_to_first_cycle(project_tree: Path):
    cfg = build_run_config(
        {
            **_base_cli(project_tree),
            "pinned_task_id": 5,
            "task_source": "handover",
        }
    )
    state = TaskSelectionState(pinned_task_id=5)
    calls = {"next": 0}

    def fake_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        calls["next"] += 1
        return _lookup(9)

    first = resolve_cycle_task(
        cfg,
        selection_state=state,
        get_task_by_id_fn=lambda *_a, **_k: _lookup(5),
        get_next_task_fn=fake_next,
    )
    second = resolve_cycle_task(
        cfg,
        list_pending_tasks_fn=lambda *_a, **_k: [],
        selection_state=state,
        get_task_by_id_fn=lambda *_a, **_k: NextTaskLookup.empty(),
        get_next_task_fn=fake_next,
    )

    assert first.task is not None and first.task.numeric_id == 5
    assert second.task is not None and second.task.numeric_id == 9
    assert calls["next"] == 1


def test_format_selection_mismatch_message():
    message = format_selection_mismatch_message(
        selected_task_id=2,
        backend_next_id=7,
        task_source="sequential",
        project_root=Path("G:/DEV/CursorOrchestrator"),
    )
    assert "Selected task ID 2" in message
    assert "backend next task ID 7" in message
    assert "task-source: sequential" in message


def _seed_tasks(project_root: Path, tasks: list[dict]) -> None:
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(path, tasks, tag="master", merge=False)


def test_list_pending_tasks_from_native_store(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_tasks(
        root,
        [
            {"id": 2, "title": "Two", "status": "pending", "dependencies": []},
            {"id": 7, "title": "Seven", "status": "pending", "dependencies": []},
        ],
    )
    active_tag, tasks = list_pending_tasks(root)
    assert active_tag == "master"
    assert [task["id"] for task in tasks] == [2, 7]


def test_get_task_by_id_from_native_store(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_tasks(
        root,
        [{"id": 16, "title": "Sixteen", "status": "pending", "dependencies": []}],
    )
    lookup = get_task_by_id(root, 16)
    assert lookup.found is True
    assert lookup.task is not None
    assert lookup.task.numeric_id == 16
