"""Tests for multi-phase PRD continuation: path-aware launch --prd, bootstrap
guard, tags CLI, and phase-complete nudges."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclopsctl.bootstrap import (
    BootstrapConfig,
    BootstrapError,
    _guard_destructive_reparse,
    resolve_bootstrap_config,
)
from cyclopsctl.launcher import format_phase_complete_hint
from cyclopsctl.project_setup import (
    LAST_PARSED_PRD_REL,
    ProjectSetupResult,
    format_ready_message,
    handle_launch_prd_change,
    list_existing_tags,
    load_last_parsed_prd,
    prd_source_changed,
    sha256_file,
    write_last_parsed_prd,
)
from cyclopsctl.tasks.cli import (
    TasksCliError,
    list_all_tags,
    switch_tag,
)
from cyclopsctl.tasks.store import save_tag_tasks, set_current_tag


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _seed_tasks(root: Path, *, tag: str, task_ids: tuple[int, ...]) -> None:
    path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {
            "id": task_id,
            "title": f"Task {task_id}",
            "description": "desc",
            "details": "details",
            "testStrategy": "pytest",
            "priority": "high",
            "dependencies": [],
            "status": "pending",
            "subtasks": [],
            "complexity": 5,
        }
        for task_id in task_ids
    ]
    save_tag_tasks(path, tasks, tag=tag, merge=True)
    set_current_tag(path.parent / "state.json", tag)


def _write_initialized_project(
    root: Path,
    *,
    prd_name: str = "prd.md",
    prd_text: str = "# PRD: Phase 1\n",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("CURSOR_API_KEY=test\n", encoding="utf-8")
    (root / prd_name).write_text(prd_text, encoding="utf-8")
    (root / "cyclopsctl.toml").write_text("cycles = 1\n", encoding="utf-8")
    (root / ".cyclopsctl" / "reports").mkdir(parents=True, exist_ok=True)
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}',
        encoding="utf-8",
    )
    (root / "ai-context.md").write_text("# AI Context\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    _seed_tasks(root, tag="master", task_ids=(1, 2))
    write_last_parsed_prd(root, root / prd_name, tag="master")


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
            json.dumps({"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}) + "\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr("cyclopsctl.tasks.parse_prd.parse_prd_with_cursor", fake_parse)
    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor", fake_analyze
    )


def _change_kwargs(root: Path) -> dict:
    return {
        "current_handover": root / "current-handover-prompt.md",
        "complexity_report": root / ".cyclopsctl" / "reports" / "complexity-report.json",
        "ai_context": root / "ai-context.md",
    }


# --------------------------------------------------------------------------- #
# Path-aware PRD change
# --------------------------------------------------------------------------- #


def test_prd_source_changed_detects_path_and_hash(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)

    # Same file, same content -> unchanged.
    assert prd_source_changed(root, root / "prd.md") is False

    # Different file path -> changed even if content is unrelated.
    (root / "prd-phase2.md").write_text("# PRD: Phase 2\n", encoding="utf-8")
    assert prd_source_changed(root, root / "prd-phase2.md") is True

    # Same file, edited content -> changed.
    (root / "prd.md").write_text("# PRD: Phase 1\n\nMore.\n", encoding="utf-8")
    assert prd_source_changed(root, root / "prd.md") is True


def test_explicit_new_prd_file_creates_new_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    # prd.md is unchanged; a brand-new phase file drives the new tag.
    (root / "prd-phase2.md").write_text("# PRD: Phase 2\n\nNext scope.\n", encoding="utf-8")
    _mock_native_prd_change(monkeypatch)

    result = handle_launch_prd_change(
        root,
        prd_path=root / "prd-phase2.md",
        assume_yes=True,
        task_backend="native",
        **_change_kwargs(root),
    )

    assert result is not None
    assert result.new_tag_created is True
    assert result.active_tag == "phase-2"
    assert "master" in list_existing_tags(root)
    assert "phase-2" in list_existing_tags(root)

    last = load_last_parsed_prd(root)
    assert last is not None
    assert last.tag == "phase-2"
    assert last.path == "prd-phase2.md"


def test_bare_launch_after_phase_switch_does_not_retrigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd-phase2.md").write_text("# PRD: Phase 2\n", encoding="utf-8")
    _mock_native_prd_change(monkeypatch)

    handle_launch_prd_change(
        root,
        prd_path=root / "prd-phase2.md",
        assume_yes=True,
        task_backend="native",
        **_change_kwargs(root),
    )

    # Bare launch (no --prd) must keep tracking prd-phase2.md (the current tag's
    # PRD), not default back to prd.md and spuriously create a third tag.
    result = handle_launch_prd_change(
        root,
        assume_yes=True,
        task_backend="native",
        **_change_kwargs(root),
    )
    assert result is None
    assert list_existing_tags(root) == frozenset({"master", "phase-2"})


# --------------------------------------------------------------------------- #
# Bootstrap destructive re-parse guard
# --------------------------------------------------------------------------- #


def _bootstrap_config(root: Path, *, prd: str = "prd.md", tag=None, append=False) -> BootstrapConfig:
    return resolve_bootstrap_config(
        project_root=root,
        from_prd=root / prd,
        tag=tag,
        append=append,
    )


def test_guard_raises_on_changed_prd_with_existing_tasks(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd.md").write_text("# PRD: Phase 1\n\nReworked.\n", encoding="utf-8")

    with pytest.raises(BootstrapError, match="cyclopsctl launch --prd"):
        _guard_destructive_reparse(_bootstrap_config(root))


def test_guard_allows_unchanged_prd(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    # No exception when the PRD is unchanged.
    _guard_destructive_reparse(_bootstrap_config(root))


def test_guard_allows_new_empty_tag(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd.md").write_text("# PRD: Phase 1\n\nReworked.\n", encoding="utf-8")
    # Targeting a tag that has no tasks yet is safe (fresh phase).
    _guard_destructive_reparse(_bootstrap_config(root, tag="phase-2"))


def test_run_bootstrap_blocks_destructive_reparse(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    (root / "prd.md").write_text("# PRD: Phase 1\n\nReworked.\n", encoding="utf-8")

    class _ExplodingBackend:
        def parse_prd(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("parse_prd should not run when guard blocks")

        def init_project(self, *_a, **_k):  # pragma: no cover
            raise AssertionError("init_project should not run when guard blocks")

    from cyclopsctl.bootstrap import run_bootstrap

    with pytest.raises(BootstrapError, match="Re-parsing would replace them"):
        run_bootstrap(_bootstrap_config(root), backend=_ExplodingBackend())


# --------------------------------------------------------------------------- #
# Tags CLI
# --------------------------------------------------------------------------- #


def test_list_all_tags_counts_and_active(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    # Mark one master task done, add a second tag.
    path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["master"]["tasks"][0]["status"] = "done"
    document["phase-2"] = {"tasks": [{"id": 1, "title": "X", "status": "pending"}]}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    active, summaries = list_all_tags(root)
    assert active == "master"
    by_name = {summary.name: summary for summary in summaries}
    assert by_name["master"].total == 2
    assert by_name["master"].done == 1
    assert by_name["master"].pending == 1
    assert by_name["master"].active is True
    assert by_name["phase-2"].active is False


def test_switch_tag_validates_and_switches(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["phase-2"] = {"tasks": []}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    switched = switch_tag(root, "phase-2")
    assert switched == "phase-2"
    active, _ = list_all_tags(root)
    assert active == "phase-2"


def test_switch_tag_unknown_raises(tmp_path: Path):
    root = tmp_path / "repo"
    _write_initialized_project(root)
    with pytest.raises(TasksCliError, match="tag not found"):
        switch_tag(root, "does-not-exist")


def test_tasks_tags_cli_json(tmp_path: Path, capsys):
    from cyclopsctl.cli import main

    root = tmp_path / "repo"
    _write_initialized_project(root)

    exit_code = main(
        ["tasks", "tags", "--project-root", str(root), "--format", "json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["activeTag"] == "master"
    assert any(tag["name"] == "master" for tag in payload["tags"])


# --------------------------------------------------------------------------- #
# Phase-complete nudges
# --------------------------------------------------------------------------- #


def test_format_phase_complete_hint_mentions_tag_and_prd():
    hint = format_phase_complete_hint("master")
    assert "master" in hint
    assert "--prd" in hint
    assert "tasks tags" in hint


def test_ready_message_phase_complete_hint():
    result = ProjectSetupResult(
        already_ready=True,
        repairs=(),
        parsed_prd=False,
        task_id=None,
        pending_count=0,
        next_task_id=None,
        next_task_title=None,
        tag="master",
    )
    message = format_ready_message(result)
    assert "queue complete" in message
    assert "--prd" in message
