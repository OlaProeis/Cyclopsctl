"""Integration tests for task 12: brownfield runbook and regression fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.cli import main
from cyclopsctl.config import build_doctor_config
from cyclopsctl.doctor import run_diagnostics
from cyclopsctl.history import load_completed_task_ids, read_history
from cyclopsctl.loop import _resolve_runnable_task
from cyclopsctl.logging import CycleLogger
from cyclopsctl.project_setup import (
    LAST_PARSED_PRD_REL,
    handle_launch_prd_change,
    prd_hash_changed,
    run_project_setup,
    resolve_project_setup_config,
)
from cyclopsctl.prompt import parse_task_id, read_prompt_text
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.tasks.backend import (
    TaskBackendConfig,
    detect_task_backend_from_storage,
    get_task_backend,
)
from cyclopsctl.tasks.store import load_tasks_document

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BROWNFIELD_RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "guides" / "testing-guide.md"

SCENARIO_FIXTURES = {
    "B1-mid-backlog": FIXTURES / "brownfield-mid-backlog",
    "B3-prd-change": FIXTURES / "brownfield-prd-change",
    "B4-resume": FIXTURES / "brownfield-resume",
    "B5-no-prd": FIXTURES / "brownfield-no-prd",
    "B6-prd-only": FIXTURES / "brownfield-prd-only",
    "B7-customized-workflow": FIXTURES / "brownfield-customized-workflow",
}

FIXTURES_REQUIRING_PRD = {
    name: path
    for name, path in SCENARIO_FIXTURES.items()
    if name != "B5-no-prd"
}

CHECKLIST_STEPS = (
    "CURSOR_API_KEY",
    "current-handover-prompt.md",
    "cyclopsctl doctor",
    "cyclopsctl launch",
    "list pending",
    "implement",
    "update",
    "verify",
)


@pytest.fixture
def brownfield_repo(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Copy a named brownfield fixture into an isolated temp directory."""
    fixture_name = request.param
    source = SCENARIO_FIXTURES[fixture_name]
    root = tmp_path / fixture_name
    shutil.copytree(source, root)
    return root.resolve()


def _native_doctor_config(project_root: Path):
    return build_doctor_config(
        {
            "project_root": project_root,
            "task_backend": "native",
        }
    )


def _native_backend():
    return get_task_backend(TaskBackendConfig(task_backend="native"))


def _pending_ids(project_root: Path) -> list[int]:
    backend = _native_backend()
    pending = backend.list_pending(project_root)
    return [task.numeric_id for task in pending]


def _task_statuses(project_root: Path) -> dict[int, str]:
    document = load_tasks_document(project_root / ".cyclopsctl" / "tasks" / "tasks.json")
    tag = document.get("master") or next(iter(document.values()))
    tasks = tag.get("tasks", []) if isinstance(tag, dict) else []
    return {int(task["id"]): str(task["status"]) for task in tasks}


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
                    "title": "Phase 5 task 1",
                    "description": "New scope",
                    "details": "From PRD change.",
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


def test_brownfield_fixtures_exist() -> None:
    for name, path in SCENARIO_FIXTURES.items():
        assert path.is_dir(), f"missing fixture directory for {name}"
        assert (path / ".env").is_file()
    for name, path in FIXTURES_REQUIRING_PRD.items():
        assert (path / "prd.md").is_file(), f"{name} should include prd.md"
    assert not (SCENARIO_FIXTURES["B5-no-prd"] / "prd.md").exists()
    assert (SCENARIO_FIXTURES["B5-no-prd"] / "README.md").is_file()
    assert (SCENARIO_FIXTURES["B6-prd-only"] / "src").is_dir()
    assert (SCENARIO_FIXTURES["B7-customized-workflow"] / "ai-context.md").is_file()
    assert (SCENARIO_FIXTURES["B7-customized-workflow"] / "docs" / "index.md").is_file()


def test_brownfield_runbook_documents_checklist_and_scenarios() -> None:
    text = BROWNFIELD_RUNBOOK.read_text(encoding="utf-8")
    for step in CHECKLIST_STEPS:
        assert step.lower() in text.lower(), f"runbook missing checklist item: {step}"
    for scenario in (
        "B1",
        "mid-backlog",
        "B2",
        "PRD change",
        "B3",
        "Resume",
        "B4",
        "no PRD",
        "B5",
        "PRD exists, no tasks",
        "B6",
        "Customized workflow",
        "--attach",
        "--from-prd",
    ):
        assert scenario in text, f"runbook missing scenario reference: {scenario}"
    assert "tests/test_brownfield_integration.py" in text
    assert "tests/fixtures/brownfield-" in text
    assert "brownfield-no-prd" in text
    assert "brownfield-prd-only" in text
    assert "brownfield-customized-workflow" in text


