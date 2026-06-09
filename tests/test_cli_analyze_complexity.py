"""Tests for cyclopsctl analyze-complexity subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cyclopsctl.cli import main, run_analyze_complexity
from cyclopsctl.tasks.store import TagState, TaskStore


def _write_native_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root, backend="native")
    store.ensure_native_layout()
    store.save_tag_tasks(
        [
            {
                "id": 1,
                "title": "First task",
                "description": "Do it.",
                "details": "Details.",
                "testStrategy": "pytest",
                "priority": "high",
                "dependencies": [],
                "status": "pending",
                "subtasks": [],
            }
        ],
        tag="phase-2",
        merge=True,
    )
    store.save_tag_state(TagState(current_tag="phase-2"))


def test_run_analyze_complexity_requires_task_storage(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()

    code = run_analyze_complexity(empty)

    assert code != 0


def test_run_analyze_complexity_runs_by_default_when_report_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_native_project(root)
    store = TaskStore(root, backend="native")
    store.save_complexity_report(
        {
            "meta": {"generatedAt": "2026-01-01T00:00:00Z", "tasksAnalyzed": 1},
            "complexityAnalysis": [
                {"taskId": 99, "taskTitle": "Stale", "complexityScore": 3},
            ],
        }
    )

    captured: list[bool] = []

    def fake_analyze(project_root: Path, *, tag=None, config=None, **_kwargs: object) -> bool:
        _ = project_root, tag
        captured.append(config.skip_if_exists)
        return True

    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        fake_analyze,
    )

    code = run_analyze_complexity(root, tag="phase-2")

    assert code == 0
    assert captured == [False]


def test_run_analyze_complexity_honors_skip_if_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_native_project(root)
    store = TaskStore(root, backend="native")
    store.save_complexity_report(
        {
            "meta": {"generatedAt": "2026-01-01T00:00:00Z", "tasksAnalyzed": 1},
            "complexityAnalysis": [{"taskId": 1, "complexityScore": 4}],
        }
    )

    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        lambda *_a, **_k: False,
    )

    code = run_analyze_complexity(root, tag="phase-2", skip_if_exists=True)

    assert code == 0


def test_cli_analyze_complexity_uses_managed_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "repo"
    _write_native_project(root)
    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch(
            "cyclopsctl.cli.run_analyze_complexity",
            return_value=0,
        ) as mock_run:
            code = main(
                [
                    "analyze-complexity",
                    "--project-root",
                    str(root),
                    "--tag",
                    "phase-2",
                ]
            )

    assert code == 0
    mock_bridge.assert_called_once_with(root.resolve())
    mock_run.assert_called_once_with(
        root.resolve(),
        tag="phase-2",
        analyze_model=None,
        skip_if_exists=False,
    )


def test_cli_analyze_complexity_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cyclopsctl.tasks.analyze import build_complexity_report

    root = tmp_path / "repo"
    _write_native_project(root)
    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    report = build_complexity_report(
        [
            {
                "taskId": 1,
                "taskTitle": "First task",
                "complexityScore": 7,
                "reasoning": "Moderate scope.",
            }
        ]
    )

    def fake_analyze(project_root: Path, *, tag=None, config=None, **_kwargs: object) -> bool:
        _ = tag, config
        store = TaskStore(project_root, backend="native")
        store.save_complexity_report(report)
        tasks = store.load_tag_tasks("phase-2")
        tasks[0]["complexity"] = 7
        store.save_tag_tasks(tasks, tag="phase-2", merge=True)
        return True

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(
            "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
            fake_analyze,
        )
        code = main(["analyze-complexity", "--project-root", str(root)])

    assert code == 0
    saved = json.loads(
        (root / ".cyclopsctl" / "reports" / "complexity-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["complexityAnalysis"][0]["complexityScore"] == 7
    tasks = json.loads(
        (root / ".cyclopsctl" / "tasks" / "tasks.json").read_text(encoding="utf-8")
    )
    assert tasks["phase-2"]["tasks"][0]["complexity"] == 7
