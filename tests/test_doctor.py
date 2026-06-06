"""Tests for task 5: cyclopsctl doctor diagnostics command."""

from __future__ import annotations

import shutil
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from cyclopsctl.cli import main
from cyclopsctl.config import DoctorConfig, load_doctor_config
from cyclopsctl.doctor import (
    DiagnosticCheck,
    apply_safe_fixes,
    check_active_tag_mismatch,
    check_api_key,
    check_complexity_report_readable,
    check_handover_drift,
    check_handover_in_queue,
    check_handover_task_id,
    check_handover_task_status,
    check_native_tasks_directory,
    check_sdk_bridge,
    check_stale_workflow_files,
    check_tasks_next,
    enrich_checks_with_remediation,
    exit_code_for_checks,
    format_diagnostics_plain,
    render_diagnostics_rich,
    run_brownfield_readiness_checks,
    run_diagnostics,
    run_doctor,
    run_launch_diagnostics,
)
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult, TaskShowDetail
from cyclopsctl.errors import GENERAL_EXIT_CODE, STARTUP_EXIT_CODE


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 5\n\nImplement doctor.\n",
        encoding="utf-8",
    )
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 5, "complexityScore": 7}]}',
        encoding="utf-8",
    )
    return root.resolve()


def _native_doctor_config(root: Path, **overrides) -> DoctorConfig:
    defaults = {
        "project_root": root,
        "current_handover": root / "current-handover-prompt.md",
        "complexity_report": root / ".cyclopsctl" / "reports" / "complexity-report.json",
    }
    defaults.update(overrides)
    return DoctorConfig(**defaults)


def _doctor_config(root: Path, **overrides) -> DoctorConfig:
    return _native_doctor_config(root, **overrides)


def _enriched(check: DiagnosticCheck, root: Path) -> DiagnosticCheck:
    return enrich_checks_with_remediation([check], root)[0]


def _render_rich_text(checks: list[DiagnosticCheck]) -> str:
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=120)
    console.print(render_diagnostics_rich(checks))
    return buffer.getvalue()


def _doctor_argv(root: Path) -> list[str]:
    return [
        "doctor",
        "--project-root",
        str(root),
    ]


def _passing_checks() -> list[DiagnosticCheck]:
    return [
        DiagnosticCheck("CURSOR_API_KEY", True, "ok"),
        DiagnosticCheck("Cursor SDK bridge", True, "ok"),
        DiagnosticCheck("Handover Task ID", True, "ok"),
        DiagnosticCheck("Complexity report", True, "ok"),
        DiagnosticCheck("Backend next", True, "ok"),
    ]


def test_check_api_key_pass():
    result = check_api_key(env={"CURSOR_API_KEY": "secret"})
    assert result.passed is True
    assert "CURSOR_API_KEY is set" in result.detail


def test_check_api_key_fail():
    result = check_api_key(env={})
    assert result.passed is False
    assert "CURSOR_API_KEY" in result.detail


def test_check_native_tasks_directory_greenfield(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    result = check_native_tasks_directory(root)
    assert result.passed is True
    assert result.informational is True
    assert "greenfield" in result.detail.lower()


def test_check_native_tasks_directory_pass(project_tree: Path):
    native_tasks = project_tree / ".cyclopsctl" / "tasks" / "tasks.json"
    native_tasks.parent.mkdir(parents=True, exist_ok=True)
    native_tasks.write_text(
        '{"master": {"tasks": [{"id": 5, "title": "Doctor task", "status": "pending"}]}}',
        encoding="utf-8",
    )
    result = check_native_tasks_directory(project_tree)
    assert result.passed is True
    assert "Readable native tasks" in result.detail


def test_check_native_tasks_directory_invalid_json(project_tree: Path):
    native_tasks = project_tree / ".cyclopsctl" / "tasks" / "tasks.json"
    native_tasks.parent.mkdir(parents=True, exist_ok=True)
    native_tasks.write_text("{not json", encoding="utf-8")
    result = check_native_tasks_directory(project_tree)
    assert result.passed is False
    assert "invalid json" in result.detail.lower()


def test_run_diagnostics_native_mode_minimal_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        checks = run_diagnostics(_native_doctor_config(root))

    check_names = [check.name for check in checks]
    assert check_names == ["CURSOR_API_KEY", "Cursor SDK bridge", "Native tasks"]
    assert all(check.passed for check in checks)


def test_run_diagnostics_native_includes_complexity_when_tasks_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    root.mkdir()
    tasks_json = root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_json.parent.mkdir(parents=True, exist_ok=True)
    tasks_json.write_text('{"master": {"tasks": [{"id": 1, "title": "T", "status": "pending"}]}}', encoding="utf-8")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"complexityAnalysis": [{"taskId": 1, "complexityScore": 4}]}', encoding="utf-8")

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        checks = run_diagnostics(_native_doctor_config(root))

    check_names = [check.name for check in checks]
    assert "Complexity report" in check_names
    assert "Handover Task ID" not in check_names
    assert "Backend next" not in check_names
    assert all(check.passed for check in checks)


