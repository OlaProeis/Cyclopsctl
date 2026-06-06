"""Tests for native task storage layout and atomic JSON persistence (task 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.tasks.store import (
    CircularDependencyError,
    TagState,
    TaskStore,
    TaskStoreCorruptError,
    TaskStoreNotFoundError,
    ensure_native_layout,
    load_complexity_report,
    load_tag_state,
    load_tag_tasks,
    load_tasks_document,
    resolve_complexity_report_path,
    resolve_tag_state_path,
    resolve_tasks_json_path,
    save_complexity_report,
    save_tag_state,
    save_tag_tasks,
    save_tasks_document,
    set_current_tag,
    validate_no_circular_dependencies,
)


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
        "complexity": 5,
    }


def test_path_resolution_native_backend(project_root: Path):
    assert resolve_tasks_json_path(project_root, backend="native") == (
        project_root / ".cyclopsctl/tasks/tasks.json"
    ).resolve()

    assert resolve_tag_state_path(project_root, backend="native") == (
        project_root / ".cyclopsctl/tasks/state.json"
    ).resolve()

    assert resolve_complexity_report_path(project_root, backend="native") == (
        project_root / ".cyclopsctl/reports/complexity-report.json"
    ).resolve()


def test_path_resolution_complexity_report_override(project_root: Path):
    custom = resolve_complexity_report_path(
        project_root,
        backend="native",
        override=Path("custom/report.json"),
    )
    assert custom == (project_root / "custom/report.json").resolve()


def test_ensure_native_layout_creates_directories(project_root: Path):
    ensure_native_layout(project_root)
    assert (project_root / ".cyclopsctl/tasks").is_dir()
    assert (project_root / ".cyclopsctl/reports").is_dir()


def test_tasks_document_round_trip(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    payload = {
        "master": {
            "tasks": [
                _sample_task(1),
                _sample_task(2, dependencies=[1]),
            ]
        }
    }
    save_tasks_document(path, payload)
    loaded = load_tasks_document(path)
    assert loaded == payload


def test_tag_tasks_round_trip_merge(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    save_tag_tasks(path, [_sample_task(1)], tag="master")
    save_tag_tasks(path, [_sample_task(10)], tag="phase-5", merge=True)

    document = load_tasks_document(path)
    assert len(document["master"]["tasks"]) == 1
    assert document["phase-5"]["tasks"][0]["id"] == 10


def test_tag_state_round_trip_and_default(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/state.json"
    assert load_tag_state(path).current_tag == "master"

    save_tag_state(path, TagState(current_tag="phase-5", last_switched="2026-06-05T00:00:00+00:00"))
    loaded = load_tag_state(path)
    assert loaded.current_tag == "phase-5"
    assert loaded.last_switched == "2026-06-05T00:00:00+00:00"


def test_set_current_tag_persists_timestamp(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/state.json"
    state = set_current_tag(path, "release")
    assert state.current_tag == "release"
    assert state.last_switched is not None
    assert load_tag_state(path).current_tag == "release"


def test_complexity_report_round_trip(project_root: Path):
    path = project_root / ".cyclopsctl/reports/complexity-report.json"
    payload = {
        "meta": {"generatedAt": "2026-06-05T00:00:00+00:00", "tasksAnalyzed": 1},
        "complexityAnalysis": [
            {
                "taskId": 1,
                "taskTitle": "One",
                "complexityScore": 5,
                "recommendedSubtasks": 0,
                "expansionPrompt": "expand",
                "reasoning": "because",
            }
        ],
    }
    save_complexity_report(path, payload)
    assert load_complexity_report(path) == payload


def test_atomic_write_uses_temp_file(project_root: Path, monkeypatch: pytest.MonkeyPatch):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    observed_tmp: list[Path] = []
    original_replace = Path.replace

    def track_replace(self: Path, target: Path) -> Path:
        if self.suffix == ".tmp":
            observed_tmp.append(self)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", track_replace)
    save_tag_tasks(path, [_sample_task(1)])
    assert path.is_file()
    assert observed_tmp
    assert not observed_tmp[0].exists()


def test_load_tag_tasks_missing_tag_raises(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    save_tag_tasks(path, [_sample_task(1)], tag="master")
    with pytest.raises(TaskStoreNotFoundError, match="tag not found"):
        load_tag_tasks(path, tag="missing")


def test_missing_tasks_document_raises(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    with pytest.raises(TaskStoreNotFoundError, match="not found"):
        load_tasks_document(path)


def test_corrupt_tasks_document_raises(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TaskStoreCorruptError, match="invalid JSON"):
        load_tasks_document(path)


def test_load_migrates_legacy_flat_document(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"tasks": [_sample_task(1)], "metadata": {"v": 1}}),
        encoding="utf-8",
    )
    document = load_tasks_document(path)
    assert set(document) == {"master"}
    assert document["master"]["tasks"][0]["id"] == 1
    assert document["master"]["metadata"] == {"v": 1}


def test_save_tag_tasks_recovers_from_legacy_flat_document(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"tasks": [_sample_task(99, title="agent-written")]}),
        encoding="utf-8",
    )
    save_tag_tasks(path, [_sample_task(1), _sample_task(2)], tag="master", merge=True)
    tasks = load_tag_tasks(path, tag="master")
    assert [task["id"] for task in tasks] == [1, 2]


def test_corrupt_tag_state_raises(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/state.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"currentTag": 1}', encoding="utf-8")
    with pytest.raises(TaskStoreCorruptError, match="currentTag"):
        load_tag_state(path)


def test_corrupt_complexity_report_raises(project_root: Path):
    path = project_root / ".cyclopsctl/reports/complexity-report.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(TaskStoreCorruptError, match="JSON object"):
        load_complexity_report(path)


def test_validate_no_circular_dependencies_accepts_dag():
    tasks = [
        _sample_task(1),
        _sample_task(2, dependencies=[1]),
        _sample_task(3, dependencies=[2]),
    ]
    validate_no_circular_dependencies(tasks)


def test_validate_no_circular_dependencies_rejects_cycle():
    tasks = [
        _sample_task(1, dependencies=[2]),
        _sample_task(2, dependencies=[1]),
    ]
    with pytest.raises(CircularDependencyError, match="circular dependency"):
        validate_no_circular_dependencies(tasks)


def test_save_tasks_document_rejects_circular_dependencies(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    payload = {
        "master": {
            "tasks": [
                _sample_task(1, dependencies=[2]),
                _sample_task(2, dependencies=[1]),
            ]
        }
    }
    with pytest.raises(CircularDependencyError):
        save_tasks_document(path, payload)


def test_task_store_backend_paths_and_round_trip(project_root: Path):
    native = TaskStore(project_root, backend="native")
    native.ensure_native_layout()
    native.save_tag_tasks([_sample_task(1)])
    assert native.load_tag_tasks() == [_sample_task(1)]
    assert native.tasks_path.name == "tasks.json"

    assert native.complexity_report_path().name == "complexity-report.json"


def test_task_store_set_current_tag(project_root: Path):
    store = TaskStore(project_root, backend="native")
    store.ensure_native_layout()
    store.set_current_tag("phase-5")
    assert store.current_tag() == "phase-5"
    assert store.load_tag_state().current_tag == "phase-5"


def test_saved_json_is_pretty_printed(project_root: Path):
    path = project_root / ".cyclopsctl/tasks/tasks.json"
    path.parent.mkdir(parents=True)
    save_tag_tasks(path, [_sample_task(1)])
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw.endswith("\n")
    assert parsed["master"]["tasks"][0]["title"] == "Sample"
