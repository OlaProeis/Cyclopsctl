"""Tests for task 13: run history and resume-aware startup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.config import build_run_config, build_status_config
from cyclopsctl.history import (
    RunHistory,
    build_history_from_run,
    clear_history,
    default_history_path,
    format_history_summary,
    handover_ready_for_implementation,
    read_history,
    resolve_startup,
    validate_handover_against_history,
    write_history,
)
from cyclopsctl.prompt import snapshot_handover


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 13\n\nImplement task 13.\n",
        encoding="utf-8",
    )
    return root.resolve()


def _history_path(project_root: Path) -> Path:
    return project_root / ".cyclopsctl" / "run-history.json"


def _sample_history(project_root: Path, *, task_id: int = 13) -> RunHistory:
    return RunHistory(
        completed_cycle_task_ids=[12],
        last_handover_task_id=task_id,
        last_handover_content_hash="abc123",
        last_run_at="2026-06-05T12:00:00+00:00",
        bootstrap_complete=True,
        project_root=str(project_root),
    )


def test_default_history_path(project_root: Path):
    path = default_history_path(project_root)
    assert path == (project_root / ".cyclopsctl" / "run-history.json").resolve()


def test_write_read_clear_history(project_root: Path):
    path = _history_path(project_root)
    history = _sample_history(project_root)
    write_history(path, history)

    result = read_history(path)
    assert result.kind == "ok"
    assert result.history == history

    clear_history(path)
    missing = read_history(path)
    assert missing.kind == "missing"


def test_read_history_corrupt_json(project_root: Path):
    path = _history_path(project_root)
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")

    result = read_history(path)
    assert result.kind == "corrupt"
    assert "invalid JSON" in (result.message or "")


def test_resolve_startup_fresh_ignores_history(project_root: Path):
    path = _history_path(project_root)
    write_history(path, _sample_history(project_root))

    resolution = resolve_startup(
        project_root=project_root,
        handover_path=project_root / "current-handover-prompt.md",
        history_path=path,
        fresh=True,
    )
    assert resolution.use_first_prompt_for_cycle_one is True
    assert resolution.resumed is False


def test_resolve_startup_resumes_when_history_matches(project_root: Path):
    path = _history_path(project_root)
    write_history(path, _sample_history(project_root, task_id=13))

    resolution = resolve_startup(
        project_root=project_root,
        handover_path=project_root / "current-handover-prompt.md",
        history_path=path,
    )
    assert resolution.use_first_prompt_for_cycle_one is False
    assert resolution.resumed is True
    assert resolution.validation is not None
    assert resolution.validation.aligned is True


def test_resolve_startup_fresh_when_no_history(project_root: Path):
    path = _history_path(project_root)

    resolution = resolve_startup(
        project_root=project_root,
        handover_path=project_root / "current-handover-prompt.md",
        history_path=path,
    )
    assert resolution.use_first_prompt_for_cycle_one is False
    assert resolution.resumed is False


def test_resolve_startup_fresh_when_bootstrap_incomplete(project_root: Path):
    path = _history_path(project_root)
    history = RunHistory(
        completed_cycle_task_ids=[],
        last_handover_task_id=None,
        last_handover_content_hash=None,
        last_run_at="2026-06-05T12:00:00+00:00",
        bootstrap_complete=False,
        project_root=str(project_root),
    )
    write_history(path, history)

    resolution = resolve_startup(
        project_root=project_root,
        handover_path=project_root / "current-handover-prompt.md",
        history_path=path,
    )
    assert resolution.use_first_prompt_for_cycle_one is False
    assert resolution.resumed is False


def test_handover_ready_rejects_task_id_zero(project_root: Path):
    handover = project_root / "current-handover-prompt.md"
    handover.write_text("# Task ID: 0\n\nQueue empty.\n", encoding="utf-8")
    assert handover_ready_for_implementation(handover) is False


def test_resolve_startup_uses_first_prompt_when_handover_missing(project_root: Path):
    path = _history_path(project_root)
    handover = project_root / "current-handover-prompt.md"
    handover.unlink()

    resolution = resolve_startup(
        project_root=project_root,
        handover_path=handover,
        history_path=path,
    )
    assert resolution.use_first_prompt_for_cycle_one is True


def test_validate_handover_against_history_mismatch_warns(project_root: Path):
    path = _history_path(project_root)
    history = _sample_history(project_root, task_id=8)
    handover_path = project_root / "current-handover-prompt.md"
    (handover_path).write_text("# Task ID: 99\n\nWrong.\n", encoding="utf-8")

    result = validate_handover_against_history(
        handover_path,
        history,
        history_path=path,
    )
    assert result.checked is True
    assert result.aligned is False
    assert result.handover_task_id == 99
    assert result.expected_handover_task_id == 8


def test_validate_handover_against_history_strict_does_not_raise(project_root: Path):
    """History mismatch is warn-only; handover updates always win over stale history."""
    path = _history_path(project_root)
    history = _sample_history(project_root, task_id=8)
    handover_path = project_root / "current-handover-prompt.md"
    handover_path.write_text("# Task ID: 99\n\nUpdated externally.\n", encoding="utf-8")

    result = validate_handover_against_history(
        handover_path,
        history,
        history_path=path,
        strict=True,
    )
    assert result.checked is True
    assert result.aligned is False
    assert result.handover_task_id == 99
    assert result.expected_handover_task_id == 8


def test_build_history_from_run(project_root: Path):
    snap = snapshot_handover(project_root / "current-handover-prompt.md")
    history = build_history_from_run(
        project_root=project_root,
        completed_task_ids=[12, 13],
        final_handover=snap,
    )
    assert history.bootstrap_complete is True
    assert history.completed_cycle_task_ids == [12, 13]
    assert history.last_handover_task_id == 13
    assert history.last_handover_content_hash == snap.content_hash


def test_format_history_summary_variants(project_root: Path):
    path = _history_path(project_root)
    missing = read_history(path)
    assert "No cyclopsctl run history file found" in format_history_summary(missing)

    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["array"]), encoding="utf-8")
    corrupt = read_history(path)
    assert "JSON object" in format_history_summary(corrupt)

    write_history(path, _sample_history(project_root))
    ok = read_history(path)
    summary = format_history_summary(ok)
    assert "Cyclopsctl run history" in summary
    assert "Bootstrap complete: True" in summary
    assert "Last handover task ID: 13" in summary


def test_build_run_config_history_file_default_and_disable(project_root: Path):
    base = {
        "cycles": 1,
        "project_root": project_root,
        "first_prompt": project_root / "first.md",
        "current_handover": project_root / "current.md",
        "update_handover": project_root / "update.md",
        "complexity_report": project_root / "report.json",
    }
    for name in ("first.md", "current.md", "update.md", "report.json"):
        (project_root / name).write_text("x", encoding="utf-8")

    cfg = build_run_config(base)
    assert cfg.history_file == default_history_path(project_root)
    assert cfg.fresh is False

    disabled = build_run_config({**base, "history_file": ""})
    assert disabled.history_file is None

    fresh = build_run_config({**base, "fresh": True})
    assert fresh.fresh is True


def test_build_status_config_includes_history_file(project_root: Path):
    cfg = build_status_config({"project_root": project_root})
    assert cfg.history_file == default_history_path(project_root)