def test_check_handover_task_id_pass(project_tree: Path):
    result = check_handover_task_id(project_tree / "current-handover-prompt.md")
    assert result.passed is True
    assert "Task ID 5" in result.detail


def test_check_handover_task_id_missing_file(project_tree: Path):
    result = check_handover_task_id(project_tree / "missing.md")
    assert result.passed is False
    assert "not found" in result.detail


def test_check_handover_task_id_rejects_placeholder_zero(project_tree: Path):
    path = project_tree / "current-handover-prompt.md"
    path.write_text("# Task ID: 0\n\nPlaceholder.\n", encoding="utf-8")
    result = check_handover_task_id(path)
    assert result.passed is False
    assert "Task ID 0" in result.detail


def test_check_handover_task_id_invalid_marker(project_tree: Path):
    path = project_tree / "bad-handover.md"
    path.write_text("# No task id here\n", encoding="utf-8")
    result = check_handover_task_id(path)
    assert result.passed is False
    assert "Task ID" in result.detail


def test_check_complexity_report_readable_pass(project_tree: Path):
    report = project_tree / ".cyclopsctl" / "reports" / "complexity-report.json"
    result = check_complexity_report_readable(report)
    assert result.passed is True
    assert "Readable JSON" in result.detail


def test_check_complexity_report_missing(project_tree: Path):
    result = check_complexity_report_readable(project_tree / "missing.json")
    assert result.passed is False
    assert "not found" in result.detail


def test_check_complexity_report_invalid_json(project_tree: Path):
    report = project_tree / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.write_text("{not json", encoding="utf-8")
    result = check_complexity_report_readable(report)
    assert result.passed is False
    assert "Invalid JSON" in result.detail


def test_check_tasks_next_found(project_tree: Path):
    from cyclopsctl.tasks.backend import TaskBackendConfig, get_task_backend

    tasks_json = project_tree / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_json.parent.mkdir(parents=True, exist_ok=True)
    tasks_json.write_text(
        '{"master": {"tasks": [{"id": 5, "title": "Doctor task", "status": "pending", "dependencies": []}]}}',
        encoding="utf-8",
    )
    backend = get_task_backend(TaskBackendConfig())
    result = check_tasks_next(project_tree, backend=backend)
    assert result.passed is True
    assert "Next task #5" in result.detail
    assert result.informational is False


def test_check_tasks_next_empty_queue(project_tree: Path):
    from cyclopsctl.tasks.backend import TaskBackendConfig, get_task_backend

    tasks_json = project_tree / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_json.parent.mkdir(parents=True, exist_ok=True)
    tasks_json.write_text('{"phase-2": {"tasks": []}}', encoding="utf-8")
    (tasks_json.parent / "state.json").write_text(
        '{"currentTag": "phase-2"}', encoding="utf-8"
    )
    backend = get_task_backend(TaskBackendConfig())
    result = check_tasks_next(project_tree, tag="phase-2", backend=backend)
    assert result.passed is True
    assert result.informational is True
    assert "queue is empty" in result.detail
    assert "phase-2" in result.detail


def test_check_tasks_next_failure(project_tree: Path):
    class BrokenBackend:
        def get_next(self, project_root, *, tag=None):
            raise RuntimeError("database locked")

    result = check_tasks_next(project_tree, backend=BrokenBackend())
    assert result.passed is False
    assert "database locked" in result.detail


def test_check_sdk_bridge_skips_when_not_needed(project_tree: Path):
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        result = check_sdk_bridge(project_tree)
    assert result.passed is True
    assert "not required" in result.detail


