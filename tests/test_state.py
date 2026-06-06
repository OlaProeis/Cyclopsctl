"""Tests for task 9: crash recovery state file persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.config import build_run_config, build_status_config, load_status_config
from cyclopsctl.state import (
    RunState,
    RunStateStatus,
    RunStateTracker,
    clear_state,
    default_state_path,
    format_state_summary,
    read_state,
    resolve_state_path,
    write_atomic,
    write_state,
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root.resolve()


def _sample_state(project_root: Path) -> RunState:
    return RunState(
        cycle_number=2,
        total_cycles=5,
        phase="implementation",
        task_id=9,
        task_title="Add crash recovery state file",
        agent_id="agent-1",
        run_id="run-1",
        last_event="cycle started",
        status=RunStateStatus.RUNNING,
        updated_at="2026-06-05T12:00:00+00:00",
        project_root=str(project_root),
    )


def test_default_state_path(project_root: Path):
    path = default_state_path(project_root)
    assert path == (project_root / ".cyclopsctl" / "state.json").resolve()


def test_resolve_state_path_default_and_custom(project_root: Path):
    assert resolve_state_path(None, project_root) == default_state_path(project_root)
    custom = resolve_state_path(Path("custom/state.json"), project_root)
    assert custom == (project_root / "custom" / "state.json").resolve()


def test_write_atomic_creates_file(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    write_atomic(path, '{"ok": true}')
    assert path.read_text(encoding="utf-8") == '{"ok": true}'


def test_write_read_clear_lifecycle(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    state = _sample_state(project_root)
    write_state(path, state)

    result = read_state(path)
    assert result.kind == "ok"
    assert result.state == state

    clear_state(path)
    missing = read_state(path)
    assert missing.kind == "missing"


def test_tracker_persist_and_mark_completed(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    tracker = RunStateTracker(path, project_root=project_root)
    tracker.persist(
        cycle_number=1,
        total_cycles=3,
        phase="resolve",
        last_event="task selected",
        task_id=9,
        task_title="State file",
    )
    assert path.is_file()

    tracker.mark_completed()
    assert not path.is_file()


def test_tracker_mark_interrupted(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    tracker = RunStateTracker(path, project_root=project_root)
    tracker.persist(
        cycle_number=2,
        total_cycles=4,
        phase="update",
        last_event="update running",
        task_id=9,
        task_title="State file",
        agent_id="agent-9",
        run_id="run-9",
    )
    tracker.mark_interrupted(
        cycle_number=2,
        phase="update",
        agent_id="agent-9",
        run_id="run-9",
    )

    result = read_state(path)
    assert result.kind == "ok"
    assert result.state is not None
    assert result.state.status is RunStateStatus.INTERRUPTED
    assert result.state.phase == "update"
    assert result.state.last_event == "run interrupted by user"


def test_tracker_disabled_when_path_none(project_root: Path):
    tracker = RunStateTracker(None, project_root=project_root)
    assert tracker.enabled is False
    tracker.persist(
        cycle_number=1,
        total_cycles=1,
        phase="idle",
        last_event="noop",
    )
    assert not (project_root / ".cyclopsctl" / "state.json").exists()


def test_read_state_corrupt_json(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    result = read_state(path)
    assert result.kind == "corrupt"
    assert "invalid JSON" in (result.message or "")


def test_read_state_invalid_schema(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["array"]), encoding="utf-8")

    result = read_state(path)
    assert result.kind == "corrupt"
    assert "JSON object" in (result.message or "")


def test_format_state_summary_variants(project_root: Path):
    path = project_root / ".cyclopsctl" / "state.json"
    missing = read_state(path)
    assert "No cyclopsctl state file found" in format_state_summary(missing)

    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    corrupt = read_state(path)
    summary = format_state_summary(corrupt)
    assert "invalid JSON" in summary
    assert str(path) in summary

    write_state(path, _sample_state(project_root))
    ok = read_state(path)
    summary = format_state_summary(ok)
    assert "Cyclopsctl run state" in summary
    assert "Cycle: 2/5" in summary
    assert "Task: 9" in summary
    assert "running" in summary


def test_build_run_config_state_file_default_and_disable(project_root: Path):
    base = {
        "cycles": 1,
        "project_root": project_root,
        "first_prompt": project_root / "first.md",
        "current_handover": project_root / "current.md",
        "update_handover": project_root / "update.md",
        "complexity_report": project_root / "report.json",
    }
    (project_root / "first.md").write_text("x", encoding="utf-8")
    (project_root / "current.md").write_text("x", encoding="utf-8")
    (project_root / "update.md").write_text("x", encoding="utf-8")
    (project_root / "report.json").write_text("{}", encoding="utf-8")

    cfg = build_run_config(base)
    assert cfg.state_file == default_state_path(project_root)

    disabled = build_run_config({**base, "state_file": ""})
    assert disabled.state_file is None


def test_load_status_config(project_root: Path):
    cfg = load_status_config(project_root=project_root)
    assert cfg.project_root == project_root
    assert cfg.state_file == default_state_path(project_root)


def test_build_status_config_custom_state_file(project_root: Path):
    cfg = build_status_config(
        {"project_root": project_root, "state_file": Path("alt/state.json")}
    )
    assert cfg.state_file == (project_root / "alt" / "state.json").resolve()
