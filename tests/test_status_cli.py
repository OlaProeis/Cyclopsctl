"""CLI tests for task 9: cyclopsctl status command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.cli import main
from cyclopsctl.state import RunState, RunStateStatus, write_state


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root.resolve()


def _state_file(project_root: Path) -> Path:
    return project_root / ".cyclopsctl" / "state.json"


def _write_sample_state(project_root: Path) -> Path:
    path = _state_file(project_root)
    state = RunState(
        cycle_number=1,
        total_cycles=2,
        phase="implementation",
        task_id=9,
        task_title="Crash recovery",
        agent_id="agent-cli",
        run_id="run-cli",
        last_event="cycle started",
        status=RunStateStatus.RUNNING,
        updated_at="2026-06-05T12:00:00+00:00",
        project_root=str(project_root),
    )
    write_state(path, state)
    return path


def test_status_defaults_to_cwd(project_root: Path, monkeypatch, capsys):
    monkeypatch.chdir(project_root)
    code = main(["status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "No cyclopsctl state file found" in captured.out


def test_status_missing_state_exits_zero(project_root: Path, capsys):
    code = main(["status", "--project-root", str(project_root)])
    captured = capsys.readouterr()
    assert code == 0
    assert "No cyclopsctl state file found" in captured.out
    assert "No cyclopsctl run history file found" in captured.out


def test_status_present_state_exits_zero(project_root: Path, capsys):
    _write_sample_state(project_root)
    code = main(["status", "--project-root", str(project_root)])
    captured = capsys.readouterr()
    assert code == 0
    assert "Cyclopsctl run state" in captured.out
    assert "Task: 9" in captured.out
    assert "agent-cli" in captured.out
    assert "Cyclopsctl run history" in captured.out or "No cyclopsctl run history" in captured.out


def test_status_corrupt_state_exits_zero(project_root: Path, capsys):
    path = _state_file(project_root)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    code = main(["status", "--project-root", str(project_root)])
    captured = capsys.readouterr()
    assert code == 0
    assert "invalid JSON" in captured.out


def test_status_custom_state_file(project_root: Path, capsys):
    custom = project_root / "custom" / "run-state.json"
    state = RunState(
        cycle_number=3,
        total_cycles=3,
        phase="complete",
        task_id=1,
        task_title="Done",
        agent_id=None,
        run_id=None,
        last_event="verification passed",
        status=RunStateStatus.COMPLETED,
        updated_at="2026-06-05T12:00:00+00:00",
        project_root=str(project_root),
    )
    write_state(custom, state)

    code = main(
        [
            "status",
            "--project-root",
            str(project_root),
            "--state-file",
            str(custom),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Cycle: 3/3" in captured.out


def test_status_disabled_via_empty_config(project_root: Path, tmp_path: Path, capsys):
    toml = tmp_path / "cyclopsctl.toml"
    toml.write_text(
        f'project_root = "{project_root.as_posix()}"\nstate_file = ""\n',
        encoding="utf-8",
    )
    code = main(["status", "--config", str(toml)])
    captured = capsys.readouterr()
    assert code == 0
    assert "disabled" in captured.out.lower()
