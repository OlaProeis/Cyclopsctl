"""Tests for task 4: idempotent project setup assessment."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cyclopsctl.project_setup import (
    LAST_PARSED_PRD_REL,
    PRD_CHANGED_MESSAGE,
    PRD_CONTINUE_CONFIRM_MESSAGE,
    ProjectSetupError,
    backfill_last_parsed_prd_if_missing,
    format_ready_message,
    load_last_parsed_prd,
    prd_changed_with_existing_tasks,
    repo_has_existing_content,
    resolve_project_setup_config,
    run_project_setup,
    sha256_file,
    tasks_exist_in_tag,
    write_last_parsed_prd,
)
from cyclopsctl.prompt import render_synced_handover
from cyclopsctl.tasks.types import TaskShowDetail
from cyclopsctl.tasks.store import save_tag_tasks, set_current_tag
from cyclopsctl.workflow_gen import (
    install_cyclopsctl_cursor_rules,
    install_cyclopsctl_skill,
)


def _sample_task(task_id: int = 1) -> dict:
    return {
        "id": task_id,
        "title": "First setup task",
        "description": "Do the thing.",
        "details": "Implement setup helpers.",
        "testStrategy": "Mock SDK calls and verify setup shape.",
        "priority": "high",
        "dependencies": [],
        "status": "pending",
        "subtasks": [],
        "complexity": 5,
    }


def _mock_native_parse_analyze(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parse_error: str | None = None,
) -> None:
    def fake_parse(
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        **_kwargs: object,
    ) -> None:
        if parse_error is not None:
            raise RuntimeError(parse_error)
        tasks_path = project_root / ".cyclopsctl" / "tasks" / "tasks.json"
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        save_tag_tasks(
            tasks_path,
            [_sample_task()],
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


@pytest.fixture
def greenfield_root(tmp_path: Path) -> Path:
    root = tmp_path / "greenfield"
    root.mkdir()
    (root / "prd.md").write_text(
        "# PRD: Greenfield Project\n\n## Tech stack\n\nPython 3.10+",
        encoding="utf-8",
    )
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    return root.resolve()


def _write_native_tasks_json(root: Path, *, task_ids: tuple[int, ...] = (1, 2)) -> None:
    tasks_path = root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(
        tasks_path,
        [_sample_task(task_id) for task_id in task_ids],
        tag="master",
        merge=False,
    )


def _write_synced_handover(root: Path, *, task_id: int = 1) -> None:
    task = TaskShowDetail(
        task_id=str(task_id),
        title="First setup task",
        description="Do the thing.",
        details="Implement setup helpers.",
        test_strategy="pytest",
        priority="high",
        dependencies=(),
        status="pending",
        complexity=5,
    )
    text = render_synced_handover(
        task=task,
        project_name="Greenfield Project",
        project_root=root,
        tech_stack="Python 3.10+",
        branch="master",
    )
    (root / "current-handover-prompt.md").write_text(text, encoding="utf-8")


def _write_healthy_project(root: Path) -> None:
    (root / "cyclopsctl.toml").write_text(
        'project_root = "./"\ncycles = 5\n',
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "cyclopsctl.toml\n.cyclopsctl/\n.env\n",
        encoding="utf-8",
    )
    _write_native_tasks_json(root)
    set_current_tag(root / ".cyclopsctl" / "tasks" / "state.json", "master")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}) + "\n",
        encoding="utf-8",
    )
    _write_synced_handover(root)
    (root / "ai-context.md").write_text("# AI Context\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text(
        "# Update Handover Instructions\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "index.md").write_text("# Docs Index\n", encoding="utf-8")
    write_last_parsed_prd(root, root / "prd.md", tag="master")
    install_cyclopsctl_cursor_rules(root, project_root_value=str(root.resolve()))
    install_cyclopsctl_skill(root, project_root_value=str(root.resolve()))


def test_resolve_project_setup_config_requires_directory(tmp_path: Path):
    missing = tmp_path / "missing"
    with pytest.raises(ProjectSetupError, match="not a directory"):
        resolve_project_setup_config(project_root=missing)


def test_missing_prd_fails_before_mutations(greenfield_root: Path):
    (greenfield_root / "prd.md").unlink()
    config = resolve_project_setup_config(project_root=greenfield_root)

    with pytest.raises(ProjectSetupError, match="PRD file not found"):
        run_project_setup(config)

    assert not (greenfield_root / "cyclopsctl.toml").exists()
    assert not (greenfield_root / ".cyclopsctl" / "tasks").exists()


def test_missing_env_fails_without_fix(greenfield_root: Path):
    (greenfield_root / ".env").unlink()
    config = resolve_project_setup_config(project_root=greenfield_root)

    with pytest.raises(ProjectSetupError, match="CURSOR_API_KEY is not set"):
        run_project_setup(config, env={})


def test_missing_env_fix_writes_stub(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (greenfield_root / ".env").unlink()
    config = resolve_project_setup_config(project_root=greenfield_root, fix_env=True)
    _mock_native_parse_analyze(monkeypatch)

    result = run_project_setup(config, env={})

    assert (greenfield_root / ".env").is_file()
    env_text = (greenfield_root / ".env").read_text(encoding="utf-8")
    assert "CURSOR_API_KEY=" in env_text
    assert "parse-prd" in result.repairs


def test_native_init_does_not_require_extra_api_keys(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    _mock_native_parse_analyze(monkeypatch)
    config = resolve_project_setup_config(project_root=greenfield_root)

    result = run_project_setup(config, env={})

    assert result.parsed_prd is True
    assert "parse-prd" in result.repairs


def test_fresh_repo_runs_full_setup_pipeline(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _mock_native_parse_analyze(monkeypatch)
    config = resolve_project_setup_config(project_root=greenfield_root)

    result = run_project_setup(config)

    assert result.parsed_prd is True
    assert result.already_ready is False
    assert "cyclopsctl.toml" in result.repairs
    assert ".gitignore" in result.repairs
    assert "native tasks init" in result.repairs
    assert "cyclopsctl rules" in result.repairs
    assert "cyclopsctl skill" in result.repairs
    assert "parse-prd" in result.repairs
    assert "analyze-complexity" in result.repairs
    assert "handover sync" in result.repairs
    assert (greenfield_root / "cyclopsctl.toml").is_file()
    assert (greenfield_root / "ai-context.md").is_file()
    assert (greenfield_root / "current-handover-prompt.md").is_file()
    assert "# Task ID: 1" in (
        greenfield_root / "current-handover-prompt.md"
    ).read_text(encoding="utf-8")

    last_parsed = greenfield_root / LAST_PARSED_PRD_REL
    assert last_parsed.is_file()
    state = load_last_parsed_prd(greenfield_root)
    assert state is not None
    assert state.path == "prd.md"
    assert state.tag == "master"
    assert state.sha256 == sha256_file(greenfield_root / "prd.md")
    assert state.parsed_at
    assert (greenfield_root / ".cyclopsctl" / "tasks" / "tasks.json").is_file()

    update_handover = (
        greenfield_root / "update-handover-prompt.md"
    ).read_text(encoding="utf-8")
    ai_context = (greenfield_root / "ai-context.md").read_text(encoding="utf-8")
    assert "cyclopsctl tasks set-status" in update_handover
    assert "cyclopsctl tasks" in ai_context
    assert (
        greenfield_root / ".cursor" / "rules" / "cyclopsctl" / "agent-workflow.mdc"
    ).is_file()


def test_partially_initialized_repo_repairs_missing_pieces(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (greenfield_root / ".cyclopsctl" / "tasks").mkdir(parents=True)
    _mock_native_parse_analyze(monkeypatch)
    config = resolve_project_setup_config(project_root=greenfield_root)

    result = run_project_setup(config)

    assert result.already_ready is False
    assert "cyclopsctl.toml" in result.repairs
    assert "parse-prd" in result.repairs
    assert "native tasks init" not in result.repairs


def test_healthy_repo_is_idempotent(greenfield_root: Path):
    _write_healthy_project(greenfield_root)
    config = resolve_project_setup_config(project_root=greenfield_root)

    result = run_project_setup(config)

    assert result.already_ready is True
    assert result.repairs == ()
    assert result.parsed_prd is False
    assert (greenfield_root / LAST_PARSED_PRD_REL).is_file()


def test_refresh_workflow_updates_only_stale_files(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write_healthy_project(greenfield_root)
    custom_ai = "# Custom AI\n\n## Implementation Phase Rules\n## Update Phase Rules\n"
    custom_ai += "cyclopsctl tasks list pending\n"
    (greenfield_root / "ai-context.md").write_text(custom_ai, encoding="utf-8")
    (greenfield_root / "update-handover-prompt.md").write_text(
        "Mark tasks done manually.\n",
        encoding="utf-8",
    )

    config = resolve_project_setup_config(
        project_root=greenfield_root,
        refresh_workflow=True,
    )
    result = run_project_setup(config)

    assert (greenfield_root / "ai-context.md").read_text(encoding="utf-8") == custom_ai
    refreshed_update = (
        greenfield_root / "update-handover-prompt.md"
    ).read_text(encoding="utf-8")
    assert "cyclopsctl tasks set-status" in refreshed_update
    assert any(
        repair.startswith("workflow refresh: update-handover-prompt.md")
        for repair in result.repairs
    )
    captured = capsys.readouterr()
    assert "Workflow refresh updated: update-handover-prompt.md" in captured.err
    assert "Workflow refresh skipped (not stale): ai-context.md" in captured.err


def test_mature_repo_without_last_parsed_backfills_on_init(greenfield_root: Path):
    _write_healthy_project(greenfield_root)
    (greenfield_root / LAST_PARSED_PRD_REL).unlink()
    config = resolve_project_setup_config(project_root=greenfield_root)

    result = run_project_setup(config)

    assert "last-parsed-prd" in result.repairs
    state = load_last_parsed_prd(greenfield_root)
    assert state is not None
    assert state.tag == "master"
    assert state.sha256 == sha256_file(greenfield_root / "prd.md")


def test_backfill_prefers_native_current_tag(greenfield_root: Path):
    _write_healthy_project(greenfield_root)
    (greenfield_root / LAST_PARSED_PRD_REL).unlink()
    tasks_path = greenfield_root / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "master": {"tasks": []},
                "phase-5": {
                    "tasks": [
                        {
                            "id": 1,
                            "title": "Phase task",
                            "status": "pending",
                            "priority": "high",
                            "dependencies": [],
                            "subtasks": [],
                        }
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    set_current_tag(greenfield_root / ".cyclopsctl" / "tasks" / "state.json", "phase-5")

    assert backfill_last_parsed_prd_if_missing(
        greenfield_root,
        greenfield_root / "prd.md",
    )

    state = load_last_parsed_prd(greenfield_root)
    assert state is not None
    assert state.tag == "phase-5"


def test_changed_prd_with_existing_tasks_refuses_reparse(greenfield_root: Path):
    _write_healthy_project(greenfield_root)
    (greenfield_root / "prd.md").write_text("# PRD: Updated scope\n", encoding="utf-8")
    config = resolve_project_setup_config(project_root=greenfield_root)

    with pytest.raises(ProjectSetupError, match=PRD_CHANGED_MESSAGE):
        run_project_setup(config)

    assert prd_changed_with_existing_tasks(greenfield_root, greenfield_root / "prd.md")


def test_last_parsed_written_only_after_successful_parse(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _mock_native_parse_analyze(monkeypatch, parse_error="parse-prd failed")
    config = resolve_project_setup_config(project_root=greenfield_root)

    with pytest.raises(ProjectSetupError, match="parse-prd failed"):
        run_project_setup(config)

    assert not (greenfield_root / LAST_PARSED_PRD_REL).exists()


def test_tasks_exist_in_tag_reads_tasks_json(greenfield_root: Path):
    assert tasks_exist_in_tag(greenfield_root) is False
    _write_native_tasks_json(greenfield_root, task_ids=(3,))
    assert tasks_exist_in_tag(greenfield_root) is True


@pytest.fixture
def brownfield_attach_root(tmp_path: Path) -> Path:
    root = tmp_path / "brownfield-attach"
    root.mkdir()
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    _write_native_tasks_json(root)
    set_current_tag(root / ".cyclopsctl" / "tasks" / "state.json", "master")
    report = root / ".cyclopsctl" / "reports" / "complexity-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"complexityAnalysis": [{"taskId": 1, "complexityScore": 5}]}) + "\n",
        encoding="utf-8",
    )
    return root.resolve()


def test_attach_mode_uses_readme_context_source(
    brownfield_attach_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    workflow_fixtures = (
        Path(__file__).resolve().parent / "fixtures" / "workflow_gen"
    )
    shutil.copy(workflow_fixtures / "python-readme.md", brownfield_attach_root / "README.md")
    (brownfield_attach_root / "pyproject.toml").write_text(
        "[project]\nname='api'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    config = resolve_project_setup_config(project_root=brownfield_attach_root)
    run_project_setup(config, stdin_is_tty=True)

    ai_context = (brownfield_attach_root / "ai-context.md").read_text(encoding="utf-8")
    assert "Python API Service - AI Context" in ai_context
    assert "Run `python -m pytest`" in ai_context
    assert "See prd.md" not in ai_context.split("## Tech Stack", 1)[1].split("##", 1)[0]

    captured = capsys.readouterr()
    assert "Workflow context source: README.md" in captured.err


def test_attach_mode_confirm_yes_repairs_without_parse(
    brownfield_attach_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    parse_calls: list[str] = []

    def track_parse(*_args, **_kwargs) -> None:
        parse_calls.append("parse")

    monkeypatch.setattr(
        "cyclopsctl.tasks.parse_prd.parse_prd_with_cursor",
        track_parse,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    config = resolve_project_setup_config(project_root=brownfield_attach_root)
    result = run_project_setup(config, stdin_is_tty=True)

    assert parse_calls == []
    assert "parse-prd" not in result.repairs
    assert "handover sync" in result.repairs
    assert "cyclopsctl.toml" in result.repairs
    assert result.attach_mode is True
    assert not (brownfield_attach_root / LAST_PARSED_PRD_REL).exists()
    handover = (brownfield_attach_root / "current-handover-prompt.md").read_text(
        encoding="utf-8"
    )
    assert "# Task ID: 1" in handover
    captured = capsys.readouterr()
    assert "Brownfield attach mode" in captured.err
    assert "PRD file not found" in captured.err
    assert "Workflow context source: repository metadata" in captured.err


def test_attach_mode_preserves_customized_ai_context(
    brownfield_attach_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    custom_ai = (
        "# Custom AI\n\n## Implementation Phase Rules\n## Update Phase Rules\n"
        "cyclopsctl tasks list pending\n"
    )
    (brownfield_attach_root / "ai-context.md").write_text(custom_ai, encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    config = resolve_project_setup_config(project_root=brownfield_attach_root)
    run_project_setup(config, stdin_is_tty=True)

    assert (
        brownfield_attach_root / "ai-context.md"
    ).read_text(encoding="utf-8") == custom_ai


def test_attach_mode_non_tty_requires_attach_yes(
    brownfield_attach_root: Path,
):
    config = resolve_project_setup_config(project_root=brownfield_attach_root)

    with pytest.raises(ProjectSetupError, match="--attach --yes"):
        run_project_setup(config, stdin_is_tty=False)

    assert not (brownfield_attach_root / "cyclopsctl.toml").exists()


def test_attach_mode_attach_yes_non_tty(
    brownfield_attach_root: Path,
    capsys: pytest.CaptureFixture[str],
):
    config = resolve_project_setup_config(
        project_root=brownfield_attach_root,
        attach=True,
        assume_yes=True,
    )
    result = run_project_setup(config, stdin_is_tty=False)

    assert result.attach_mode is True
    assert "handover sync" in result.repairs
    captured = capsys.readouterr()
    assert "Brownfield attach mode" in captured.err


def test_attach_mode_skipped_when_prd_present(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_healthy_project(greenfield_root)

    def fail_confirm(*_args, **_kwargs) -> bool:
        raise AssertionError("attach confirm must not run when PRD exists")

    monkeypatch.setattr(
        "cyclopsctl.project_setup._confirm_attach_continue",
        fail_confirm,
    )

    config = resolve_project_setup_config(project_root=greenfield_root)
    result = run_project_setup(config)

    assert result.attach_mode is False
    assert result.already_ready is True


@pytest.fixture
def prd_continue_root(tmp_path: Path) -> Path:
    root = tmp_path / "prd-continue"
    root.mkdir()
    (root / "prd.md").write_text(
        "# PRD: Existing App\n\n## Tech stack\n\nPython 3.10+",
        encoding="utf-8",
    )
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return root.resolve()


def test_repo_has_existing_content_detects_src_and_readme(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    assert repo_has_existing_content(root) is False

    (root / "src").mkdir()
    assert repo_has_existing_content(root) is True

    (root / "src").rmdir()
    (root / "README.md").write_text("# App\n", encoding="utf-8")
    assert repo_has_existing_content(root) is True

    (root / "README.md").unlink()
    (root / ".git").mkdir()
    assert repo_has_existing_content(root) is True


def test_prd_continue_mode_parses_and_preserves_custom_ai_context(
    prd_continue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    custom_ai = (
        "# Custom AI\n\n## Implementation Phase Rules\n## Update Phase Rules\n"
        "cyclopsctl tasks list pending\n"
    )
    (prd_continue_root / "ai-context.md").write_text(custom_ai, encoding="utf-8")
    _mock_native_parse_analyze(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    config = resolve_project_setup_config(project_root=prd_continue_root)
    result = run_project_setup(config, stdin_is_tty=True)

    assert result.continue_mode is True
    assert result.parsed_prd is True
    assert "parse-prd" in result.repairs
    assert "handover sync" in result.repairs
    assert (
        prd_continue_root / "ai-context.md"
    ).read_text(encoding="utf-8") == custom_ai
    assert "# Task ID: 1" in (
        prd_continue_root / "current-handover-prompt.md"
    ).read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "PRD continue mode" in captured.err
    assert "Cursor SDK" in captured.err


def test_prd_continue_mode_non_tty_requires_yes(prd_continue_root: Path):
    config = resolve_project_setup_config(project_root=prd_continue_root)

    with pytest.raises(ProjectSetupError, match=PRD_CONTINUE_CONFIRM_MESSAGE):
        run_project_setup(config, stdin_is_tty=False)

    assert not (prd_continue_root / "cyclopsctl.toml").exists()


def test_prd_continue_mode_yes_non_tty(
    prd_continue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _mock_native_parse_analyze(monkeypatch)
    config = resolve_project_setup_config(
        project_root=prd_continue_root,
        assume_yes=True,
    )
    result = run_project_setup(config, stdin_is_tty=False)

    assert result.continue_mode is True
    assert result.parsed_prd is True
    captured = capsys.readouterr()
    assert "PRD continue mode" in captured.err


def test_prd_continue_mode_skipped_when_tasks_exist(greenfield_root: Path):
    _write_healthy_project(greenfield_root)
    config = resolve_project_setup_config(project_root=greenfield_root)
    result = run_project_setup(config)

    assert result.continue_mode is False
    assert result.already_ready is True
    assert "parse-prd" not in result.repairs


def test_missing_prd_existing_repo_suggests_attach_remediation(tmp_path: Path):
    root = tmp_path / "no-prd-existing"
    root.mkdir()
    (root / "src").mkdir()
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    config = resolve_project_setup_config(project_root=root)

    with pytest.raises(ProjectSetupError, match="--attach --yes"):
        run_project_setup(config)


def test_prd_continue_from_custom_prd_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "custom-prd"
    root.mkdir()
    (root / "src").mkdir()
    (root / "docs").mkdir()
    custom_prd = root / "docs" / "product.md"
    custom_prd.write_text("# PRD: Custom Path\n", encoding="utf-8")
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    _mock_native_parse_analyze(monkeypatch)

    config = resolve_project_setup_config(
        project_root=root,
        prd_path=Path("docs/product.md"),
        assume_yes=True,
    )
    result = run_project_setup(config, stdin_is_tty=False)

    assert result.continue_mode is True
    assert result.parsed_prd is True
    state = load_last_parsed_prd(root)
    assert state is not None
    assert state.path == "docs/product.md"


def test_format_ready_message_includes_status():
    from cyclopsctl.project_setup import ProjectSetupResult

    result = ProjectSetupResult(
        already_ready=True,
        repairs=(),
        parsed_prd=False,
        task_id=1,
        pending_count=2,
        next_task_id=1,
        next_task_title="First setup task",
        tag="master",
    )
    message = format_ready_message(result)
    assert "Already ready." in message
    assert "Tag: master" in message
    assert "Pending tasks: 2" in message
    assert "Next task: 1 — First setup task" in message

    fresh = ProjectSetupResult(
        already_ready=False,
        repairs=("parse-prd",),
        parsed_prd=True,
        task_id=1,
        pending_count=1,
        next_task_id=1,
        next_task_title="First setup task",
        tag="master",
    )
    assert format_ready_message(fresh).startswith("Ready.")