def test_brownfield_runbook_default_path_uses_native_tasks() -> None:
    text = BROWNFIELD_RUNBOOK.read_text(encoding="utf-8")
    part_a_end = text.lower().find("part b")
    default_section = text if part_a_end == -1 else text[:part_a_end]
    assert "cyclopsctl tasks" in default_section.lower()


@pytest.mark.parametrize("brownfield_repo", ["B1-mid-backlog"], indirect=True)
def test_b1_mid_backlog_queue_and_handover_state(brownfield_repo: Path) -> None:
    handover_text = read_prompt_text(brownfield_repo / "current-handover-prompt.md")
    handover_id = parse_task_id(handover_text)
    assert handover_id == 6

    statuses = _task_statuses(brownfield_repo)
    assert statuses[5] == "done"
    assert statuses[6] == "pending"
    assert all(statuses[task_id] == "done" for task_id in range(1, 6))

    pending_ids = _pending_ids(brownfield_repo)
    assert pending_ids == [6, 7, 8]
    assert handover_id in pending_ids

    next_lookup = _native_backend().get_next(brownfield_repo)
    assert next_lookup.found is True
    assert next_lookup.task is not None
    assert next_lookup.task.numeric_id == 6


@pytest.mark.parametrize("brownfield_repo", ["B1-mid-backlog"], indirect=True)
def test_b1_doctor_passes_native_checks(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "brownfield-test-key")
    with patch("cyclopsctl.doctor.needs_windows_bridge_bootstrap", return_value=False), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=False,
    ):
        checks = run_diagnostics(_native_doctor_config(brownfield_repo))

    check_names = [check.name for check in checks]
    assert "Native tasks" in check_names
    assert "Complexity report" in check_names
    assert all(check.passed for check in checks)


@pytest.mark.parametrize("brownfield_repo", ["B1-mid-backlog"], indirect=True)
def test_b1_launch_doctor_preflight_mocked(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "brownfield-test-key")

    with patch(
        "cyclopsctl.doctor.needs_windows_bridge_bootstrap",
        return_value=False,
    ), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=True,
    ):
        code = main(
            [
                "launch",
                "--project-root",
                str(brownfield_repo),
                "--plain",
                "--yes",
                "--action",
                "doctor",
            ]
        )

    captured = capsys.readouterr()
    assert code == 0
    assert "All checks passed" in captured.err
    assert "Handover task ID: 6" in captured.err
    assert "Pending tasks: 3" in captured.err


