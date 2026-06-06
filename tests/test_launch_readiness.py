"""Tests for task 20: relaxed launch readiness for brownfield attach projects."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.config import LaunchConfig
from cyclopsctl.doctor import remediation_for_check, run_launch_diagnostics
from cyclopsctl.doctor import DiagnosticCheck
from cyclopsctl.launcher import prepare_launch_workspace, run_launch
from cyclopsctl.project_setup import (
    ATTACH_LAUNCH_INFO_MESSAGE as ATTACH_INFO,
    INIT_MISSING_TOML_MESSAGE,
    INIT_REQUIRED_MESSAGE,
    format_init_required_message,
    handover_launch_ready,
    is_brownfield_attach_context,
    is_project_initialized_for_launch,
    project_has_non_empty_tasks,
)
from cyclopsctl.tasks.store import save_tag_tasks, set_current_tag


def _write_native_tasks(
    root: Path,
    *,
    task_ids: tuple[int, ...] = (1, 2),
    tag: str = "master",
) -> None:
    tasks_path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(
        tasks_path,
        [
            {
                "id": task_id,
                "title": f"Task {task_id}",
                "status": "pending",
                "priority": "high",
                "dependencies": [],
                "subtasks": [],
            }
            for task_id in task_ids
        ],
        tag=tag,
        merge=False,
    )
    set_current_tag(tasks_path.parent / "state.json", tag)


def _attach_ready_native_root(tmp_path: Path, *, with_handover: bool = False) -> Path:
    root = tmp_path / "attach-native"
    root.mkdir()
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Sample Service\n\nPython API.\n", encoding="utf-8")
    _write_native_tasks(root)
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}) + "\n",
        encoding="utf-8",
    )
    if with_handover:
        (root / "ai-context.md").write_text("# AI Context\n", encoding="utf-8")
        (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
        (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    return root.resolve()


def _launch_config(root: Path, *, task_backend: str = "native") -> LaunchConfig:
    return LaunchConfig(
        project_root=root,
        first_prompt=root / "prompts" / "first.md",
        current_handover=root / "current-handover-prompt.md",
        update_handover=root / "update-handover-prompt.md",
        complexity_report=root / ".cyclopsctl" / "reports" / "complexity-report.json",
        ai_context=root / "ai-context.md",
        task_backend=task_backend,  # type: ignore[arg-type]
    )


def test_project_has_non_empty_tasks_native_only(tmp_path: Path):
    native_root = tmp_path / "native"
    native_root.mkdir()
    _write_native_tasks(native_root)
    assert project_has_non_empty_tasks(native_root, backend_kind="native") is True


def test_is_project_initialized_attach_ready_without_prd(tmp_path: Path):
    root = _attach_ready_native_root(tmp_path, with_handover=True)
    assert is_brownfield_attach_context(root) is True
    assert is_project_initialized_for_launch(root) is True


def test_is_project_initialized_missing_toml_not_ready(tmp_path: Path):
    root = tmp_path / "no-toml"
    root.mkdir()
    _write_native_tasks(root)
    assert is_project_initialized_for_launch(root) is False
    assert format_init_required_message(root) == INIT_MISSING_TOML_MESSAGE


def test_is_project_initialized_never_ran_init_message(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    assert is_project_initialized_for_launch(root) is False
    assert format_init_required_message(root) == INIT_REQUIRED_MESSAGE


def test_handover_repairable_without_files(tmp_path: Path):
    root = _attach_ready_native_root(tmp_path, with_handover=False)
    assert handover_launch_ready(root) is True
    assert is_project_initialized_for_launch(root) is True


def test_prepare_launch_workspace_repairs_missing_handover(tmp_path: Path):
    root = _attach_ready_native_root(tmp_path, with_handover=False)
    config = _launch_config(root)

    message = prepare_launch_workspace(config)

    assert message is not None
    assert "Synced handover for task 1" in message
    assert config.ai_context.is_file()
    assert config.update_handover.is_file()
    assert config.current_handover.is_file()
    assert "# Task ID: 1" in config.current_handover.read_text(encoding="utf-8")


def test_doctor_attach_remediation_hints(tmp_path: Path):
    root = _attach_ready_native_root(tmp_path, with_handover=False)
    check = DiagnosticCheck(
        name="Update handover",
        passed=False,
        detail=f"Update handover not found: {root / 'update-handover-prompt.md'}",
    )
    remediation = remediation_for_check(check, root)
    assert remediation is not None
    assert "cyclopsctl init --attach --yes" in remediation
    assert "bootstrap" not in remediation


def test_run_launch_attach_ready_without_prd_prompts_cycles(tmp_path: Path, capsys):
    root = _attach_ready_native_root(tmp_path, with_handover=True)
    config = _launch_config(root)

    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather, patch(
        "cyclopsctl.launcher.can_spawn_run",
        return_value=True,
    ):
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": config,
                "checks": [],
                "handover_task_id": 1,
                "next_task": None,
                "pending_count": 2,
                "suggested_cycles": 2,
                "resume_available": False,
                "profile_names": (),
                "default_composer_tier": "standard",
                "default_opus_enabled": True,
                "next_task_error": None,
                "active_tag": "master",
            },
        )()
        dispatch = run_launch(
            config,
            action="run",
            cycles=1,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    assert dispatch.argv is not None
    assert dispatch.exit_code == 0
    assert ATTACH_INFO in captured.err
    assert "Project is not initialized" not in captured.err


def test_run_launch_attach_ready_repairs_then_passes_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    root = _attach_ready_native_root(tmp_path, with_handover=False)
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    config = _launch_config(root)

    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=True,
    ), patch("cyclopsctl.launcher.can_spawn_run", return_value=True):
        dispatch = run_launch(
            config,
            action="run",
            cycles=1,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    checks = run_launch_diagnostics(config, env={"CURSOR_API_KEY": "test-key"})
    assert all(check.passed for check in checks)
    assert dispatch.argv is not None
    assert dispatch.exit_code == 0
    assert "Synced handover for task 1" in captured.err
    assert ATTACH_INFO in captured.err


def test_run_launch_greenfield_with_prd_unchanged(tmp_path: Path, capsys):
    root = tmp_path / "greenfield"
    root.mkdir()
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    (root / "prd.md").write_text("# PRD: Greenfield\n", encoding="utf-8")
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    _write_native_tasks(root)
    (root / "ai-context.md").write_text("# AI Context\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}) + "\n",
        encoding="utf-8",
    )
    config = _launch_config(root)

    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather, patch(
        "cyclopsctl.launcher.can_spawn_run",
        return_value=True,
    ):
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": config,
                "checks": [],
                "handover_task_id": 1,
                "next_task": None,
                "pending_count": 2,
                "suggested_cycles": 2,
                "resume_available": False,
                "profile_names": (),
                "default_composer_tier": "standard",
                "default_opus_enabled": True,
                "next_task_error": None,
                "active_tag": "master",
            },
        )()
        dispatch = run_launch(
            config,
            action="run",
            cycles=1,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    assert dispatch.argv is not None
    assert ATTACH_INFO not in captured.err


def test_run_launch_missing_toml_with_tasks(tmp_path: Path, capsys):
    root = tmp_path / "tasks-no-toml"
    root.mkdir()
    _write_native_tasks(root)
    config = _launch_config(root)

    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": config,
                "checks": [],
                "handover_task_id": None,
                "next_task": None,
                "pending_count": 2,
                "suggested_cycles": 2,
                "resume_available": False,
                "profile_names": (),
                "default_composer_tier": "standard",
                "default_opus_enabled": True,
                "next_task_error": None,
                "active_tag": "master",
            },
        )()
        dispatch = run_launch(
            config,
            action="run",
            cycles=1,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    assert dispatch.argv is None
    assert dispatch.exit_code == 1
    assert INIT_MISSING_TOML_MESSAGE in captured.err

