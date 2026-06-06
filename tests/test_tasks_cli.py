"""CLI integration tests for native ``cyclopsctl tasks`` CRUD (task 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.cli import main
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.tasks.cli import (
    get_next_task,
    list_pending_tasks,
    list_tasks,
    set_task_status,
)
from cyclopsctl.tasks.store import save_tag_tasks, set_current_tag


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root.resolve()


def _sample_task(
    task_id: int,
    *,
    title: str = "Sample",
    dependencies: list[int | str] | None = None,
    status: str = "pending",
    complexity: int = 5,
) -> dict:
    return {
        "id": task_id,
        "title": title,
        "description": "desc",
        "details": "details",
        "testStrategy": "pytest",
        "priority": "high",
        "dependencies": dependencies or [],
        "status": status,
        "subtasks": [],
        "complexity": complexity,
    }


def _seed_tasks(project_root: Path, tasks: list[dict], *, tag: str = "master") -> None:
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(path, tasks, tag=tag, merge=False)


def test_list_tasks_all_includes_done_and_sorts(project_root: Path):
    _seed_tasks(
        project_root,
        [
            _sample_task(3, title="Three", status="pending"),
            _sample_task(1, title="One", status="in-progress"),
            _sample_task(2, title="Two", status="done"),
            _sample_task(4, title="Four", status="deferred"),
        ],
    )

    tag, tasks = list_tasks(project_root)
    assert tag == "master"
    assert [task["id"] for task in tasks] == [1, 2, 3, 4]


def test_list_tasks_done_filter(project_root: Path):
    _seed_tasks(
        project_root,
        [
            _sample_task(1, title="One", status="done"),
            _sample_task(2, title="Two", status="pending"),
            _sample_task(3, title="Three", status="done"),
        ],
    )

    tag, tasks = list_tasks(project_root, status_filter="done")
    assert tag == "master"
    assert [task["id"] for task in tasks] == [1, 3]


def test_list_pending_filters_non_done_and_sorts(project_root: Path):
    _seed_tasks(
        project_root,
        [
            _sample_task(3, title="Three", status="pending"),
            _sample_task(1, title="One", status="in-progress"),
            _sample_task(2, title="Two", status="done"),
            _sample_task(4, title="Four", status="deferred"),
        ],
    )

    tag, tasks = list_pending_tasks(project_root)
    assert tag == "master"
    assert [task["id"] for task in tasks] == [1, 3, 4]


def test_get_next_respects_dependencies(project_root: Path):
    _seed_tasks(
        project_root,
        [
            _sample_task(1, title="One", status="done"),
            _sample_task(2, title="Two", status="pending", dependencies=[1]),
            _sample_task(3, title="Three", status="pending", dependencies=[99]),
            _sample_task(4, title="Four", status="pending"),
        ],
    )

    lookup = get_next_task(project_root)
    assert lookup.found is True
    assert lookup.task is not None
    assert lookup.task.numeric_id == 2


def test_get_next_returns_empty_when_no_eligible_tasks(project_root: Path):
    _seed_tasks(
        project_root,
        [
            _sample_task(1, title="One", status="pending", dependencies=[99]),
            _sample_task(2, title="Two", status="pending", dependencies=[1]),
        ],
    )

    lookup = get_next_task(project_root)
    assert lookup.found is False
    assert lookup.task is None


def test_set_status_updates_record_and_timestamp(project_root: Path):
    _seed_tasks(project_root, [_sample_task(1, status="pending")])

    set_task_status(project_root, "1", "done")

    path = project_root / ".cyclopsctl/tasks/tasks.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    task = data["master"]["tasks"][0]
    assert task["status"] == "done"
    assert isinstance(task["updatedAt"], str)
    assert task["updatedAt"].endswith("Z")


def test_set_status_rejects_invalid_status(project_root: Path):
    _seed_tasks(project_root, [_sample_task(1, status="pending")])

    with pytest.raises(Exception, match="invalid status"):
        set_task_status(project_root, "1", "not-a-status")


def test_cli_list_pending_json_parser_compatible(project_root: Path, capsys):
    _seed_tasks(
        project_root,
        [
            _sample_task(2, title="Two", status="pending"),
            _sample_task(7, title="Seven", status="in-progress"),
        ],
    )

    code = main(
        [
            "tasks",
            "list",
            "pending",
            "--format",
            "json",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0

    payload = json.loads(captured.out)
    tasks = payload["tasks"]
    assert [task["id"] for task in tasks] == ["2", "7"]
    assert tasks[0]["title"] == "Two"


def test_cli_show_json_parser_compatible(project_root: Path, capsys):
    _seed_tasks(project_root, [_sample_task(16, title="Sixteen", status="pending")])

    code = main(
        [
            "tasks",
            "show",
            "16",
            "--format",
            "json",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0

    payload = json.loads(captured.out)
    task = payload["task"]
    assert int(task["id"]) == 16
    assert task["title"] == "Sixteen"
    assert task["testStrategy"] == "pytest"
    assert payload["found"] is True


def test_cli_next_json_parser_compatible(project_root: Path, capsys):
    _seed_tasks(
        project_root,
        [
            _sample_task(1, title="One", status="done"),
            _sample_task(2, title="Two", status="pending", dependencies=[1]),
        ],
    )

    code = main(
        [
            "tasks",
            "next",
            "--format",
            "json",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0

    payload = json.loads(captured.out)
    assert payload["found"] is True
    assert int(payload["task"]["id"]) == 2


def test_cli_set_status_json(project_root: Path, capsys):
    _seed_tasks(project_root, [_sample_task(3, status="pending")])

    code = main(
        [
            "tasks",
            "set-status",
            "--id=3",
            "--status=done",
            "--format",
            "json",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["updated"] is True
    assert payload["id"] == "3"
    assert payload["status"] == "done"


def test_cli_show_missing_task_exits_non_zero(project_root: Path, capsys):
    _seed_tasks(project_root, [_sample_task(1)])

    code = main(
        [
            "tasks",
            "show",
            "99",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "task not found" in captured.err.lower()


def test_cli_set_status_missing_task_exits_non_zero(project_root: Path, capsys):
    _seed_tasks(project_root, [_sample_task(1)])

    code = main(
        [
            "tasks",
            "set-status",
            "--id=99",
            "--status=done",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "task not found" in captured.err.lower()


def test_cli_corrupt_tasks_file_exits_non_zero(project_root: Path, capsys):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    code = main(
        [
            "tasks",
            "list",
            "pending",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "invalid json" in captured.err.lower()


def test_cli_list_all_default_shows_table(project_root: Path, capsys):
    _seed_tasks(
        project_root,
        [
            _sample_task(1, title="Alpha", status="done", complexity=3),
            _sample_task(2, title="Beta", status="pending", complexity=7, dependencies=[1]),
        ],
    )

    code = main(
        [
            "tasks",
            "list",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Alpha" in captured.out
    assert "Beta" in captured.out
    assert "done" in captured.out
    assert "pending" in captured.out
    assert "Complexity" in captured.out
    assert "1" in captured.out


def test_cli_list_done_filter(project_root: Path, capsys):
    _seed_tasks(
        project_root,
        [
            _sample_task(1, title="Finished", status="done"),
            _sample_task(2, title="Open", status="pending"),
        ],
    )

    code = main(
        [
            "tasks",
            "list",
            "done",
            "--format",
            "json",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["filter"] == "done"
    assert [task["id"] for task in payload["tasks"]] == ["1"]


def test_cli_list_pending_plain_table_output(project_root: Path, capsys):
    _seed_tasks(project_root, [_sample_task(5, title="Plain task", status="pending")])

    code = main(
        [
            "tasks",
            "list",
            "pending",
            "--plain-table",
            "--project-root",
            str(project_root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Plain task" in captured.out
    assert "pending" in captured.out
    assert "ID" in captured.out


def test_cli_respects_tag_flag(project_root: Path, capsys):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    save_tag_tasks(path, [_sample_task(1, title="Master task")], tag="master", merge=False)
    save_tag_tasks(path, [_sample_task(9, title="Phase task")], tag="phase-5", merge=True)
    set_current_tag(project_root / ".cyclopsctl/tasks/state.json", "phase-5")

    code = main(
        [
            "tasks",
            "list",
            "pending",
            "--format",
            "json",
            "--project-root",
            str(project_root),
            "--tag",
            "phase-5",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["tag"] == "phase-5"
    assert payload["tasks"][0]["id"] == "9"


def test_native_backend_list_and_next(tmp_path: Path):
    from cyclopsctl.tasks.native_backend import NativeTaskBackend

    root = tmp_path / "repo"
    root.mkdir()
    _seed_tasks(
        root,
        [
            _sample_task(1, status="done"),
            _sample_task(2, status="pending", dependencies=[1]),
        ],
    )

    backend = NativeTaskBackend()
    pending = backend.list_pending(root)
    assert [task.numeric_id for task in pending] == [2]

    lookup = backend.get_next(root)
    assert lookup.found is True
    assert lookup.task is not None
    assert lookup.task.numeric_id == 2

    detail = backend.show(root, "2")
    assert detail.numeric_id == 2

    backend.set_status(root, "2", "done")
    lookup_after = backend.get_next(root)
    assert lookup_after.found is False