def test_check_sdk_bridge_env_configured(project_tree: Path):
    with patch("cyclopsctl.doctor.bridge_env_configured", return_value=True):
        result = check_sdk_bridge(project_tree)
    assert result.passed is True
    assert "environment" in result.detail


def test_exit_code_for_checks_all_pass():
    assert exit_code_for_checks(_passing_checks()) == 0


def test_exit_code_for_checks_startup_failure():
    checks = _passing_checks()
    checks[0] = DiagnosticCheck("CURSOR_API_KEY", False, "missing")
    assert exit_code_for_checks(checks) == STARTUP_EXIT_CODE


def test_exit_code_for_checks_general_failure():
    checks = _passing_checks()
    checks[3] = DiagnosticCheck("Handover Task ID", False, "bad")
    assert exit_code_for_checks(checks) == GENERAL_EXIT_CODE


def test_format_diagnostics_plain_empty_queue_note():
    checks = _passing_checks()
    checks[-1] = DiagnosticCheck(
        "Backend next",
        True,
        "Task queue is empty",
        informational=True,
    )
    text = format_diagnostics_plain(checks)
    assert "[NOTE] Backend next" in text
    assert "All checks passed." in text


def test_run_doctor_all_pass(project_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.run_diagnostics", return_value=_passing_checks()):
        code = run_doctor(_doctor_config(project_tree, plain=True), stderr_is_tty=False)

    captured = capsys.readouterr()
    assert code == 0
    assert "All checks passed." in captured.err


def test_run_doctor_reports_failures(project_tree: Path, capsys):
    checks = _passing_checks()
    checks[3] = DiagnosticCheck("Handover Task ID", False, "missing marker")
    with patch("cyclopsctl.doctor.run_diagnostics", return_value=checks):
        code = run_doctor(_doctor_config(project_tree, plain=True), stderr_is_tty=False)

    captured = capsys.readouterr()
    assert code == GENERAL_EXIT_CODE
    assert "[FAIL] Handover Task ID" in captured.err
    assert "1 check(s) failed." in captured.err


def test_load_doctor_config_from_cli(project_tree: Path):
    config = load_doctor_config(project_root=project_tree)
    assert config.project_root == project_tree
    assert config.current_handover == (project_tree / "current-handover-prompt.md").resolve()


def test_cli_doctor_delegates(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.cli.run_doctor", return_value=0) as mock_doctor:
        code = main(_doctor_argv(project_tree))

    assert code == 0
    mock_doctor.assert_called_once()
    config = mock_doctor.call_args.args[0]
    assert config.project_root == project_tree


def test_cli_check_alias(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.cli.run_doctor", return_value=0) as mock_doctor:
        code = main(["check", "--project-root", str(project_tree)])

    assert code == 0
    mock_doctor.assert_called_once()


def test_cli_doctor_plumbs_flags(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    handover = project_tree / "current-handover-prompt.md"
    report = project_tree / ".cyclopsctl" / "reports" / "complexity-report.json"
    with patch("cyclopsctl.cli.run_doctor", return_value=0) as mock_doctor:
        code = main(
            [
                "doctor",
                "--project-root",
                str(project_tree),
                "--current-handover",
                str(handover),
                "--complexity-report",
                str(report),
                "--tag",
                "phase-2",
                "--plain",
            ]
        )

    assert code == 0
    config = mock_doctor.call_args.args[0]
    assert config.current_handover == handover.resolve()
    assert config.complexity_report == report.resolve()
    assert config.tag == "phase-2"
    assert config.plain is True


def test_cli_doctor_loads_env_before_checks(
    project_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (project_tree / ".env").write_text("CURSOR_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    call_order: list[str] = []

    def track_env(*args, **kwargs):
        call_order.append("env")
        from cyclopsctl.env import load_project_env as real_load

        return real_load(*args, **kwargs)

    def track_doctor(config, **kwargs):
        call_order.append("doctor")
        assert __import__("os").environ.get("CURSOR_API_KEY") == "from-dotenv"
        return 0

    with patch("cyclopsctl.cli.load_project_env", side_effect=track_env), patch(
        "cyclopsctl.cli.run_doctor",
        side_effect=track_doctor,
    ):
        code = main(_doctor_argv(project_tree))

    assert code == 0
    assert call_order == ["env", "doctor"]


def test_run_diagnostics_integration(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    tasks_json = project_tree / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_json.parent.mkdir(parents=True, exist_ok=True)
    tasks_json.write_text(
        '{"master": {"tasks": [{"id": 5, "title": "Doctor task", "status": "pending", "dependencies": []}]}}',
        encoding="utf-8",
    )

    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        checks = run_diagnostics(_doctor_config(project_tree))

    assert all(check.passed for check in checks)
    assert any("task 5" in check.detail.lower() for check in checks)


@pytest.mark.parametrize(
    ("check_factory", "expected_substring"),
    [
        (lambda root: check_api_key(env={}), "cyclopsctl init"),
        (
            lambda root: check_handover_task_id(root / "missing-handover.md"),
            "bootstrap",
        ),
        (
            lambda root: check_handover_task_id(_write_invalid_handover(root)),
            "Task ID",
        ),
        (
            lambda root: check_complexity_report_readable(root / "missing-report.json"),
            "cyclopsctl bootstrap",
        ),
    ],
)
def test_failure_remediation_plain_and_rich(
    project_tree: Path,
    check_factory,
    expected_substring: str,
):
    check = _enriched(check_factory(project_tree), project_tree)
    assert check.remediation is not None
    assert expected_substring in check.remediation

    plain = format_diagnostics_plain([check])
    rich = _render_rich_text([check])
    assert "Remediation:" in plain
    assert expected_substring in plain
    assert "Remediation:" in rich
    assert expected_substring in rich


def _write_invalid_handover(root: Path) -> Path:
    path = root / "bad-handover.md"
    path.write_text("# No task id here\n", encoding="utf-8")
    return path


def test_empty_queue_remediation_with_native_tasks(project_tree: Path):
    check = DiagnosticCheck(
        "Backend next",
        True,
        "Task queue is empty",
        informational=True,
    )
    enriched = _enriched(check, project_tree)
    assert enriched.remediation is not None
    assert "parse-prd" in enriched.remediation

    plain = format_diagnostics_plain([enriched])
    rich = _render_rich_text([enriched])
    assert "Remediation:" in plain
    assert "Remediation:" in rich


def test_empty_queue_remediation_greenfield(tmp_path: Path):
    root = tmp_path / "greenfield"
    root.mkdir()
    check = DiagnosticCheck(
        "Backend next",
        True,
        "Task queue is empty",
        informational=True,
    )
    enriched = _enriched(check, root)
    assert enriched.remediation is not None
    assert "cyclopsctl bootstrap" in enriched.remediation


def test_handover_remediation_greenfield(tmp_path: Path):
    root = tmp_path / "greenfield"
    root.mkdir()
    check = check_handover_task_id(root / "missing.md")
    enriched = _enriched(check, root)
    assert enriched.remediation is not None
    assert "cyclopsctl init" in enriched.remediation


def test_complexity_report_remediation_greenfield(tmp_path: Path):
    root = tmp_path / "greenfield"
    root.mkdir()
    check = check_complexity_report_readable(root / "missing.json")
    enriched = _enriched(check, root)
    assert enriched.remediation is not None
    assert "cyclopsctl bootstrap" in enriched.remediation


def test_apply_safe_fixes_creates_env_stub(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    config = _doctor_config(root)
    checks = [DiagnosticCheck("CURSOR_API_KEY", False, "missing key")]
    actions = apply_safe_fixes(checks, config)
    env_path = root / ".env"

    assert len(actions) == 1
    assert env_path.is_file()
    content = env_path.read_text(encoding="utf-8")
    assert "CURSOR_API_KEY=" in content
    assert "ANTHROPIC_API_KEY=" not in content
    assert "PERPLEXITY_API_KEY=" not in content
    assert "Cyclopsctl" in content


def test_apply_safe_fixes_skips_existing_env(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("CURSOR_API_KEY=existing\n", encoding="utf-8")
    config = _doctor_config(root)
    checks = [DiagnosticCheck("CURSOR_API_KEY", False, "missing key")]
    actions = apply_safe_fixes(checks, config)

    assert actions == []
    assert (root / ".env").read_text(encoding="utf-8") == "CURSOR_API_KEY=existing\n"


def test_run_doctor_fix_does_not_invoke_subprocess(tmp_path: Path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": []}',
        encoding="utf-8",
    )

    def runner(_cmd):
        class Completed:
            returncode = 0
            stdout = '{"found": true, "task": {"id": "1", "title": "Task"}}'
            stderr = ""

        return Completed()

    with patch("subprocess.run") as mock_run, patch(
        "cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False
    ), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        code = run_doctor(
            _doctor_config(root, fix=True, plain=True),
            env={},
            runner=runner,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    mock_run.assert_not_called()
    assert (root / ".env").is_file()
    assert "Applied safe fixes:" in captured.err
    assert code == STARTUP_EXIT_CODE


def test_cli_doctor_plumbs_fix_flag(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.cli.run_doctor", return_value=0) as mock_doctor:
        code = main(_doctor_argv(project_tree) + ["--fix"])

    assert code == 0
    config = mock_doctor.call_args.args[0]
    assert config.fix is True


class _MockBrownfieldBackend:
    def __init__(
        self,
        *,
        next_task_id: str = "6",
        next_title: str = "Next task",
        handover_status: str = "pending",
        handover_exists: bool = True,
        pending_count: int = 3,
        active_tag: str = "master",
    ) -> None:
        self.next_task_id = next_task_id
        self.next_title = next_title
        self.handover_status = handover_status
        self.handover_exists = handover_exists
        self.pending_count = pending_count
        self.active_tag = active_tag

    def get_next(self, _project_root, *, tag=None):
        return NextTaskLookup.from_task(
            NextTaskResult(
                task_id=self.next_task_id,
                title=self.next_title,
                status="pending",
                priority=None,
                complexity=None,
                tag=tag or self.active_tag,
            )
        )

    def show(self, _project_root, task_id, *, tag=None):
        if not self.handover_exists:
            raise RuntimeError(f"task not found: {task_id}")
        return TaskShowDetail(
            task_id=task_id,
            title="Handover task",
            description=None,
            details=None,
            test_strategy=None,
            priority=None,
            dependencies=(),
            status=self.handover_status,
            complexity=None,
        )

    def list_pending(self, _project_root, *, tag=None):
        return [
            NextTaskResult(
                task_id=self.next_task_id,
                title=self.next_title,
                status="pending",
                priority=None,
                complexity=None,
                tag=tag or self.active_tag,
            )
            for _ in range(self.pending_count)
        ]

    def current_tag(self, _project_root):
        return self.active_tag


def _native_tasks_file(root: Path) -> None:
    tasks_json = root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_json.parent.mkdir(parents=True, exist_ok=True)
    tasks_json.write_text(
        '{"master": {"tasks": [{"id": 6, "title": "Task 6", "status": "pending"}]}}',
        encoding="utf-8",
    )


def test_check_handover_drift_warns_on_mismatch(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    handover = root / "current-handover-prompt.md"
    handover.write_text("# Task ID: 9\n", encoding="utf-8")
    backend = _MockBrownfieldBackend(next_task_id="6")

    result = check_handover_drift(handover, root, backend)

    assert result.passed is True
    assert result.informational is True
    assert "differs from backend.get_next()" in result.detail


def test_check_handover_drift_passes_when_aligned(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    handover = root / "current-handover-prompt.md"
    handover.write_text("# Task ID: 6\n", encoding="utf-8")
    backend = _MockBrownfieldBackend(next_task_id="6")

    result = check_handover_drift(handover, root, backend)

    assert result.passed is True
    assert result.informational is False
    assert "matches backend.get_next()" in result.detail


def test_check_handover_in_queue_warns_when_missing(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    handover = root / "current-handover-prompt.md"
    handover.write_text("# Task ID: 9\n", encoding="utf-8")
    backend = _MockBrownfieldBackend(handover_exists=False)

    result = check_handover_in_queue(handover, root, backend)

    assert result is not None
    assert result.passed is True
    assert result.informational is True
    assert "not found in queue" in result.detail


def test_check_handover_task_status_warns_when_done_with_pending(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    handover = root / "current-handover-prompt.md"
    handover.write_text("# Task ID: 5\n", encoding="utf-8")
    backend = _MockBrownfieldBackend(handover_status="done", pending_count=2)

    result = check_handover_task_status(handover, root, backend)

    assert result is not None
    assert result.passed is True
    assert result.informational is True
    assert "is done but" in result.detail


def test_check_stale_workflow_files_warns_on_stale_ai_context(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ai-context.md").write_text("# Custom\n\nNo cyclopsctl tasks references.\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("Native path.\n", encoding="utf-8")

    result = check_stale_workflow_files(root)

    assert result.passed is True
    assert result.informational is True
    assert "ai-context.md" in result.detail
    assert "upgrade" in result.detail.lower()


def test_check_stale_workflow_files_remediation_suggests_refresh_workflow(
    tmp_path: Path,
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "update-handover-prompt.md").write_text(
        "Mark tasks done manually.\n",
        encoding="utf-8",
    )

    result = check_stale_workflow_files(root)
    enriched = enrich_checks_with_remediation([result], root)[0]

    assert enriched.remediation is not None
    assert "--refresh-workflow" in enriched.remediation


def test_check_active_tag_mismatch_warns(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    backend = _MockBrownfieldBackend(active_tag="master")

    result = check_active_tag_mismatch(root, backend, config_tag="phase-2")

    assert result is not None
    assert result.passed is True
    assert result.informational is True
    assert "differs from active tag" in result.detail


def test_check_active_tag_mismatch_skipped_without_config_tag(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    backend = _MockBrownfieldBackend()

    assert check_active_tag_mismatch(root, backend, config_tag=None) is None


def test_check_complexity_report_warn_missing_is_informational(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    result = check_complexity_report_readable(root / "missing.json", warn_missing=True)

    assert result.passed is True
    assert result.informational is True
    assert "not found" in result.detail


def test_run_diagnostics_native_includes_brownfield_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    root.mkdir()
    _native_tasks_file(root)
    (root / "current-handover-prompt.md").write_text("# Task ID: 6\n", encoding="utf-8")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"complexityAnalysis": [{"taskId": 6, "complexityScore": 4}]}', encoding="utf-8")

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        checks = run_diagnostics(_native_doctor_config(root))

    check_names = [check.name for check in checks]
    assert "Handover drift" in check_names
    assert "Stale workflow" in check_names
    assert all(check.passed for check in checks)


def test_run_launch_diagnostics_native_uses_backend_not_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cyclopsctl.config import LaunchConfig

    root = tmp_path / "repo"
    root.mkdir()
    _native_tasks_file(root)
    (root / "current-handover-prompt.md").write_text("# Task ID: 6\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / "ai-context.md").write_text("# AI\n", encoding="utf-8")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"complexityAnalysis": [{"taskId": 6, "complexityScore": 4}]}', encoding="utf-8")

    config = LaunchConfig(
        project_root=root,
        first_prompt=root / "prompts" / "first.md",
        current_handover=root / "current-handover-prompt.md",
        update_handover=root / "update-handover-prompt.md",
        complexity_report=report,
        ai_context=root / "ai-context.md",
        task_backend="native",
    )

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ), patch("cyclopsctl.doctor.check_workspace_dirty", return_value=None), patch(
        "subprocess.run"
    ) as mock_run:
        checks = run_launch_diagnostics(config, env={"CURSOR_API_KEY": "test-key"})

    mock_run.assert_not_called()
    check_names = [check.name for check in checks]
    assert "Backend next" in check_names
    assert "Handover drift" in check_names
    assert all(check.passed for check in checks)


def test_brownfield_fixture_stale_workflow_detection(tmp_path: Path):
    fixture_root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "brownfield-mid-backlog"
    repo = tmp_path / "stale"
    shutil.copytree(fixture_root, repo)
    (repo / "update-handover-prompt.md").write_text(
        "Mark tasks done manually.\n",
        encoding="utf-8",
    )

    backend = _MockBrownfieldBackend()
    checks = run_brownfield_readiness_checks(
        repo,
        backend,
        current_handover=repo / "current-handover-prompt.md",
        update_handover=repo / "update-handover-prompt.md",
        ai_context=repo / "ai-context.md",
    )
    stale = next(check for check in checks if check.name == "Stale workflow")
    assert stale.informational is True
    assert "update-handover-prompt.md" in stale.detail


def test_brownfield_fixture_tag_mismatch_warning(tmp_path: Path):
    fixture_root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "brownfield-mid-backlog"
    repo = tmp_path / "tag-mismatch"
    shutil.copytree(fixture_root, repo)
    backend = _MockBrownfieldBackend(active_tag="master")

    checks = run_brownfield_readiness_checks(
        repo,
        backend,
        current_handover=repo / "current-handover-prompt.md",
        config_tag="phase-5",
    )
    tag_check = next(check for check in checks if check.name == "Active tag")
    assert tag_check.informational is True
    assert "phase-5" in tag_check.detail