@pytest.mark.parametrize("brownfield_repo", ["B3-prd-change"], indirect=True)
def test_b3_prd_change_detection(brownfield_repo: Path) -> None:
    prd_path = brownfield_repo / "prd.md"
    assert prd_hash_changed(brownfield_repo, prd_path) is True

    last_parsed = json.loads(
        (brownfield_repo / ".cyclopsctl" / "last-parsed-prd.json").read_text(
            encoding="utf-8"
        )
    )
    assert last_parsed["tag"] == "master"
    assert last_parsed["sha256"] != __import__("hashlib").sha256(
        prd_path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("brownfield_repo", ["B3-prd-change"], indirect=True)
def test_b3_launch_prd_change_creates_new_tag_mocked(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_native_prd_change(monkeypatch)

    result = handle_launch_prd_change(
        brownfield_repo,
        current_handover=brownfield_repo / "current-handover-prompt.md",
        complexity_report=brownfield_repo / ".cyclopsctl" / "reports" / "complexity-report.json",
        ai_context=brownfield_repo / "ai-context.md",
        assume_yes=True,
    )

    assert result is not None
    assert result.new_tag_created is True
    assert result.active_tag
    assert "parse-prd" in result.steps

    document = load_tasks_document(
        brownfield_repo / ".cyclopsctl" / "tasks" / "tasks.json"
    )
    assert result.active_tag in document
    new_tasks = document[result.active_tag]["tasks"]
    assert new_tasks[0]["title"] == "Phase 5 task 1"


@pytest.mark.parametrize("brownfield_repo", ["B4-resume"], indirect=True)
def test_b4_resume_history_and_handover_alignment(brownfield_repo: Path) -> None:
    history_path = brownfield_repo / ".cyclopsctl" / "run-history.json"
    read_result = read_history(history_path)
    assert read_result.kind == "ok"
    assert read_result.history is not None
    assert read_result.history.completed_cycle_task_ids == [1, 2, 3, 4, 5]

    completed = load_completed_task_ids(history_path)
    assert completed == frozenset({1, 2, 3, 4, 5})

    handover_id = parse_task_id(
        read_prompt_text(brownfield_repo / "current-handover-prompt.md")
    )
    assert handover_id == 6
    assert handover_id not in completed

    next_lookup = _native_backend().get_next(brownfield_repo)
    assert next_lookup.task is not None
    assert next_lookup.task.numeric_id == 6


@pytest.mark.parametrize("brownfield_repo", ["B4-resume"], indirect=True)
def test_b4_resume_skips_completed_tasks_in_loop(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cyclopsctl.config import build_run_config

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    config = build_run_config(
        {
            "cycles": 1,
            "project_root": brownfield_repo,
            "current_handover": brownfield_repo / "current-handover-prompt.md",
            "update_handover": brownfield_repo / "update-handover-prompt.md",
            "complexity_report": brownfield_repo
            / ".cyclopsctl"
            / "reports"
            / "complexity-report.json",
            "task_source": "handover",
            "resume": True,
            "history_file": brownfield_repo / ".cyclopsctl" / "run-history.json",
            "task_backend": "native",
        }
    )

    completed = load_completed_task_ids(config.history_file)

    def resolve_selected(
        project_root: Path,
        *,
        tag: str | None = None,
        exclude_task_ids: set[int] | None = None,
    ) -> NextTaskLookup:
        del project_root, tag
        exclude = exclude_task_ids or set()
        for task_id in (1, 2, 3, 4, 5, 6):
            if task_id in exclude:
                continue
            return NextTaskLookup.from_task(
                NextTaskResult(
                    task_id=str(task_id),
                    title=f"Task {task_id}",
                    status="pending" if task_id >= 6 else "done",
                    priority="medium",
                    complexity=5,
                    tag="master",
                ),
                tag="master",
            )
        return NextTaskLookup.empty(tag="master")

    lookup, skipped = _resolve_runnable_task(
        config,
        resolve_selected=resolve_selected,
        completed_task_ids=completed,
        resume_exclude=set(),
        log=CycleLogger(),
        cycle_number=1,
    )

    assert [task.task_id for task in skipped] == [1, 2, 3, 4, 5]
    assert lookup.found is True
    assert lookup.task is not None
    assert lookup.task.numeric_id == 6


def _mock_native_parse_analyze(monkeypatch: pytest.MonkeyPatch) -> None:
    from cyclopsctl.tasks.store import save_tag_tasks

    def fake_parse(
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        **_kwargs: object,
    ) -> None:
        tasks_path = project_root / ".cyclopsctl" / "tasks" / "tasks.json"
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        save_tag_tasks(
            tasks_path,
            [
                {
                    "id": 1,
                    "title": "Continue init task",
                    "description": "Parsed from PRD",
                    "details": "Implement.",
                    "testStrategy": "pytest",
                    "priority": "high",
                    "dependencies": [],
                    "status": "pending",
                    "subtasks": [],
                    "complexity": 5,
                }
            ],
            tag=tag or "master",
            merge=append,
        )

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


@pytest.mark.parametrize("brownfield_repo", ["B5-no-prd"], indirect=True)
def test_b5_no_prd_fixture_state(brownfield_repo: Path) -> None:
    assert not (brownfield_repo / "prd.md").exists()
    assert not (brownfield_repo / "cyclopsctl.toml").exists()
    assert not (brownfield_repo / "current-handover-prompt.md").exists()

    pending_ids = _pending_ids(brownfield_repo)
    assert pending_ids == [2, 3]
    assert _task_statuses(brownfield_repo)[1] == "done"


@pytest.mark.parametrize("brownfield_repo", ["B5-no-prd"], indirect=True)
def test_b5_attach_init_then_launch_doctor_mocked(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    parse_calls: list[str] = []

    def track_parse(*_args, **_kwargs) -> None:
        parse_calls.append("parse")

    monkeypatch.setattr(
        "cyclopsctl.tasks.parse_prd.parse_prd_with_cursor",
        track_parse,
    )
    monkeypatch.setenv("CURSOR_API_KEY", "brownfield-test-key")

    init_code = main(
        [
            "init",
            "--project-root",
            str(brownfield_repo),
            "--attach",
            "--yes",
        ]
    )
    init_captured = capsys.readouterr()
    assert init_code == 0
    assert parse_calls == []
    assert "Brownfield attach mode" in init_captured.err
    assert "Workflow context source: README.md" in init_captured.err
    assert (brownfield_repo / "cyclopsctl.toml").is_file()
    assert not (brownfield_repo / LAST_PARSED_PRD_REL).exists()
    handover_id = parse_task_id(
        read_prompt_text(brownfield_repo / "current-handover-prompt.md")
    )
    assert handover_id == 2

    with patch(
        "cyclopsctl.doctor.needs_windows_bridge_bootstrap",
        return_value=False,
    ), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=True,
    ):
        launch_code = main(
            [
                "launch",
                "--project-root",
                str(brownfield_repo),
                "--plain",
                "--yes",
                "--action",
                "doctor",
            ]
        )

    launch_captured = capsys.readouterr()
    assert launch_code == 0
    assert "All checks passed" in launch_captured.err
    assert "Handover task ID: 2" in launch_captured.err


@pytest.mark.parametrize("brownfield_repo", ["B6-prd-only"], indirect=True)
def test_b6_prd_only_fixture_state(brownfield_repo: Path) -> None:
    assert (brownfield_repo / "prd.md").is_file()
    assert (brownfield_repo / "src").is_dir()
    assert not (brownfield_repo / ".cyclopsctl" / "tasks" / "tasks.json").exists()
    assert not (brownfield_repo / "cyclopsctl.toml").exists()


@pytest.mark.parametrize("brownfield_repo", ["B6-prd-only"], indirect=True)
def test_b6_continue_init_then_launch_doctor_mocked(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    _mock_native_parse_analyze(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "brownfield-test-key")

    init_code = main(
        [
            "init",
            "--project-root",
            str(brownfield_repo),
            "--yes",
        ]
    )
    init_captured = capsys.readouterr()
    assert init_code == 0
    assert "PRD continue mode" in init_captured.err
    assert (brownfield_repo / "cyclopsctl.toml").is_file()
    assert (brownfield_repo / LAST_PARSED_PRD_REL).is_file()
    handover_id = parse_task_id(
        read_prompt_text(brownfield_repo / "current-handover-prompt.md")
    )
    assert handover_id == 1

    with patch(
        "cyclopsctl.doctor.needs_windows_bridge_bootstrap",
        return_value=False,
    ), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=True,
    ):
        launch_code = main(
            [
                "launch",
                "--project-root",
                str(brownfield_repo),
                "--plain",
                "--yes",
                "--action",
                "doctor",
            ]
        )

    launch_captured = capsys.readouterr()
    assert launch_code == 0
    assert "All checks passed" in launch_captured.err
    assert "Handover task ID: 1" in launch_captured.err


@pytest.mark.parametrize("brownfield_repo", ["B7-customized-workflow"], indirect=True)
def test_b7_customized_workflow_fixture_state(brownfield_repo: Path) -> None:
    ai_text = (brownfield_repo / "ai-context.md").read_text(encoding="utf-8")
    index_text = (brownfield_repo / "docs" / "index.md").read_text(encoding="utf-8")
    assert "B7-CUSTOM-AI-MARKER" in ai_text
    assert "B7-CUSTOM-INDEX-MARKER" in index_text
    assert parse_task_id(
        read_prompt_text(brownfield_repo / "current-handover-prompt.md")
    ) == 1


@pytest.mark.parametrize("brownfield_repo", ["B7-customized-workflow"], indirect=True)
def test_b7_init_preserves_customized_workflow_files(
    brownfield_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_before = (brownfield_repo / "ai-context.md").read_text(encoding="utf-8")
    index_before = (brownfield_repo / "docs" / "index.md").read_text(encoding="utf-8")
    update_before = (
        brownfield_repo / "update-handover-prompt.md"
    ).read_text(encoding="utf-8")

    config = resolve_project_setup_config(project_root=brownfield_repo)
    result = run_project_setup(config)

    assert result.attach_mode is False
    assert result.continue_mode is False
    assert "parse-prd" not in result.repairs
    assert "ai-context.md" not in result.repairs
    assert "docs/index.md" not in result.repairs
    assert (brownfield_repo / "ai-context.md").read_text(encoding="utf-8") == ai_before
    assert (
        brownfield_repo / "docs" / "index.md"
    ).read_text(encoding="utf-8") == index_before
    assert (
        brownfield_repo / "update-handover-prompt.md"
    ).read_text(encoding="utf-8") == update_before
