"""Integration tests for task 5: cyclopsctl init delegates to project setup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.cli import main
from tests.test_project_setup import (
    _mock_native_parse_analyze,
    _write_healthy_project,
)


@pytest.fixture
def greenfield_root(tmp_path: Path) -> Path:
    root = tmp_path / "greenfield"
    root.mkdir()
    (root / "prd.md").write_text(
        "# PRD: Init Integration\n\n## Tech stack\n\nPython 3.10+",
        encoding="utf-8",
    )
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def mocked_native_init(greenfield_root: Path, monkeypatch: pytest.MonkeyPatch):
    del greenfield_root
    _mock_native_parse_analyze(monkeypatch)
    yield


def test_cli_init_fresh_repo_prepares_for_launch(
    greenfield_root: Path,
    mocked_native_init,
    capsys,
):
    code = main(["init", "--project-root", str(greenfield_root)])
    assert code == 0
    captured = capsys.readouterr()
    assert "cyclopsctl launch" in captured.err
    assert "Pending tasks:" in captured.err
    assert "Next task:" in captured.err
    assert (greenfield_root / "cyclopsctl.toml").is_file()
    assert (greenfield_root / "current-handover-prompt.md").is_file()
    assert "# Task ID: 1" in (
        greenfield_root / "current-handover-prompt.md"
    ).read_text(encoding="utf-8")
    assert (greenfield_root / ".cyclopsctl" / "tasks" / "tasks.json").is_file()


def test_cli_init_ready_repo_is_non_destructive(
    greenfield_root: Path,
    capsys,
):
    _write_healthy_project(greenfield_root)
    custom_ai = "# Custom AI context\n"
    (greenfield_root / "ai-context.md").write_text(custom_ai, encoding="utf-8")

    code = main(["init", "--project-root", str(greenfield_root)])
    assert code == 0
    captured = capsys.readouterr()
    assert "Already ready." in captured.err
    assert "cyclopsctl launch" in captured.err
    assert (greenfield_root / "ai-context.md").read_text(encoding="utf-8") == custom_ai


def test_cli_init_does_not_prompt_for_cycles(
    greenfield_root: Path,
    mocked_native_init,
    monkeypatch,
):
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        raise AssertionError("init must not prompt interactively")

    monkeypatch.setattr("builtins.input", fake_input)

    code = main(["init", "--project-root", str(greenfield_root)])
    assert code == 0
    assert prompts == []


def test_cli_init_never_dispatches_run_or_launch_cycles(
    greenfield_root: Path,
    mocked_native_init,
    monkeypatch,
):
    dispatched: list[list[str]] = []

    def fake_dispatch(argv, parser):
        dispatched.append(argv)
        return 0

    monkeypatch.setattr("cyclopsctl.cli._dispatch_launch_argv", fake_dispatch)

    code = main(["init", "--project-root", str(greenfield_root)])
    assert code == 0
    assert dispatched == []


def test_cli_init_missing_prd_fails_early(greenfield_root: Path, capsys):
    (greenfield_root / "prd.md").unlink()
    code = main(["init", "--project-root", str(greenfield_root)])
    assert code != 0
    captured = capsys.readouterr()
    assert "PRD file not found" in captured.err
    assert not (greenfield_root / "cyclopsctl.toml").exists()


def test_cli_init_missing_api_key_fails_early(greenfield_root: Path, capsys, monkeypatch):
    (greenfield_root / ".env").unlink()
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    code = main(["init", "--project-root", str(greenfield_root), "--no-env"])
    assert code != 0
    captured = capsys.readouterr()
    assert "CURSOR_API_KEY is not set" in captured.err


def test_cli_init_dry_run_lists_plan_without_writes(
    greenfield_root: Path,
    capsys,
):
    code = main(
        ["init", "--project-root", str(greenfield_root), "--dry-run", "--no-env"]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "Dry run" in captured.err
    assert "cyclopsctl.toml" in captured.err
    assert not (greenfield_root / "cyclopsctl.toml").exists()


def test_cli_init_skip_templates_only_writes_config(
    greenfield_root: Path,
    mocked_native_init,
):
    code = main(
        [
            "init",
            "--project-root",
            str(greenfield_root),
            "--skip-templates",
        ]
    )
    assert code == 0
    assert (greenfield_root / "cyclopsctl.toml").is_file()
    assert not (greenfield_root / ".gitignore").exists()


def test_cli_init_profile_applied_to_config(
    greenfield_root: Path,
    mocked_native_init,
    capsys,
):
    code = main(
        [
            "init",
            "--project-root",
            str(greenfield_root),
            "--profile",
            "solo-default",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "Applied profile: solo-default" in captured.err
    content = (greenfield_root / "cyclopsctl.toml").read_text(encoding="utf-8")
    assert 'task_source = "handover"' in content


def test_cli_init_repeat_run_does_not_rewrite_customized_workflow_without_force(
    greenfield_root: Path,
    mocked_native_init,
):
    main(["init", "--project-root", str(greenfield_root)])
    custom = "# Custom workflow context\n"
    (greenfield_root / "ai-context.md").write_text(custom, encoding="utf-8")

    code = main(["init", "--project-root", str(greenfield_root)])
    assert code == 0
    assert (greenfield_root / "ai-context.md").read_text(encoding="utf-8") == custom
