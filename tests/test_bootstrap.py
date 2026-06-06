"""Tests for task 19: PRD bootstrap pipeline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cyclopsctl.bootstrap import (
    BootstrapConfig,
    BootstrapError,
    copy_workflow_templates,
    resolve_bootstrap_config,
    resolve_prd_path,
    run_bootstrap,
)
from cyclopsctl.cli import main
from cyclopsctl.prompt import parse_task_id, render_synced_handover
from cyclopsctl.tasks.store import save_tag_tasks
from cyclopsctl.tasks.types import TaskShowDetail


def _sample_show_json(task_id: str = "3") -> dict:
    return {
        "task": {
            "id": task_id,
            "title": "Example bootstrap task",
            "description": "Do the thing.",
            "details": "Implement bootstrap helpers.",
            "testStrategy": "Mock subprocess calls and verify handover shape.",
            "priority": "high",
            "dependencies": ["1", "2"],
            "status": "pending",
            "complexity": 5,
        },
        "found": True,
        "storageType": "file",
    }


def _sample_next_json(task_id: str = "3") -> dict:
    return {
        "task": {
            "id": task_id,
            "title": "Example bootstrap task",
            "description": "Do the thing.",
            "status": "pending",
            "priority": "high",
            "complexity": 5,
        },
        "found": True,
        "tag": "master",
        "hasAnyTasks": True,
    }


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prd.md").write_text("# PRD: Sample Project\n\nOverview.", encoding="utf-8")
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    return root


def _sample_native_task(task_id: int = 3) -> dict:
    return {
        "id": task_id,
        "title": "Example bootstrap task",
        "description": "Do the thing.",
        "details": "Implement bootstrap helpers.",
        "testStrategy": "Mock SDK calls and verify handover shape.",
        "priority": "high",
        "dependencies": [],
        "status": "pending",
        "subtasks": [],
        "complexity": 5,
    }


def _seed_native_tasks(project_root: Path, *, task_id: int = 3) -> None:
    tasks_path = project_root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(
        tasks_path,
        [_sample_native_task(task_id)],
        tag="master",
        merge=False,
    )


def _mock_native_parse_analyze(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parse_error: str | None = None,
    track_append: list[bool] | None = None,
) -> None:
    def fake_parse(
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        **_kwargs: object,
    ) -> None:
        if track_append is not None:
            track_append.append(append)
        if parse_error is not None:
            raise RuntimeError(parse_error)
        _seed_native_tasks(project_root)

    def fake_analyze(project_root: Path, *, tag: str | None = None, **_kwargs: object) -> bool:
        report = project_root / ".cyclopsctl" / "reports" / "complexity-report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"complexityAnalysis": [{"taskId": 3, "complexityScore": 5}]})
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


def test_resolve_prd_path_missing_raises(project_root: Path):
    config = resolve_bootstrap_config(project_root=project_root, from_prd=Path("missing.md"))
    with pytest.raises(BootstrapError, match="PRD file not found"):
        resolve_prd_path(config)


def test_render_synced_handover_contains_required_sections():
    task = TaskShowDetail(
        task_id="7",
        title="Bootstrap wiring",
        description="Wire CLI.",
        details="Add subcommand.",
        test_strategy="pytest",
        priority="high",
        dependencies=("6",),
        status="pending",
        complexity=4,
    )
    text = render_synced_handover(
        task=task,
        project_name="Sample Project",
        project_root=Path("G:/repo"),
        tech_stack="Python 3.10+",
        branch="main",
    )
    assert text.startswith("# Session Handover")
    assert "# Task ID: 7" in text
    assert "## Environment" in text
    assert "## Core Handover Rules" in text
    assert "## Implementation Phase — Do Only This" in text
    assert "## Current Task: 7 — Bootstrap wiring" in text
    assert "### Description" in text
    assert "### Implementation Details" in text
    assert "### Test Strategy" in text
    assert "## Verification" in text
    assert "## Model Selection" in text
    assert "Composer 2.5" in text
    assert "Context7 MCP" in text
    assert "cyclopsctl tasks" in text


def test_render_synced_handover_empty_queue_uses_task_id_zero():
    text = render_synced_handover(
        task=None,
        project_name="Sample Project",
        project_root=Path("G:/repo"),
        tech_stack="Python 3.10+",
        branch="main",
    )
    assert "# Task ID: 0" in text
    assert "## Status: Task queue complete" in text
    assert "## Current Task:" not in text


def test_run_bootstrap_full_pipeline_native_backend(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _mock_native_parse_analyze(monkeypatch)
    config = resolve_bootstrap_config(project_root=project_root)
    result = run_bootstrap(config)

    assert result.task_id == 3
    handover = result.handover_path.read_text(encoding="utf-8")
    assert parse_task_id(handover) == 3
    assert "# Task ID: 3" in handover
    assert "Example bootstrap task" in handover
    assert (project_root / ".cyclopsctl" / "tasks" / "tasks.json").is_file()


def test_run_bootstrap_skip_analyze(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    analyzed: list[bool] = []

    def fake_analyze(project_root: Path, *, tag: str | None = None, **_kwargs: object) -> bool:
        analyzed.append(True)
        return True

    _mock_native_parse_analyze(monkeypatch)
    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        fake_analyze,
    )
    config = resolve_bootstrap_config(project_root=project_root, skip_analyze=True)
    run_bootstrap(config)

    assert analyzed == []


def test_run_bootstrap_sync_handover_only_skips_parse_and_analyze(project_root: Path):
    _seed_native_tasks(project_root)
    config = resolve_bootstrap_config(project_root=project_root, sync_handover_only=True)
    result = run_bootstrap(config)

    assert result.task_id == 3


def test_run_bootstrap_runs_native_init_when_storage_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "greenfield"
    root.mkdir()
    (root / "prd.md").write_text("# PRD: Greenfield\n", encoding="utf-8")
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    _mock_native_parse_analyze(monkeypatch)

    config = resolve_bootstrap_config(project_root=root)
    assert not (root / ".cyclopsctl" / "tasks").exists()
    run_bootstrap(config)
    assert (root / ".cyclopsctl" / "tasks" / "tasks.json").is_file()


def test_run_bootstrap_parse_failure_surfaces_clear_error(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _mock_native_parse_analyze(monkeypatch, parse_error="parse-prd failed: invalid PRD")
    config = resolve_bootstrap_config(project_root=project_root)
    with pytest.raises(BootstrapError, match="parse-prd failed"):
        run_bootstrap(config)


def test_run_bootstrap_append_flag_propagates(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    append_flags: list[bool] = []
    _mock_native_parse_analyze(monkeypatch, track_append=append_flags)
    config = resolve_bootstrap_config(project_root=project_root, append=True)
    run_bootstrap(config)

    assert append_flags == [True]


def test_run_bootstrap_with_workflow_copies_missing_templates(project_root: Path):
    _seed_native_tasks(project_root)
    config = resolve_bootstrap_config(
        project_root=project_root,
        sync_handover_only=True,
        with_workflow=True,
    )
    result = run_bootstrap(config)

    ai_context = (project_root / "ai-context.md").read_text(encoding="utf-8")
    assert (project_root / "update-handover-prompt.md").is_file()
    assert (project_root / "docs" / "index.md").is_file()
    assert "Sample Project - AI Context" in ai_context
    assert "python -m pytest" in ai_context
    assert result.copied_templates == (
        "ai-context.md",
        "update-handover-prompt.md",
        "docs/index.md",
    )


def test_copy_workflow_templates_is_idempotent(project_root: Path):
    first = copy_workflow_templates(project_root)
    second = copy_workflow_templates(project_root)
    assert first == (
        "ai-context.md",
        "update-handover-prompt.md",
        "docs/index.md",
    )
    assert second == ()


def test_cli_bootstrap_missing_prd_exits_nonzero(project_root: Path, capsys):
    (project_root / "prd.md").unlink()
    code = main(
        [
            "bootstrap",
            "--project-root",
            str(project_root),
        ]
    )
    assert code != 0
    err = capsys.readouterr().err
    assert "PRD file not found" in err


def test_cli_bootstrap_help_exits_zero():
    assert main(["bootstrap", "--help"]) == 0
