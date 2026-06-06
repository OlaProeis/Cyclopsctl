"""Tests for task 14: interactive TUI launcher for run setup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.cli import _normalize_argv, main
from cyclopsctl.config import LaunchConfig, load_launch_config
from cyclopsctl.doctor import (
    DiagnosticCheck,
    check_ai_context,
    check_tasks_pending_list,
    check_update_handover,
    run_launch_diagnostics,
)
from cyclopsctl.launcher import (
    BootstrapChoices,
    LAUNCH_OPUS_PROMPT,
    LaunchAction,
    LaunchChoices,
    _format_cycles_confirm_message,
    build_bootstrap_argv,
    build_doctor_argv,
    build_models_argv,
    build_run_argv,
    can_spawn_run,
    format_launch_summary_line,
    format_non_tty_message,
    gather_launch_status,
    infer_launch_defaults,
    maybe_repair_handover_at_launch,
    prompt_bootstrap_choices,
    prompt_launch_action,
    prompt_launch_choices,
    resolve_launch_choices,
    run_launch,
    suggest_cycles,
)
from cyclopsctl.tui import format_launch_overview_plain
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    from cyclopsctl.tasks.store import save_tag_tasks, set_current_tag

    root = tmp_path / "repo"
    root.mkdir()
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    (root / "prd.md").write_text("# PRD: Launcher tests\n", encoding="utf-8")
    (root / "prompts").mkdir()
    (root / "prompts" / "first.md").write_text("# First\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 14\n\nImplement launcher.\n",
        encoding="utf-8",
    )
    (root / "update-handover-prompt.md").write_text(
        "# Update\n\ncyclopsctl tasks set-status --id=14 --status=done\n",
        encoding="utf-8",
    )
    (root / "ai-context.md").write_text(
        "# AI context\n\n## Implementation Phase Rules\n## Update Phase Rules\n"
        "Use cyclopsctl tasks CLI only.\n",
        encoding="utf-8",
    )
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        '{"complexityAnalysis": [{"taskId": 14, "complexityScore": 8}]}',
        encoding="utf-8",
    )
    tasks_path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(
        tasks_path,
        [
            {
                "id": 14,
                "title": "Launcher task",
                "status": "pending",
                "priority": "high",
                "dependencies": [],
                "subtasks": [],
            }
        ],
        tag="master",
        merge=False,
    )
    set_current_tag(tasks_path.parent / "state.json", "master")
    return root.resolve()


def _launch_config(root: Path, **overrides) -> LaunchConfig:
    defaults = {
        "project_root": root,
        "first_prompt": root / "prompts" / "first.md",
        "current_handover": root / "current-handover-prompt.md",
        "update_handover": root / "update-handover-prompt.md",
        "complexity_report": root / ".cyclopsctl" / "reports" / "complexity-report.json",
        "ai_context": root / "ai-context.md",
        "task_backend": "native",
    }
    defaults.update(overrides)
    return LaunchConfig(**defaults)


def _mock_tasks_runner(next_id: str = "14", *, pending: int = 3):
    def runner(cmd):
        class Completed:
            returncode = 0
            stderr = ""

        if cmd[1] == "next":
            Completed.stdout = (
                f'{{"found": true, "task": {{"id": "{next_id}", "title": "Launcher task"}}}}'
            )
        elif cmd[1] == "list":
            tasks = [
                {"id": str(index), "title": f"Task {index}"}
                for index in range(1, pending + 1)
            ]
            import json

            Completed.stdout = json.dumps({"tasks": tasks})
        else:
            Completed.stdout = "{}"
        return Completed()

    return runner


def _passing_launch_checks() -> list[DiagnosticCheck]:
    return [
        DiagnosticCheck("CURSOR_API_KEY", True, "ok"),
        DiagnosticCheck("Native tasks", True, "ok"),
        DiagnosticCheck("Cursor SDK bridge", True, "ok"),
        DiagnosticCheck("Handover Task ID", True, "Parsed Task ID 14"),
        DiagnosticCheck("Complexity report", True, "ok"),
        DiagnosticCheck("Backend next", True, "Next task #14: 'Launcher task'"),
        DiagnosticCheck("Update handover", True, "ok"),
        DiagnosticCheck("ai-context", True, "ok"),
        DiagnosticCheck("Task queue list", True, "3 pending tasks"),
    ]


@dataclass
class FakePrompts:
    cycles: int = 2
    confirm: bool = True
    opus: bool = True
    action_choice: int = 0
    analyze_complexity: bool = True
    prd_path: str = ""
    tag: str = ""
    prompts_seen: list[str] | None = None

    def __post_init__(self) -> None:
        if self.prompts_seen is None:
            self.prompts_seen = []

    def ask_int(self, prompt: str, *, default: int, minimum: int = 1) -> int:
        self.prompts_seen.append(prompt)
        return self.cycles

    def ask_bool(self, prompt: str, *, default: bool) -> bool:
        if "analyze-complexity" in prompt:
            return self.analyze_complexity
        if prompt == LAUNCH_OPUS_PROMPT:
            self.prompts_seen.append(prompt)
            return self.opus
        return default

    def ask_choice(self, prompt: str, choices: list[str], *, default_index: int) -> int:
        if prompt == "Launcher action":
            return self.action_choice
        return default_index

    def ask_str(self, prompt: str, *, default: str) -> str:
        if "PRD" in prompt:
            return self.prd_path or default
        return self.tag or default

    def ask_confirm(self, prompt: str, *, default: bool = False) -> bool:
        self.prompts_seen.append(prompt)
        return self.confirm


def test_normalize_argv_defaults_to_launch():
    assert _normalize_argv([]) == ["launch"]
    assert _normalize_argv(["--cycles", "2"]) == ["launch", "--cycles", "2"]
    assert _normalize_argv(["run", "--cycles", "1"]) == ["run", "--cycles", "1"]


def test_suggest_cycles():
    assert suggest_cycles(None) == 1
    assert suggest_cycles(0) == 1
    assert suggest_cycles(3) == 3
    assert suggest_cycles(9) == 5


def test_check_update_handover_and_ai_context(project_tree: Path):
    assert check_update_handover(project_tree / "update-handover-prompt.md").passed is True
    assert check_ai_context(project_tree / "ai-context.md").passed is True
    assert check_update_handover(project_tree / "missing.md").passed is False


def test_check_tasks_pending_list(project_tree: Path):
    runner = _mock_tasks_runner(pending=2)
    result = check_tasks_pending_list(project_tree, runner=runner)
    assert result.passed is True
    assert "2 pending tasks" in result.detail


def test_build_run_argv_without_config(project_tree: Path):
    config = _launch_config(project_tree)
    choices = LaunchChoices(
        cycles=4,
        plain=True,
        strict_handover=True,
        fresh=True,
        resume=False,
        tag="phase-2",
        profile="daytime-fast",
        composer_tier="fast",
        opus_enabled=False,
    )
    argv = build_run_argv(config, choices)
    assert argv[:4] == ["run", "--cycles", "4", "--project-root"]
    assert str(project_tree) in argv
    assert "--first-prompt" not in argv
    assert "--plain" in argv
    assert "--strict-handover" in argv
    assert "--fresh" in argv
    assert "--profile" in argv and "daytime-fast" in argv
    assert "--composer-tier" in argv and "fast" in argv
    assert "--no-opus" in argv
    assert "--tag" in argv
    assert "phase-2" in argv


def test_build_run_argv_includes_first_prompt_when_handover_not_ready(
    project_tree: Path,
):
    (project_tree / "current-handover-prompt.md").write_text(
        "PRE-TASKS placeholder — no Task ID marker yet.\n",
        encoding="utf-8",
    )
    config = _launch_config(project_tree)
    choices = LaunchChoices(
        cycles=2,
        plain=False,
        strict_handover=False,
        fresh=False,
        resume=False,
        tag=None,
    )
    argv = build_run_argv(config, choices)
    assert "--first-prompt" in argv
    assert str(config.first_prompt) in argv


def test_build_run_argv_with_config_file(project_tree: Path, tmp_path: Path):
    config_path = tmp_path / "cyclopsctl.toml"
    config_path.write_text("cycles = 1\n", encoding="utf-8")
    config = _launch_config(project_tree, config_path=config_path.resolve())
    choices = LaunchChoices(
        cycles=2,
        plain=False,
        strict_handover=False,
        fresh=False,
        resume=False,
        tag=None,
    )
    argv = build_run_argv(config, choices)
    assert "--config" in argv
    assert str(config_path.resolve()) in argv
    assert "--first-prompt" not in argv


def test_gather_launch_status(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    from cyclopsctl.tasks.store import save_tag_tasks

    tasks_path = project_tree / ".cyclopsctl" / "tasks" / "tasks.json"
    save_tag_tasks(
        tasks_path,
        [
            {
                "id": task_id,
                "title": f"Launcher task {task_id}",
                "status": "pending",
                "priority": "high",
                "dependencies": [],
                "subtasks": [],
            }
            for task_id in (14, 15, 16, 17)
        ],
        tag="master",
        merge=False,
    )

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        status = gather_launch_status(
            _launch_config(project_tree),
            env={"CURSOR_API_KEY": "test-key"},
        )

    assert status.handover_task_id == 14
    assert status.pending_count == 4
    assert status.suggested_cycles == 4
    assert status.next_task is not None
    assert status.next_task.found is True
    assert all(check.passed for check in status.checks)


def test_prompt_launch_choices_mocked():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
        tag="master",
    )
    launch_status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 3,
            "resume_available": True,
            "handover_task_id": 14,
        },
    )()
    prompts = FakePrompts(cycles=3)
    choices = prompt_launch_choices(launch_status, prompts=prompts)
    assert choices == LaunchChoices(
        cycles=3,
        plain=False,
        strict_handover=False,
        fresh=False,
        resume=True,
        tag="master",
        composer_tier="standard",
        opus_enabled=True,
    )
    assert prompts.prompts_seen == [
        "Number of cycles",
        LAUNCH_OPUS_PROMPT,
        _format_cycles_confirm_message(3, 14),
    ]


def test_prompt_launch_choices_respects_no_opus_flag():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
    )
    launch_status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 2,
            "resume_available": False,
            "default_opus_enabled": True,
        },
    )()
    prompts = FakePrompts(cycles=2)
    choices = prompt_launch_choices(
        launch_status,
        prompts=prompts,
        opus_enabled=False,
    )
    assert choices is not None
    assert choices.opus_enabled is False
    assert LAUNCH_OPUS_PROMPT not in prompts.prompts_seen


def test_prompt_launch_choices_cancelled():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
    )
    launch_status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 1,
            "resume_available": False,
        },
    )()
    prompts = FakePrompts(confirm=False)
    assert prompt_launch_choices(launch_status, prompts=prompts) is None


def test_run_launch_non_tty_requires_explicit_flags(project_tree: Path, capsys):
    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": _launch_config(project_tree),
                "checks": _passing_launch_checks(),
                "handover_task_id": 14,
                "next_task": NextTaskLookup.from_task(
                    NextTaskResult(
                        task_id="14",
                        title="Launcher",
                        status=None,
                        priority=None,
                        complexity=None,
                        tag=None,
                    )
                ),
                "pending_count": 3,
                "suggested_cycles": 3,
                "resume_available": False,
                "next_task_error": None,
            },
        )()
        dispatch = run_launch(
            _launch_config(project_tree),
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    assert dispatch.argv is None
    assert dispatch.exit_code == 2
    assert "Interactive launcher requires a TTY or explicit --action with flags." in captured.err
    assert "bootstrap" in captured.err
    assert "doctor" in captured.err
    assert "models" in captured.err
    assert format_non_tty_message() in captured.err


def test_run_launch_non_tty_with_flags_assembles_argv(project_tree: Path):
    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": _launch_config(project_tree),
                "checks": _passing_launch_checks(),
                "handover_task_id": 14,
                "next_task": None,
                "pending_count": 2,
                "suggested_cycles": 2,
                "resume_available": False,
                "next_task_error": None,
            },
        )()
        dispatch = run_launch(
            _launch_config(project_tree),
            action="run",
            cycles=2,
            plain=True,
            strict_handover=True,
            fresh=True,
            tag="dev",
            composer_tier="fast",
            opus_enabled=False,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    assert dispatch.exit_code == 0
    assert dispatch.action == LaunchAction.RUN
    argv = dispatch.argv
    assert argv is not None
    assert "--cycles" in argv and "2" in argv
    assert "--plain" in argv
    assert "--strict-handover" in argv
    assert "--fresh" in argv
    assert "--composer-tier" in argv and "fast" in argv
    assert "--no-opus" in argv
    assert "--tag" in argv and "dev" in argv


def test_run_launch_blocks_when_checks_fail(project_tree: Path, capsys):
    failing = _passing_launch_checks()
    failing[0] = DiagnosticCheck("CURSOR_API_KEY", False, "missing")
    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": _launch_config(project_tree),
                "checks": failing,
                "handover_task_id": 14,
                "next_task": None,
                "pending_count": 1,
                "suggested_cycles": 1,
                "resume_available": False,
                "next_task_error": None,
            },
        )()
        dispatch = run_launch(
            _launch_config(project_tree),
            action="run",
            cycles=1,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    captured = capsys.readouterr()
    assert dispatch.argv is None
    assert dispatch.exit_code != 0
    assert "Cannot start run until all preflight checks pass." in captured.err


def test_can_spawn_run():
    assert can_spawn_run(_passing_launch_checks()) is True
    failing = _passing_launch_checks()
    failing[-1] = DiagnosticCheck("Task queue list", False, "boom")
    assert can_spawn_run(failing) is False


def test_load_launch_config_defaults_project_root_to_cwd(
    project_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(project_tree)
    config = load_launch_config()
    assert config.project_root == project_tree
    assert config.current_handover == (project_tree / "current-handover-prompt.md").resolve()


def test_run_launch_diagnostics_integration(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ), patch("cyclopsctl.doctor.check_workspace_dirty", return_value=None):
        checks = run_launch_diagnostics(
            _launch_config(project_tree),
            env={"CURSOR_API_KEY": "test-key"},
        )

    names = [check.name for check in checks]
    assert "Update handover" in names
    assert "ai-context" in names
    assert "Task queue list" in names
    assert all(check.passed for check in checks)


def test_cli_launch_subcommand_delegates(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    from cyclopsctl.launcher import LaunchDispatch

    with patch(
        "cyclopsctl.cli.run_launch",
        return_value=LaunchDispatch(
            action=LaunchAction.RUN,
            argv=["run", "--cycles", "1"],
            exit_code=0,
        ),
    ) as mock_launch, patch(
        "cyclopsctl.cli._run_command",
        return_value=0,
    ) as mock_run:
        code = main(
            [
                "launch",
                "--project-root",
                str(project_tree),
                "--cycles",
                "1",
                "--yes",
            ]
        )

    assert code == 0
    mock_launch.assert_called_once()
    mock_run.assert_called_once()


def test_cli_default_invocation_is_launch(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    from cyclopsctl.launcher import LaunchDispatch

    with patch(
        "cyclopsctl.cli.run_launch",
        return_value=LaunchDispatch(action=None, argv=None, exit_code=0),
    ) as mock_launch:
        code = main(["--project-root", str(project_tree), "--cycles", "1", "--yes"])

    assert code == 0
    mock_launch.assert_called_once()


def test_infer_launch_defaults_auto_resume():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
        tag="phase-4",
    )
    status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 3,
            "resume_available": True,
            "default_composer_tier": "standard",
            "default_opus_enabled": True,
        },
    )()
    choices = infer_launch_defaults(status)
    assert choices.resume is True
    assert choices.fresh is False
    assert choices.tag == "phase-4"
    assert choices.composer_tier == "standard"
    assert choices.opus_enabled is True


def test_infer_launch_defaults_respects_explicit_fresh():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
    )
    status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 1,
            "resume_available": True,
        },
    )()
    choices = infer_launch_defaults(status, fresh=True)
    assert choices.fresh is True
    assert choices.resume is False


def test_infer_launch_defaults_reads_strict_handover_from_config(
    project_tree: Path,
):
    (project_tree / "cyclopsctl.toml").write_text(
        "strict_handover = true\n",
        encoding="utf-8",
    )
    config = _launch_config(
        project_tree,
        config_path=(project_tree / "cyclopsctl.toml").resolve(),
    )
    status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 2,
            "resume_available": False,
        },
    )()
    choices = infer_launch_defaults(status)
    assert choices.strict_handover is True


def test_maybe_repair_handover_at_launch_syncs_stale_handover(
    project_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 0\n\nPlaceholder.\n",
        encoding="utf-8",
    )
    config = _launch_config(project_tree, tag="phase-4")
    status = type(
        "S",
        (),
        {
            "config": config,
            "checks": _passing_launch_checks(),
            "handover_task_id": 0,
            "next_task": NextTaskLookup.from_task(
                NextTaskResult(
                    task_id="14",
                    title="Launcher task",
                    status=None,
                    priority=None,
                    complexity=None,
                    tag=None,
                )
            ),
            "pending_count": 3,
            "suggested_cycles": 3,
            "resume_available": False,
            "next_task_error": None,
        },
    )()

    with patch(
        "cyclopsctl.launcher.sync_current_handover",
        return_value=("# Task ID: 14\n", 14),
    ) as mock_sync, patch(
        "cyclopsctl.launcher.gather_launch_status",
        return_value=type(
            "S",
            (),
            {
                "config": config,
                "checks": _passing_launch_checks(),
                "handover_task_id": 14,
                "next_task": status.next_task,
                "pending_count": 3,
                "suggested_cycles": 3,
                "resume_available": False,
                "next_task_error": None,
            },
        )(),
    ):
        _, refreshed, message = maybe_repair_handover_at_launch(config, status)

    mock_sync.assert_called_once()
    assert message == "Synced handover for task 14 — Launcher task"
    assert refreshed.handover_task_id == 14


def test_maybe_repair_handover_skips_when_queue_empty(project_tree: Path):
    config = _launch_config(project_tree)
    status = type(
        "S",
        (),
        {
            "config": config,
            "pending_count": 0,
            "next_task": NextTaskLookup.empty(tag="phase-4"),
        },
    )()
    _, same_status, message = maybe_repair_handover_at_launch(config, status)
    assert message is None
    assert same_status.pending_count == 0


def test_run_launch_tty_only_prompts_for_cycles(project_tree: Path):
    prompts = FakePrompts(cycles=4)
    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather, patch(
        "cyclopsctl.launcher.maybe_repair_handover_at_launch",
        side_effect=lambda config, status, **kwargs: (config, status, None),
    ), patch(
        "cyclopsctl.launcher._apply_prd_change_at_launch",
        return_value=(_launch_config(project_tree), None, None),
    ):
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": _launch_config(project_tree),
                "checks": _passing_launch_checks(),
                "handover_task_id": 14,
                "next_task": NextTaskLookup.from_task(
                    NextTaskResult(
                        task_id="14",
                        title="Launcher",
                        status=None,
                        priority=None,
                        complexity=None,
                        tag=None,
                    )
                ),
                "pending_count": 4,
                "suggested_cycles": 4,
                "resume_available": False,
                "next_task_error": None,
            },
        )()
        dispatch = run_launch(
            _launch_config(project_tree),
            stdin_is_tty=True,
            stderr_is_tty=False,
            prompts=prompts,
        )

    assert dispatch.exit_code == 0
    assert dispatch.argv is not None
    assert "--cycles" in dispatch.argv and "4" in dispatch.argv
    assert prompts.prompts_seen == [
        "Number of cycles",
        LAUNCH_OPUS_PROMPT,
        _format_cycles_confirm_message(4, 14),
    ]


def test_resolve_launch_choices_non_interactive():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
        tag="master",
    )
    status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 2,
            "resume_available": False,
        },
    )()
    choices = resolve_launch_choices(
        status,
        cycles=2,
        plain=True,
        strict_handover=False,
        fresh=False,
        resume=True,
        tag="dev",
        profile="fast-profile",
        composer_tier="fast",
        opus_enabled=False,
        assume_yes=True,
        stdin_is_tty=False,
    )
    assert choices == LaunchChoices(
        cycles=2,
        plain=True,
        strict_handover=False,
        fresh=False,
        resume=True,
        tag="dev",
        profile="fast-profile",
        composer_tier="fast",
        opus_enabled=False,
    )


def test_resolve_launch_choices_infers_resume_when_unspecified():
    config = LaunchConfig(
        project_root=Path("/tmp/project"),
        first_prompt=Path("/tmp/project/first.md"),
        current_handover=Path("/tmp/project/current.md"),
        update_handover=Path("/tmp/project/update.md"),
        complexity_report=Path("/tmp/project/report.json"),
        ai_context=Path("/tmp/project/ai-context.md"),
        tag="master",
    )
    status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 2,
            "resume_available": True,
        },
    )()
    choices = resolve_launch_choices(
        status,
        cycles=2,
        plain=None,
        strict_handover=None,
        fresh=None,
        resume=None,
        tag=None,
        profile=None,
        composer_tier=None,
        opus_enabled=None,
        assume_yes=True,
        stdin_is_tty=False,
    )
    assert choices is not None
    assert choices.resume is True
    assert choices.fresh is False


def test_prompt_launch_action_mocked():
    prompts = FakePrompts(action_choice=1)
    assert prompt_launch_action(prompts) == LaunchAction.BOOTSTRAP


def test_build_bootstrap_argv(project_tree: Path):
    config = _launch_config(project_tree)
    choices = BootstrapChoices(
        from_prd=project_tree / "prd.md",
        tag="phase-1",
        skip_analyze=True,
    )
    argv = build_bootstrap_argv(config, choices)
    assert argv[0] == "bootstrap"
    assert "--from-prd" in argv
    assert "--skip-analyze" in argv
    assert "--tag" in argv and "phase-1" in argv


def test_build_doctor_and_models_argv(project_tree: Path):
    config = _launch_config(project_tree)
    doctor_argv = build_doctor_argv(config, fix=True)
    assert doctor_argv[:2] == ["doctor", "--project-root"]
    assert "--fix" in doctor_argv
    assert build_models_argv() == ["models"]


def test_run_launch_bootstrap_handoff(project_tree: Path):
    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": _launch_config(project_tree),
                "checks": _passing_launch_checks(),
                "handover_task_id": 14,
                "next_task": None,
                "pending_count": 1,
                "suggested_cycles": 1,
                "resume_available": False,
                "profile_names": (),
                "default_composer_tier": "standard",
                "default_opus_enabled": True,
                "next_task_error": None,
            },
        )()
        dispatch = run_launch(
            _launch_config(project_tree),
            action="bootstrap",
            from_prd=project_tree / "prd.md",
            tag="dev",
            skip_analyze=True,
            assume_yes=True,
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    assert dispatch.action == LaunchAction.BOOTSTRAP
    assert dispatch.argv is not None
    assert dispatch.argv[0] == "bootstrap"
    assert "--from-prd" in dispatch.argv
    assert "--skip-analyze" in dispatch.argv


def test_run_launch_doctor_and_models_dispatch(project_tree: Path):
    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": _launch_config(project_tree),
                "checks": _passing_launch_checks(),
                "handover_task_id": 14,
                "next_task": None,
                "pending_count": 1,
                "suggested_cycles": 1,
                "resume_available": False,
                "profile_names": (),
                "default_composer_tier": "standard",
                "default_opus_enabled": True,
                "next_task_error": None,
            },
        )()
        doctor = run_launch(
            _launch_config(project_tree),
            action="doctor",
            stdin_is_tty=False,
            stderr_is_tty=False,
        )
        models = run_launch(
            _launch_config(project_tree),
            action="models",
            stdin_is_tty=False,
            stderr_is_tty=False,
        )

    assert doctor.argv == build_doctor_argv(_launch_config(project_tree))
    assert models.argv == ["models"]


def test_prompt_bootstrap_choices_mocked(project_tree: Path):
    config = _launch_config(project_tree)
    launch_status = type(
        "S",
        (),
        {
            "config": config,
            "suggested_cycles": 1,
            "resume_available": False,
        },
    )()
    prompts = FakePrompts(
        prd_path=str(project_tree / "custom-prd.md"),
        tag="bootstrap-tag",
        analyze_complexity=False,
    )
    choices = prompt_bootstrap_choices(launch_status, prompts=prompts)
    assert choices == BootstrapChoices(
        from_prd=project_tree / "custom-prd.md",
        tag="bootstrap-tag",
        skip_analyze=True,
    )


def test_cli_launch_dispatches_bootstrap(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.cli.run_launch") as mock_launch, patch(
        "cyclopsctl.cli._bootstrap_command",
        return_value=0,
    ) as mock_bootstrap:
        from cyclopsctl.launcher import LaunchDispatch

        mock_launch.return_value = LaunchDispatch(
            action=LaunchAction.BOOTSTRAP,
            argv=["bootstrap", "--project-root", str(project_tree)],
            exit_code=0,
        )
        code = main(
            [
                "launch",
                "--project-root",
                str(project_tree),
                "--action",
                "bootstrap",
                "--yes",
            ]
        )

    assert code == 0
    mock_launch.assert_called_once()
    mock_bootstrap.assert_called_once()


def test_format_launch_summary_line_shows_workflow_upgrade_when_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cyclopsctl.tasks.store import save_tag_tasks, set_current_tag

    root = tmp_path / "stale-workflow"
    root.mkdir()
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text("# Task ID: 3\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text(
        "Mark tasks done manually.\n",
        encoding="utf-8",
    )
    (root / "ai-context.md").write_text("# AI\n", encoding="utf-8")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        '{"complexityAnalysis": [{"taskId": 3, "complexityScore": 5}]}',
        encoding="utf-8",
    )
    tasks_path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(
        tasks_path,
        [
            {
                "id": 3,
                "title": "Stale workflow task",
                "status": "pending",
                "priority": "high",
                "dependencies": [],
                "subtasks": [],
            }
        ],
        tag="master",
        merge=False,
    )
    set_current_tag(root / ".cyclopsctl" / "tasks" / "state.json", "master")

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ), patch("cyclopsctl.doctor.check_workspace_dirty", return_value=None):
        status = gather_launch_status(
            _launch_config(root),
            env={"CURSOR_API_KEY": "test-key"},
        )

    summary = format_launch_summary_line(status)
    assert "Workflow: upgrade available" in summary


def test_format_launch_summary_line(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ), patch("cyclopsctl.doctor.check_workspace_dirty", return_value=None):
        status = gather_launch_status(
            _launch_config(project_tree),
            env={"CURSOR_API_KEY": "test-key"},
        )

    summary = format_launch_summary_line(status)
    assert summary == "Pending: 1 · Handover: task 14 · Next: task 14 · Tag: master"


def test_launch_overview_includes_summary_line(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ), patch("cyclopsctl.doctor.check_workspace_dirty", return_value=None):
        status = gather_launch_status(
            _launch_config(project_tree),
            env={"CURSOR_API_KEY": "test-key"},
        )

    overview = format_launch_overview_plain(status)
    assert "Pending: 1 · Handover: task 14 · Next: task 14 · Tag: master" in overview
