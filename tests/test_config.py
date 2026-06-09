"""Tests for task 2: CLI configuration and argument parsing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cyclopsctl.cli import main
from cyclopsctl.config import (
    ConfigError,
    build_doctor_config,
    build_run_config,
    build_status_config,
    load_run_config,
    resolve_project_root,
)


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "prompts" / "first.md").write_text("# First\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        "{}", encoding="utf-8"
    )
    return root.resolve()


def _base_cli(root: Path) -> dict:
    return {
        "cycles": 3,
        "project_root": root,
        "first_prompt": root / "prompts" / "first.md",
        "current_handover": root / "current-handover-prompt.md",
        "update_handover": root / "update-handover-prompt.md",
    }


def test_build_run_config_valid_cli(project_tree: Path):
    cfg = build_run_config(_base_cli(project_tree))
    assert cfg.cycles == 3
    assert cfg.project_root == project_tree
    assert cfg.first_prompt.name == "first.md"
    assert cfg.default_model == "composer-2.5"
    assert cfg.complexity_report.name == "complexity-report.json"
    assert cfg.ai_context == (project_tree / "ai-context.md").resolve()
    assert cfg.require_ai_context is False
    assert cfg.ai_context_max_chars == 100_000


def test_missing_cycles_raises(project_tree: Path):
    cli = _base_cli(project_tree)
    del cli["cycles"]
    with pytest.raises(ConfigError, match="cycles"):
        build_run_config(cli)


def test_relative_project_root_resolves_against_cwd(project_tree: Path, monkeypatch):
    monkeypatch.chdir(project_tree)
    cli = _base_cli(project_tree)
    cli["project_root"] = Path(".")
    cfg = build_run_config(cli)
    assert cfg.project_root == project_tree


def test_missing_project_root_defaults_to_cwd(project_tree: Path, monkeypatch):
    monkeypatch.chdir(project_tree)
    cli = _base_cli(project_tree)
    del cli["project_root"]
    cfg = build_run_config(cli)
    assert cfg.project_root == project_tree


def test_resolve_project_root_none_uses_cwd(project_tree: Path, monkeypatch):
    monkeypatch.chdir(project_tree)
    assert resolve_project_root(None) == project_tree


def test_build_doctor_config_defaults_project_root_to_cwd(project_tree: Path, monkeypatch):
    monkeypatch.chdir(project_tree)
    cfg = build_doctor_config({})
    assert cfg.project_root == project_tree


def test_build_status_config_defaults_project_root_to_cwd(project_tree: Path, monkeypatch):
    monkeypatch.chdir(project_tree)
    cfg = build_status_config({})
    assert cfg.project_root == project_tree


def test_missing_first_prompt_allowed_at_config_load(project_tree: Path):
    cli = _base_cli(project_tree)
    cli["first_prompt"] = project_tree / "missing.md"
    cfg = build_run_config(cli)
    assert cfg.first_prompt == (project_tree / "missing.md").resolve()


def test_config_file_with_cli_override(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 9
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            tag = "master"
            default_model = "composer-2.5"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(
        config_path=toml_path,
        cycles=2,
        project_root=None,
        first_prompt=None,
        current_handover=None,
        update_handover=None,
    )
    assert cfg.cycles == 2
    assert cfg.tag == "master"


def test_plain_from_toml_when_cli_flag_not_set(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 1
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            plain = true
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path)
    assert cfg.plain is True


def test_cli_plain_overrides_toml(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 1
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            plain = false
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path, plain=True)
    assert cfg.plain is True


def test_strict_handover_defaults_false(project_tree: Path):
    cfg = build_run_config(_base_cli(project_tree))
    assert cfg.strict_handover is False


def test_task_source_defaults_handover(project_tree: Path):
    cfg = build_run_config(_base_cli(project_tree))
    assert cfg.task_source == "handover"
    assert cfg.pinned_task_id is None


def test_task_source_from_toml(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 1
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            task_source = "sequential"
            task_id = 12
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path)
    assert cfg.task_source == "sequential"
    assert cfg.pinned_task_id == 12


def test_strict_handover_from_toml(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 1
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            strict_handover = true
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path)
    assert cfg.strict_handover is True


def test_cli_strict_handover_overrides_toml(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 1
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            strict_handover = false
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path, strict_handover=True)
    assert cfg.strict_handover is True


def test_relative_paths_in_toml_resolve_to_project_root(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        textwrap.dedent(
            f"""
            cycles = 1
            project_root = "{project_tree.as_posix()}"
            first_prompt = "prompts/first.md"
            current_handover = "current-handover-prompt.md"
            update_handover = "update-handover-prompt.md"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path)
    assert cfg.first_prompt == (project_tree / "prompts" / "first.md").resolve()


