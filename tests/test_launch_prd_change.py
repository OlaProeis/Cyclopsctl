"""Tests for task 6: PRD-change detection and new-tag launch flow."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.launcher import LaunchAction, run_launch
from cyclopsctl.project_setup import (
    INIT_MISSING_TOML_MESSAGE,
    INIT_REQUIRED_MESSAGE,
    LaunchPrdChangeError,
    LaunchReadinessError,
    LAST_PARSED_PRD_REL,
    handle_launch_prd_change,
    list_existing_tags,
    propose_tag_name,
    prd_hash_changed,
    sha256_file,
    slugify_tag_name,
    write_last_parsed_prd,
)
from cyclopsctl.config import LaunchConfig


def _write_tasks_json(root: Path, *, tags: dict[str, tuple[int, ...]]) -> None:
    tasks_dir = root / ".cyclopsctl" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        tag: {
            "tasks": [
                {
                    "id": task_id,
                    "title": f"Task {task_id}",
                    "status": "pending",
                    "priority": "high",
                    "dependencies": [],
                    "subtasks": [],
                }
                for task_id in task_ids
            ]
        }
        for tag, task_ids in tags.items()
    }
    (tasks_dir / "tasks.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    from cyclopsctl.tasks.store import set_current_tag

    set_current_tag(tasks_dir / "state.json", "master")


def _write_initialized_project(root: Path, *, prd_text: str = "# PRD: Phase 4\n") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("CURSOR_API_KEY=test\n", encoding="utf-8")
    (root / "prd.md").write_text(prd_text, encoding="utf-8")
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    (root / ".cyclopsctl" / "reports").mkdir(parents=True, exist_ok=True)
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}',
        encoding="utf-8",
    )
    (root / "ai-context.md").write_text("# AI Context\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    _write_tasks_json(root, tags={"master": (1, 2)})
    write_last_parsed_prd(root, root / "prd.md", tag="master")


def _mock_native_prd_change(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_parse(
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        **_kwargs: object,
    ) -> None:
        tasks_path = project_root / ".cyclopsctl" / "tasks" / "tasks.json"
        document = json.loads(tasks_path.read_text(encoding="utf-8"))
        document[tag or "master"] = {
            "tasks": [
                {
                    "id": 1,
                    "title": "First new task",
                    "description": "Do it.",
                    "details": "Details.",
                    "testStrategy": "pytest",
                    "priority": "high",
                    "dependencies": [],
                    "status": "pending",
                    "subtasks": [],
                    "complexity": 5,
                }
            ]
        }
        tasks_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def fake_analyze(project_root: Path, *, tag: str | None = None, **_kwargs: object) -> bool:
        report = project_root / ".cyclopsctl" / "reports" / "complexity-report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]})
            + "\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(
        "cyclopsctl.tasks.parse_prd.parse_prd_with_cursor",
        fake_parse,
    )
    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        fake_analyze,
    )


def _make_prd_change_runner(root: Path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        subcommand = cmd[1] if len(cmd) > 1 else ""
        if subcommand == "next":
            stdout = json.dumps(
                {
                    "found": True,
                    "tag": "phase-4",
                    "task": {
                        "id": "1",
                        "title": "First new task",
                        "status": "pending",
                        "priority": "high",
                    },
                }
            )
        elif subcommand == "show":
            stdout = json.dumps(
                {
                    "found": True,
                    "task": {
                        "id": "1",
                        "title": "First new task",
                        "description": "Do it.",
                        "details": "Details.",
                        "testStrategy": "pytest",
                        "priority": "high",
                        "dependencies": [],
                        "status": "pending",
                        "complexity": 5,
                    },
                }
            )
        elif subcommand == "list":
            stdout = json.dumps({"tasks": [{"id": "1", "title": "First new task"}], "tag": "phase-4"})
        else:
            stdout = ""
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _launch_config(root: Path) -> LaunchConfig:
    return LaunchConfig(
        project_root=root,
        first_prompt=root / "prompts" / "first.md",
        current_handover=root / "current-handover-prompt.md",
        update_handover=root / "update-handover-prompt.md",
        complexity_report=root / ".cyclopsctl" / "reports" / "complexity-report.json",
        ai_context=root / "ai-context.md",
        task_backend="native",
    )


def test_slugify_tag_name():
    assert slugify_tag_name("Phase 4 - Smart Launch") == "phase-4-smart-launch"
    assert slugify_tag_name("Feature Dashboard!!!") == "feature-dashboard"


def test_propose_tag_name_from_prd_title(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    prd = root / "prd.md"
    prd.write_text("# PRD: Phase 4 — Smart Launch\n", encoding="utf-8")
    _write_tasks_json(root, tags={"master": (1,)})

    assert propose_tag_name(root, prd) == "phase-4"


def test_propose_tag_name_date_fallback(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    prd = root / "prd.md"
    prd.write_text("No heading here\n", encoding="utf-8")
    _write_tasks_json(root, tags={"master": (1,)})

    fixed = datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert propose_tag_name(root, prd, now=fixed) == "prd-2026-06-05"


def test_propose_tag_name_avoids_existing_tags(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    prd = root / "prd.md"
    prd.write_text("# PRD: Phase 4\n", encoding="utf-8")
    _write_tasks_json(root, tags={"master": (1,), "phase-4": (1,)})

    assert propose_tag_name(root, prd) == "phase-4-2"


def test_unchanged_prd_skips_new_tag_flow(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    config_paths = _launch_config(root)

    result = handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
    )

    assert result is None


def test_changed_prd_runs_tag_pipeline_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd.md").write_text("# PRD: Phase 5\n\nUpdated scope.\n", encoding="utf-8")
    _mock_native_prd_change(monkeypatch)
    config_paths = _launch_config(root)

    result = handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
        assume_yes=True,
        task_backend="native",
    )

    assert result is not None
    assert result.new_tag_created is True
    assert result.active_tag == "phase-5"
    assert result.steps[:4] == (
        "add-tag",
        "use-tag",
        "parse-prd",
        "analyze-complexity",
    )

    last = json.loads((root / LAST_PARSED_PRD_REL).read_text(encoding="utf-8"))
    assert last["tag"] == "phase-5"
    assert last["sha256"] == sha256_file(root / "prd.md")
    assert prd_hash_changed(root, root / "prd.md") is False


def test_changed_prd_preserves_customized_ai_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    custom_ai = (
        "# Custom Project - AI Context\n\n"
        "## Rules (DO NOT UPDATE)\n"
        "- Keep going.\n\n"
        "## Implementation Phase Rules\n"
        "- Implement only.\n\n"
        "## Update Phase Rules\n"
        "- Update memory only.\n\n"
        "## Project Memory\n"
        "- Phase 1 fact: auth lives in src/auth.py\n"
        "- Phase 4 fact: routing uses ModelRouter\n"
    )
    (root / "ai-context.md").write_text(custom_ai, encoding="utf-8")
    (root / "prd.md").write_text("# PRD: Phase 5\n\nUpdated scope.\n", encoding="utf-8")
    _mock_native_prd_change(monkeypatch)
    config_paths = _launch_config(root)

    result = handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
        assume_yes=True,
        task_backend="native",
    )

    assert result is not None
    assert result.active_tag == "phase-5"
    assert (root / "ai-context.md").read_text(encoding="utf-8") == custom_ai
    assert "Phase 1 fact: auth lives in src/auth.py" in custom_ai
    assert "Phase 4 fact: routing uses ModelRouter" in (
        root / "ai-context.md"
    ).read_text(encoding="utf-8")


def test_changed_prd_passes_bootstrap_model_to_parse_and_analyze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd.md").write_text("# PRD: Phase 5\n\nUpdated scope.\n", encoding="utf-8")

    captured: dict[str, str | None] = {}

    def fake_parse(
        self,
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        parse_model: str | None = None,
        **_kwargs: object,
    ) -> None:
        _ = self, project_root, prd, tag
        captured["parse_model"] = parse_model

    def fake_analyze(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
        analyze_model: str | None = None,
        **_kwargs: object,
    ) -> None:
        _ = self, project_root, tag
        captured["analyze_model"] = analyze_model

    monkeypatch.setattr(
        "cyclopsctl.tasks.native_backend.NativeTaskBackend.parse_prd",
        fake_parse,
    )
    monkeypatch.setattr(
        "cyclopsctl.tasks.native_backend.NativeTaskBackend.analyze_complexity",
        fake_analyze,
    )
    monkeypatch.setattr(
        "cyclopsctl.bootstrap.sync_current_handover",
        lambda *_a, **_k: None,
    )
    config_paths = _launch_config(root)

    handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
        assume_yes=True,
        bootstrap_model="fable-high-thinking",
        task_backend="native",
    )

    assert captured == {
        "parse_model": "fable-high-thinking",
        "analyze_model": "fable-high-thinking",
    }


def test_changed_prd_forces_analyze_when_prior_report_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "meta": {"generatedAt": "2026-01-01T00:00:00Z", "tasksAnalyzed": 2},
                "complexityAnalysis": [
                    {"taskId": 1, "taskTitle": "Old", "complexityScore": 4},
                    {"taskId": 2, "taskTitle": "Old", "complexityScore": 6},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "prd.md").write_text("# PRD: Phase 5\n\nUpdated scope.\n", encoding="utf-8")

    captured: list[bool] = []

    def fake_analyze(
        project_root: Path,
        *,
        tag: str | None = None,
        config=None,
        **_kwargs: object,
    ) -> bool:
        _ = project_root, tag
        captured.append(config.skip_if_exists)
        return True

    monkeypatch.setattr(
        "cyclopsctl.tasks.parse_prd.parse_prd_with_cursor",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        fake_analyze,
    )
    config_paths = _launch_config(root)

    handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
        assume_yes=True,
        task_backend="native",
    )

    assert captured == [False]


def test_changed_prd_keeps_prior_tags_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    original_tasks = json.loads(
        (root / ".cyclopsctl" / "tasks" / "tasks.json").read_text(encoding="utf-8")
    )
    (root / "prd.md").write_text("# PRD: Phase 5\n", encoding="utf-8")
    _mock_native_prd_change(monkeypatch)
    config_paths = _launch_config(root)

    handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
        assume_yes=True,
        task_backend="native",
    )

    tasks_after = json.loads(
        (root / ".cyclopsctl" / "tasks" / "tasks.json").read_text(encoding="utf-8")
    )
    assert "master" in tasks_after
    assert tasks_after["master"] == original_tasks["master"]
    assert "phase-5" in tasks_after
    assert list_existing_tags(root) == frozenset({"master", "phase-5"})


def test_uninitialized_project_directs_to_init(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prd.md").write_text("# PRD: Phase 4\n", encoding="utf-8")
    config_paths = _launch_config(root)

    with pytest.raises(LaunchReadinessError, match=INIT_REQUIRED_MESSAGE):
        handle_launch_prd_change(
            root,
            current_handover=config_paths.current_handover,
            complexity_report=config_paths.complexity_report,
            ai_context=config_paths.ai_context,
        )


def test_missing_last_parsed_backfills_and_continues(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prd.md").write_text("# PRD: Phase 4\n", encoding="utf-8")
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    _write_tasks_json(root, tags={"master": (1,)})
    config_paths = _launch_config(root)

    result = handle_launch_prd_change(
        root,
        current_handover=config_paths.current_handover,
        complexity_report=config_paths.complexity_report,
        ai_context=config_paths.ai_context,
    )

    assert result is None
    state_path = root / LAST_PARSED_PRD_REL
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["tag"] == "master"
    assert state["path"] == "prd.md"
    assert state["sha256"] == sha256_file(root / "prd.md")


def test_run_launch_unchanged_prd_continues(tmp_path: Path, capsys):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    config = _launch_config(root)

    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
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
            },
        )()
        with patch("cyclopsctl.launcher.can_spawn_run", return_value=True):
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
    assert "PRD changed" not in captured.err


def test_run_launch_changed_prd_sets_new_tag_on_run_argv(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd.md").write_text("# PRD: Phase 5\n", encoding="utf-8")
    config = _launch_config(root)
    _mock_native_prd_change(monkeypatch)

    with patch("cyclopsctl.launcher.gather_launch_status") as mock_gather:
        mock_gather.return_value = type(
            "S",
            (),
            {
                "config": config,
                "checks": [],
                "handover_task_id": 1,
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
        with patch("cyclopsctl.launcher.can_spawn_run", return_value=True):
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
    assert "--tag" in dispatch.argv
    assert "phase-5" in dispatch.argv
    assert "PRD changed" in captured.err


def test_run_launch_uninitialized_exits_with_init_message(tmp_path: Path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prd.md").write_text("# PRD\n", encoding="utf-8")
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
                "pending_count": 0,
                "suggested_cycles": 1,
                "resume_available": False,
                "profile_names": (),
                "default_composer_tier": "standard",
                "default_opus_enabled": True,
                "next_task_error": None,
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
    assert INIT_REQUIRED_MESSAGE in captured.err
