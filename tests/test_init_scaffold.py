"""Tests for task 20: project scaffold command with starter files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cyclopsctl.cli import main
from cyclopsctl.init_scaffold import (
    GITIGNORE_ENTRIES,
    InitScaffoldError,
    discover_scaffold_plan,
    run_init_scaffold,
)


@pytest.fixture
def greenfield_root(tmp_path: Path) -> Path:
    root = tmp_path / "greenfield"
    root.mkdir()
    return root.resolve()


def test_discover_scaffold_plan_lists_config_templates_and_gitignore(greenfield_root: Path):
    planned = discover_scaffold_plan(project_root=greenfield_root)
    assert "cyclopsctl.toml" in planned
    assert "ai-context.md" in planned
    assert "current-handover-prompt.md" in planned
    assert "update-handover-prompt.md" in planned
    assert ".gitignore" in planned


def test_discover_scaffold_plan_skip_templates_only_config(greenfield_root: Path):
    planned = discover_scaffold_plan(project_root=greenfield_root, skip_templates=True)
    assert planned == ("cyclopsctl.toml",)


def test_dry_run_does_not_write_files(greenfield_root: Path):
    result = run_init_scaffold(project_root=greenfield_root, dry_run=True)
    assert result.dry_run is True
    assert result.planned_paths
    assert not (greenfield_root / "cyclopsctl.toml").exists()
    assert not (greenfield_root / "ai-context.md").exists()


def test_run_init_scaffold_writes_config_and_templates(greenfield_root: Path):
    result = run_init_scaffold(project_root=greenfield_root)
    assert result.config_path.is_file()
    assert (greenfield_root / "ai-context.md").is_file()
    assert (greenfield_root / "current-handover-prompt.md").is_file()
    assert (greenfield_root / "update-handover-prompt.md").is_file()
    assert "cyclopsctl.toml" in result.written_paths
    assert "ai-context.md" in result.written_paths


def test_template_content_smoke_checks(greenfield_root: Path):
    run_init_scaffold(project_root=greenfield_root)
    ai_context = (greenfield_root / "ai-context.md").read_text(encoding="utf-8")
    current = (greenfield_root / "current-handover-prompt.md").read_text(encoding="utf-8")
    update = (greenfield_root / "update-handover-prompt.md").read_text(encoding="utf-8")

    assert "# AI Context" in ai_context
    assert "Context7 MCP" in ai_context
    assert "# Task ID: 0" in current
    assert "Task queue not loaded" in current
    assert str(greenfield_root) in current
    assert "cyclopsctl tasks" in current
    assert "# Update Handover Instructions" in update
    assert "cyclopsctl tasks set-status" in update
    assert "cyclopsctl tasks" in ai_context


def test_gitignore_inserts_orchestrator_entries(greenfield_root: Path):
    run_init_scaffold(project_root=greenfield_root)
    content = (greenfield_root / ".gitignore").read_text(encoding="utf-8")
    for entry in GITIGNORE_ENTRIES:
        assert entry in content


def test_gitignore_appends_missing_entries_without_duplicates(greenfield_root: Path):
    gitignore = greenfield_root / ".gitignore"
    gitignore.write_text(".env\n", encoding="utf-8")
    run_init_scaffold(project_root=greenfield_root)
    content = gitignore.read_text(encoding="utf-8")
    assert content.count(".env") == 1
    assert "cyclopsctl.toml" in content
    assert ".cyclopsctl/" in content


def test_skip_templates_only_writes_config(greenfield_root: Path):
    result = run_init_scaffold(project_root=greenfield_root, skip_templates=True)
    assert result.written_paths == ("cyclopsctl.toml",)
    assert not (greenfield_root / "ai-context.md").exists()
    assert not (greenfield_root / ".gitignore").exists()


def test_refuses_overwrite_without_force(greenfield_root: Path):
    run_init_scaffold(project_root=greenfield_root)
    with pytest.raises(InitScaffoldError, match="Refusing to overwrite"):
        run_init_scaffold(project_root=greenfield_root)


def test_force_single_path_overwrites_only_that_file(greenfield_root: Path):
    run_init_scaffold(project_root=greenfield_root)
    original_update = (greenfield_root / "update-handover-prompt.md").read_text(
        encoding="utf-8"
    )
    (greenfield_root / "ai-context.md").write_text("# Custom\n", encoding="utf-8")

    with pytest.raises(InitScaffoldError, match="Refusing to overwrite"):
        run_init_scaffold(
            project_root=greenfield_root,
            force_paths=["ai-context.md"],
        )

    result = run_init_scaffold(
        project_root=greenfield_root,
        force_paths=["cyclopsctl.toml"],
        skip_templates=True,
    )
    assert result.written_paths == ("cyclopsctl.toml",)
    assert (greenfield_root / "ai-context.md").read_text(encoding="utf-8") == "# Custom\n"
    assert (
        greenfield_root / "update-handover-prompt.md"
    ).read_text(encoding="utf-8") == original_update


def test_force_all_overwrites_existing_targets(greenfield_root: Path):
    run_init_scaffold(project_root=greenfield_root)
    (greenfield_root / "ai-context.md").write_text("# Stale\n", encoding="utf-8")

    run_init_scaffold(project_root=greenfield_root, force_all=True)
    content = (greenfield_root / "ai-context.md").read_text(encoding="utf-8")
    assert "# Stale" not in content
    assert "# AI Context" in content


def test_cli_init_delegates_to_project_setup(
    greenfield_root: Path, capsys, monkeypatch, tmp_path: Path
):
    """Init CLI uses assess-and-setup; see test_init_integration for full matrix."""
    root = tmp_path / "init-delegate"
    root.mkdir()
    (root / "prd.md").write_text("# PRD\n", encoding="utf-8")
    (root / ".env").write_text("CURSOR_API_KEY=test\n", encoding="utf-8")

    from tests.test_project_setup import _mock_native_parse_analyze

    _mock_native_parse_analyze(monkeypatch)
    code = main(["init", "--project-root", str(root)])

    assert code == 0
    captured = capsys.readouterr()
    assert "cyclopsctl launch" in captured.err
    assert (root / "cyclopsctl.toml").is_file()