def test_default_complexity_report_under_project_root(project_tree: Path):
    cli = _base_cli(project_tree)
    cfg = build_run_config(cli)
    assert cfg.complexity_report == (
        project_tree / ".cyclopsctl/reports/complexity-report.json"
    ).resolve()


def test_cli_run_valid_args(project_tree: Path, monkeypatch):
    from unittest.mock import patch

    from cyclopsctl.loop import RunLoopResult

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    with patch("cyclopsctl.cli.run_cycles") as mock_run:
        mock_run.return_value = RunLoopResult(completed_cycles=1)
        code = main(
            [
                "run",
                "--cycles",
                "1",
                "--project-root",
                str(project_tree),
                "--first-prompt",
                str(project_tree / "prompts" / "first.md"),
                "--current-handover",
                str(project_tree / "current-handover-prompt.md"),
                "--update-handover",
                str(project_tree / "update-handover-prompt.md"),
            ]
        )
    assert code == 0
    mock_run.assert_called_once()


def test_cli_run_missing_required_exits_nonzero(project_tree: Path):
    code = main(
        [
            "run",
            "--project-root",
            str(project_tree),
        ]
    )
    assert code == 2


def test_require_ai_context_missing_file_raises(project_tree: Path):
    cli = _base_cli(project_tree)
    cli["require_ai_context"] = True
    with pytest.raises(ConfigError, match="ai-context not found"):
        build_run_config(cli)


def test_custom_ai_context_path_resolves(project_tree: Path):
    custom = project_tree / "docs" / "context.md"
    custom.parent.mkdir()
    custom.write_text("# Custom\n", encoding="utf-8")
    cli = _base_cli(project_tree)
    cli["ai_context"] = Path("docs/context.md")
    cfg = build_run_config(cli)
    assert cfg.ai_context == custom.resolve()


def test_retry_defaults_transient(project_tree: Path):
    cfg = build_run_config(_base_cli(project_tree))
    assert cfg.retry_on == "transient"
    assert cfg.retry_transient_enabled is True
    assert cfg.retry_max_attempts == 3
    assert cfg.retry_backoff_seconds == (5, 15)


def test_retry_off_from_cli(project_tree: Path):
    cli = _base_cli(project_tree)
    cli["retry_on"] = "off"
    cfg = build_run_config(cli)
    assert cfg.retry_on == "off"
    assert cfg.retry_transient_enabled is False


def test_retry_on_transient_from_cli(project_tree: Path):
    cli = _base_cli(project_tree)
    cli["retry_on"] = "transient"
    cli["retry_max_attempts"] = 4
    cli["retry_backoff_seconds"] = "10,20,30"
    cfg = build_run_config(cli)
    assert cfg.retry_on == "transient"
    assert cfg.retry_transient_enabled is True
    assert cfg.retry_max_attempts == 4
    assert cfg.retry_backoff_seconds == (10, 20, 30)


def test_invalid_retry_on_raises(project_tree: Path):
    cli = _base_cli(project_tree)
    cli["retry_on"] = "always"
    with pytest.raises(ConfigError, match="retry-on"):
        build_run_config(cli)


def test_cli_run_invalid_project_root_exits_nonzero():
    code = main(
        [
            "run",
            "--cycles",
            "1",
            "--project-root",
            "relative/root",
            "--first-prompt",
            "a.md",
            "--current-handover",
            "b.md",
            "--update-handover",
            "c.md",
        ]
    )
    assert code == 2
