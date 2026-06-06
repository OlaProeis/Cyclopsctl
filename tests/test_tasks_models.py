"""Tests for task model config and auto-resolution (phase-5 task 6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from cursor_sdk import ModelSelection, SDKModel

from cyclopsctl.config import ConfigError, parse_tasks_config
from cyclopsctl.tasks.backend import DEFAULT_TASK_BACKEND
from cyclopsctl.tasks.models import (
    DEFAULT_ANALYZE_MODEL,
    DEFAULT_MAX_TASKS,
    DEFAULT_PARSE_MODEL,
    detect_sonnet_model,
    resolve_analyze_model,
    resolve_parse_model,
)

EXAMPLE_TOML = Path(__file__).resolve().parent.parent / "cyclopsctl.toml.example"


def test_parse_tasks_config_defaults_to_native_backend():
    cfg = parse_tasks_config({}, {})
    assert cfg.backend == DEFAULT_TASK_BACKEND
    assert cfg.parse_model == DEFAULT_PARSE_MODEL
    assert cfg.analyze_model == DEFAULT_ANALYZE_MODEL
    assert cfg.max_tasks == DEFAULT_MAX_TASKS


def test_parse_tasks_config_reads_tasks_section():
    file_cfg = {
        "tasks": {
            "backend": "native",
            "parse_model": "claude-sonnet-4",
            "analyze_model": "composer-2.5-fast",
            "max_tasks": 15,
        }
    }
    cfg = parse_tasks_config({}, file_cfg)
    assert cfg.backend == "native"
    assert cfg.parse_model == "claude-sonnet-4"
    assert cfg.analyze_model == "composer-2.5-fast"
    assert cfg.max_tasks == 15


def test_parse_tasks_config_reads_legacy_default_num_tasks():
    file_cfg = {"tasks": {"default_num_tasks": 12}}
    cfg = parse_tasks_config({}, file_cfg)
    assert cfg.max_tasks == 12


def test_parse_tasks_config_cli_overrides_file_section():
    file_cfg = {
        "tasks": {
            "backend": "native",
            "parse_model": "auto",
            "analyze_model": "auto",
            "max_tasks": 10,
        }
    }
    cli = {
        "task_backend": "native",
        "parse_model": "custom-parse",
        "analyze_model": "custom-analyze",
        "max_tasks": 7,
    }
    cfg = parse_tasks_config(cli, file_cfg)
    assert cfg.backend == "native"
    assert cfg.parse_model == "custom-parse"
    assert cfg.analyze_model == "custom-analyze"
    assert cfg.max_tasks == 7


def test_parse_tasks_config_rejects_invalid_max_tasks():
    with pytest.raises(ConfigError, match="max-tasks"):
        parse_tasks_config({}, {"tasks": {"max_tasks": 0}})


def test_example_toml_tasks_defaults_map_to_native_behavior():
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[no-redef]

    text = EXAMPLE_TOML.read_text(encoding="utf-8")
    assert "[tasks]" in text
    assert 'backend = "native"' in text
    assert 'parse_model = "auto"' in text
    assert 'analyze_model = "auto"' in text
    assert "default_num_tasks" not in text

    data = tomllib.loads(text)
    cfg = parse_tasks_config({}, data)
    assert cfg.backend == "native"
    assert cfg.parse_model == "auto"
    assert cfg.analyze_model == "auto"
    assert cfg.max_tasks is None


def test_detect_sonnet_model_prefers_claude_sonnet():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer", description="", variants=[]),
        SDKModel(
            id="claude-sonnet-4",
            display_name="Claude Sonnet 4",
            description="",
            variants=[],
        ),
    ]
    selection = detect_sonnet_model(models)
    assert selection is not None
    assert selection.id == "claude-sonnet-4"


def test_resolve_parse_model_explicit_id():
    selection = resolve_parse_model("my-custom-model")
    assert selection == ModelSelection(id="my-custom-model")


def test_resolve_parse_model_auto_prefers_composer_for_local_runtime():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5", description="", variants=[]),
        SDKModel(
            id="claude-sonnet-4",
            display_name="Claude Sonnet",
            description="",
            variants=[],
        ),
    ]

    def list_models(**_kwargs: object) -> list[SDKModel]:
        return models

    selection = resolve_parse_model("auto", list_models=list_models)
    assert selection.id == "composer-2.5"


def test_resolve_parse_model_auto_defaults_to_composer_when_only_sonnet_listed():
    models = [
        SDKModel(
            id="claude-sonnet-4",
            display_name="Claude Sonnet",
            description="",
            variants=[],
        )
    ]

    def list_models(**_kwargs: object) -> list[SDKModel]:
        return models

    selection = resolve_parse_model("auto", list_models=list_models)
    assert selection.id == "composer-2.5"


def test_resolve_analyze_model_explicit_id():
    selection = resolve_analyze_model("my-analyze-model")
    assert selection == ModelSelection(id="my-analyze-model")


def test_resolve_analyze_model_auto_uses_composer_listing():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5", description="", variants=[]),
    ]

    def list_models(**_kwargs: object) -> list[SDKModel]:
        return models

    selection = resolve_analyze_model("auto", list_models=list_models)
    assert selection.id == "composer-2.5"
