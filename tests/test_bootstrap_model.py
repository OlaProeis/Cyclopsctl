"""Tests for init bootstrap model selection and fallback."""

from __future__ import annotations

from cursor_sdk import ModelParameterValue, ModelSelection, ModelVariant, SDKModel

import pytest

from cyclopsctl.runner import AgentRunError, RunFailureKind
from cyclopsctl.tasks.bootstrap_model import (
    BOOTSTRAP_PRESET_AUTO,
    BOOTSTRAP_PRESET_COMPOSER,
    BOOTSTRAP_PRESET_FABLE,
    BOOTSTRAP_PRESET_GROK,
    BOOTSTRAP_PRESET_OPUS,
    BOOTSTRAP_PRESET_OPUS_MAX,
    BOOTSTRAP_PRESET_SONNET_MAX,
    bootstrap_model_attempt_chain,
    bootstrap_preset_names,
    normalize_bootstrap_preset,
    persist_bootstrap_tasks_settings,
    prompt_bootstrap_model_choice,
    resolve_bootstrap_tasks_settings,
    should_fallback_bootstrap_model,
    BootstrapTasksSettings,
)


def _models() -> list[SDKModel]:
    return [
        SDKModel(id="composer-2.5", display_name="Composer 2.5", description="", variants=[]),
        SDKModel(
            id="claude-sonnet-4-6",
            display_name="Claude Sonnet 4.6",
            description="",
            variants=(
                ModelVariant(
                    display_name="Default",
                    params=(),
                    is_default=True,
                ),
                ModelVariant(
                    display_name="Max Mode",
                    params=(ModelParameterValue(id="context", value="1m"),),
                ),
            ),
        ),
        SDKModel(
            id="claude-opus-4-8",
            display_name="Claude Opus 4.8",
            description="",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
                ModelVariant(
                    display_name="Max Mode",
                    params=(
                        ModelParameterValue(id="context", value="1m"),
                        ModelParameterValue(id="thinking", value="true"),
                        ModelParameterValue(id="effort", value="xhigh"),
                    ),
                ),
            ),
        ),
        SDKModel(
            id="claude-opus-4-8-thinking-high",
            display_name="Opus 4.8 High Thinking",
            description="",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
        SDKModel(
            id="claude-fable-5-thinking-high",
            display_name="Fable 5 High Thinking",
            description="",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
        SDKModel(
            id="grok-4.5",
            display_name="Cursor Grok 4.5",
            description="",
            variants=(
                ModelVariant(
                    display_name="Standard high",
                    params=(
                        ModelParameterValue(id="effort", value="high"),
                        ModelParameterValue(id="fast", value="false"),
                    ),
                ),
                ModelVariant(
                    display_name="Fast high",
                    params=(
                        ModelParameterValue(id="effort", value="high"),
                        ModelParameterValue(id="fast", value="true"),
                    ),
                    is_default=True,
                ),
            ),
        ),
    ]


def test_normalize_bootstrap_preset_aliases():
    assert normalize_bootstrap_preset("composer") == BOOTSTRAP_PRESET_COMPOSER
    assert normalize_bootstrap_preset("AUTO") == BOOTSTRAP_PRESET_AUTO
    assert normalize_bootstrap_preset("grok") == BOOTSTRAP_PRESET_GROK
    assert normalize_bootstrap_preset("grok-4.5") == BOOTSTRAP_PRESET_GROK
    assert normalize_bootstrap_preset("opus") == BOOTSTRAP_PRESET_OPUS
    assert normalize_bootstrap_preset("sonnet-max") == BOOTSTRAP_PRESET_SONNET_MAX
    assert normalize_bootstrap_preset("opus-max") == BOOTSTRAP_PRESET_OPUS_MAX
    assert normalize_bootstrap_preset("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_bootstrap_preset_names_includes_new_presets():
    names = bootstrap_preset_names()
    assert BOOTSTRAP_PRESET_GROK in names
    assert BOOTSTRAP_PRESET_FABLE in names
    assert BOOTSTRAP_PRESET_OPUS in names
    assert BOOTSTRAP_PRESET_SONNET_MAX in names
    assert BOOTSTRAP_PRESET_OPUS_MAX in names


def test_bootstrap_model_attempt_chain_auto_tries_sonnet_then_composer():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "auto",
        api_key="key",
        list_models=list_models,
    )
    assert [model.id for model in chain] == ["claude-sonnet-4-6", "composer-2.5"]


def test_bootstrap_model_attempt_chain_grok_falls_back_to_sonnet_then_composer():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "grok",
        api_key="key",
        list_models=list_models,
    )
    assert chain[0].id == "grok-4.5"
    assert ("fast", "false") in {(p.id, p.value) for p in chain[0].params}
    assert [model.id for model in chain[1:]] == [
        "claude-sonnet-4-6",
        "composer-2.5",
    ]


def test_bootstrap_model_attempt_chain_fable_falls_back_to_sonnet_then_composer():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "fable-high-thinking",
        api_key="key",
        list_models=list_models,
    )
    assert [model.id for model in chain] == [
        "claude-fable-5-thinking-high",
        "claude-sonnet-4-6",
        "composer-2.5",
    ]


