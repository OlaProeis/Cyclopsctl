"""Tests for task 18: configuration profile merge and CLI wiring."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cyclopsctl.cli import _build_parser, main
from cyclopsctl.config import ConfigError, load_run_config
from cyclopsctl.init_scaffold import InitScaffoldError, run_init_scaffold
from cyclopsctl.profiles import (
    ProfileError,
    list_profile_names,
    merge_effective_file_config,
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


def _base_toml(root: Path) -> str:
    return textwrap.dedent(
        f"""
        cycles = 3
        project_root = "{root.as_posix()}"
        first_prompt = "prompts/first.md"
        current_handover = "current-handover-prompt.md"
        update_handover = "update-handover-prompt.md"
        task_source = "handover"
        retry_on = "off"
        """
    )


def test_profile_absent_matches_baseline_behavior(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        _base_toml(project_tree)
        + textwrap.dedent(
            """
            [profile.daytime-fast]
            cycles = 9
            retry_on = "transient"
            """
        ),
        encoding="utf-8",
    )
    baseline = load_run_config(config_path=toml_path)
    with_profiles_section = load_run_config(config_path=toml_path, profile=None)
    assert baseline.cycles == with_profiles_section.cycles == 3
    assert baseline.retry_on == with_profiles_section.retry_on == "off"
    assert baseline.task_source == with_profiles_section.task_source == "handover"


def test_profile_inherits_base_values_and_overrides_selected_fields(
    project_tree: Path, tmp_path: Path
):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        _base_toml(project_tree)
        + textwrap.dedent(
            """
            [profile.daytime-fast]
            cycles = 7
            retry_on = "transient"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path, profile="daytime-fast")
    assert cfg.cycles == 7
    assert cfg.retry_on == "transient"
    assert cfg.task_source == "handover"


def test_cli_overrides_profile_values(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        _base_toml(project_tree)
        + textwrap.dedent(
            """
            [profile.daytime-fast]
            cycles = 7
            retry_on = "transient"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(
        config_path=toml_path,
        profile="daytime-fast",
        cycles=2,
        retry_on="off",
    )
    assert cfg.cycles == 2
    assert cfg.retry_on == "off"


def test_unknown_profile_raises(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        _base_toml(project_tree)
        + textwrap.dedent(
            """
            [profile.daytime-fast]
            cycles = 7
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Unknown profile: 'missing'"):
        load_run_config(config_path=toml_path, profile="missing")


def test_profile_without_config_raises():
    with pytest.raises(ConfigError, match="Cannot use --profile without --config"):
        load_run_config(profile="daytime-fast", cycles=1)


def test_profile_routing_and_retry_settings(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        _base_toml(project_tree)
        + textwrap.dedent(
            """
            [routing_profile.fast-default]
            composer_tier = "fast"
            opus_enabled = false

            [profile.low-credits]
            routing_profile = "fast-default"
            retry_on = "transient"
            retry_max_attempts = 4
            task_source = "handover"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path, profile="low-credits")
    assert cfg.retry_on == "transient"
    assert cfg.retry_max_attempts == 4
    assert cfg.task_source == "handover"
    assert cfg.routing is not None
    assert cfg.routing.composer_tier == "fast"
    assert cfg.routing.opus_enabled is False


def test_profile_top_level_routing_overrides_routing_profile(
    project_tree: Path, tmp_path: Path
):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        _base_toml(project_tree)
        + textwrap.dedent(
            """
            [routing_profile.fast-default]
            composer_tier = "fast"
            opus_enabled = false

            [profile.custom]
            routing_profile = "fast-default"
            opus_enabled = true
            """
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(config_path=toml_path, profile="custom")
    assert cfg.routing is not None
    assert cfg.routing.composer_tier == "fast"
    assert cfg.routing.opus_enabled is True


def test_merge_effective_file_config_lists_available_profiles():
    raw = {
        "cycles": 1,
        "profile": {"alpha": {"cycles": 2}, "beta": {"cycles": 3}},
    }
    assert list_profile_names(raw) == ["alpha", "beta"]


def test_unknown_routing_profile_raises():
    raw = {
        "profile": {
            "broken": {"routing_profile": "missing-preset"},
        }
    }
    with pytest.raises(ProfileError, match="Unknown routing profile"):
        merge_effective_file_config(raw, "broken")


def test_cli_run_parser_accepts_profile_flag():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "run",
            "--config",
            "cyclopsctl.toml",
            "--profile",
            "daytime-fast",
            "--cycles",
            "1",
            "--project-root",
            "G:/DEV/Example",
            "--first-prompt",
            "first.md",
            "--current-handover",
            "current.md",
            "--update-handover",
            "update.md",
        ]
    )
    assert args.profile == "daytime-fast"


def test_cli_init_parser_accepts_profile_flag():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "init",
            "--project-root",
            "G:/DEV/Example",
            "--profile",
            "solo-default",
        ]
    )
    assert args.profile == "solo-default"


def test_init_scaffold_applies_profile_defaults(project_tree: Path):
    result = run_init_scaffold(
        project_root=project_tree,
        profile="daytime-fast",
        skip_templates=True,
    )
    assert result.profile == "daytime-fast"
    assert result.config_path.is_file()
    content = result.config_path.read_text(encoding="utf-8")
    assert "retry_on = \"transient\"" in content
    assert "[profile.daytime-fast]" in content
    assert "[routing_profile.fast-default]" in content

    (project_tree / "prompts" / "setup-ai-workflow.md").write_text(
        "# Bootstrap\n", encoding="utf-8"
    )
    cfg = load_run_config(config_path=result.config_path, profile="daytime-fast")
    assert cfg.retry_on == "transient"
    assert cfg.routing is not None
    assert cfg.routing.composer_tier == "fast"


def test_init_scaffold_refuses_existing_config(project_tree: Path):
    config_path = project_tree / "cyclopsctl.toml"
    config_path.write_text("cycles = 1\n", encoding="utf-8")
    with pytest.raises(InitScaffoldError, match="Refusing to overwrite"):
        run_init_scaffold(
            project_root=project_tree,
            config_path=config_path,
            skip_templates=True,
        )


def test_cli_init_command_writes_profile_aware_config(
    tmp_path: Path, capsys, monkeypatch
):
    project_root = tmp_path / "init-cli"
    project_root.mkdir()
    (project_root / "prd.md").write_text("# PRD\n", encoding="utf-8")
    (project_root / ".env").write_text("CURSOR_API_KEY=test\n", encoding="utf-8")

    from tests.test_project_setup import _mock_native_parse_analyze

    _mock_native_parse_analyze(monkeypatch)

    code = main(
        [
            "init",
            "--project-root",
            str(project_root.resolve()),
            "--profile",
            "solo-default",
        ]
    )
    assert code == 0
    config_path = project_root / "cyclopsctl.toml"
    assert config_path.is_file()
    captured = capsys.readouterr()
    assert "Applied profile: solo-default" in captured.err

    content = config_path.read_text(encoding="utf-8")
    assert 'task_source = "handover"' in content
    assert "cycles = 5" in content