def test_bootstrap_model_attempt_chain_composer_only():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "composer",
        api_key="key",
        list_models=list_models,
    )
    assert [model.id for model in chain] == ["composer-2.5"]


def test_bootstrap_model_attempt_chain_opus_falls_back_to_sonnet_then_composer():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "opus-high-thinking",
        api_key="key",
        list_models=list_models,
    )
    assert [model.id for model in chain] == [
        "claude-opus-4-8-thinking-high",
        "claude-sonnet-4-6",
        "composer-2.5",
    ]


def test_bootstrap_model_attempt_chain_sonnet_max_prefers_max_then_sonnet():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "sonnet-max",
        api_key="key",
        list_models=list_models,
    )
    assert chain[0] == ModelSelection(
        id="claude-sonnet-4-6",
        params=(ModelParameterValue(id="context", value="1m"),),
    )
    assert [model.id for model in chain] == [
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
        "composer-2.5",
    ]


def test_bootstrap_model_attempt_chain_opus_max_prefers_max_then_opus_ht():
    def list_models(**_kwargs: object) -> list[SDKModel]:
        return _models()

    chain = bootstrap_model_attempt_chain(
        "opus-max",
        api_key="key",
        list_models=list_models,
    )
    assert chain[0].params == (
        ModelParameterValue(id="context", value="1m"),
        ModelParameterValue(id="thinking", value="true"),
        ModelParameterValue(id="effort", value="xhigh"),
    )
    assert chain[0].id == "claude-opus-4-8"
    assert chain[1].id in {"claude-opus-4-8", "claude-opus-4-8-thinking-high"}
    assert [model.id for model in chain[2:]] == ["claude-sonnet-4-6", "composer-2.5"]


def test_should_fallback_bootstrap_model_on_run_error():
    exc = AgentRunError(
        "parse-prd run failed",
        kind=RunFailureKind.RUN,
        exit_code=2,
    )
    assert should_fallback_bootstrap_model(
        exc,
        attempted=ModelSelection(id="claude-sonnet-4-6"),
        remaining=[ModelSelection(id="composer-2.5")],
    )


def test_should_not_fallback_from_composer():
    exc = AgentRunError(
        "parse-prd run failed",
        kind=RunFailureKind.RUN,
        exit_code=2,
    )
    assert not should_fallback_bootstrap_model(
        exc,
        attempted=ModelSelection(id="composer-2.5"),
        remaining=[ModelSelection(id="claude-sonnet-4-6")],
    )


def test_prompt_bootstrap_model_choice_non_tty_defaults_auto(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert prompt_bootstrap_model_choice() == BOOTSTRAP_PRESET_AUTO


def test_prompt_bootstrap_model_choice_reads_selection(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    assert prompt_bootstrap_model_choice() == BOOTSTRAP_PRESET_COMPOSER


def test_prompt_bootstrap_model_choice_reads_grok_selection(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "4")
    assert prompt_bootstrap_model_choice() == BOOTSTRAP_PRESET_GROK


def test_prompt_bootstrap_model_choice_reads_fable_selection(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "5")
    assert prompt_bootstrap_model_choice() == BOOTSTRAP_PRESET_FABLE


def test_prompt_bootstrap_model_choice_reads_opus_max_selection(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "8")
    assert prompt_bootstrap_model_choice() == BOOTSTRAP_PRESET_OPUS_MAX


def test_resolve_bootstrap_tasks_settings_cli_bootstrap_model():
    settings = resolve_bootstrap_tasks_settings(
        bootstrap_model="composer",
        parse_model=None,
        analyze_model=None,
        max_tasks=None,
        file_cfg={},
        interactive=False,
    )
    assert settings == BootstrapTasksSettings(
        parse_model="composer",
        analyze_model="composer",
        max_tasks=None,
    )


def test_resolve_bootstrap_tasks_settings_uses_toml_when_set():
    settings = resolve_bootstrap_tasks_settings(
        bootstrap_model=None,
        parse_model=None,
        analyze_model=None,
        max_tasks=None,
        file_cfg={"tasks": {"parse_model": "composer", "analyze_model": "composer"}},
        interactive=False,
    )
    assert settings.parse_model == "composer"
    assert settings.analyze_model == "composer"


def test_persist_bootstrap_tasks_settings_updates_toml(tmp_path):
    config_path = tmp_path / "cyclopsctl.toml"
    config_path.write_text(
        "[tasks]\nbackend = \"native\"\nparse_model = \"auto\"\n"
        "analyze_model = \"auto\"\nmax_tasks = 10\n",
        encoding="utf-8",
    )
    persist_bootstrap_tasks_settings(
        config_path,
        BootstrapTasksSettings(
            parse_model="opus-high-thinking",
            analyze_model="opus-high-thinking",
            max_tasks=12,
        ),
    )
    text = config_path.read_text(encoding="utf-8")
    assert 'parse_model = "opus-high-thinking"' in text
    assert 'analyze_model = "opus-high-thinking"' in text
    assert 'max_tasks = "12"' in text or "max_tasks = 12" in text
