"""Tests for task 6: Cursor SDK model discovery helper."""

from __future__ import annotations

import pytest
from cursor_sdk import (
    CursorAgentError,
    ModelParameterValue,
    ModelSelection,
    ModelVariant,
    SDKModel,
)

from cyclopsctl.models import (
    COMPOSER_MODEL_ID,
    ModelCapabilities,
    ModelListingError,
    detect_composer,
    detect_fable_high_thinking,
    detect_opus_high_thinking,
    detect_opus_max,
    detect_sonnet_max,
    discover_model_capabilities,
    fetch_model_inventory,
    format_models_diagnostic,
)


def _opus_models() -> list[SDKModel]:
    return [
        SDKModel(
            id="claude-opus-4-8-thinking-high",
            display_name="Opus 4.8 High Thinking",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
        SDKModel(id="composer-2.5", display_name="Composer 2.5"),
    ]


def test_detect_fable_high_thinking_prefers_high_variant():
    models = [
        SDKModel(
            id="claude-fable-5",
            display_name="Fable 5",
            variants=(
                ModelVariant(
                    display_name="Default",
                    params=(),
                    is_default=True,
                ),
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
        SDKModel(
            id="claude-fable-5-thinking-high",
            display_name="Fable 5 High Thinking",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
    ]
    selection = detect_fable_high_thinking(models)
    assert selection is not None
    assert selection.id == "claude-fable-5-thinking-high"


def test_detect_fable_excludes_max_mode():
    models = [
        SDKModel(id="claude-fable-5-max", display_name="Fable 5 Max"),
        SDKModel(
            id="claude-fable-5-thinking-high",
            display_name="Fable 5 High Thinking",
            variants=(
                ModelVariant(
                    display_name="High",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
    ]
    selection = detect_fable_high_thinking(models)
    assert selection is not None
    assert selection.id == "claude-fable-5-thinking-high"


def test_detect_opus_prefers_high_thinking_over_fast():
    models = [
        SDKModel(
            id="claude-opus-4-8-fast",
            display_name="Opus Fast",
        ),
        SDKModel(
            id="claude-opus-4-8-thinking-high",
            display_name="Opus High Thinking",
            variants=(
                ModelVariant(
                    display_name="High",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
    ]
    selection = detect_opus_high_thinking(models)
    assert selection is not None
    assert selection.id == "claude-opus-4-8-thinking-high"


def test_detect_opus_excludes_max_mode():
    models = [
        SDKModel(
            id="claude-opus-4-8-max",
            display_name="Opus 4.8 Max",
        ),
        SDKModel(
            id="claude-opus-4-8-thinking-high",
            display_name="Opus High Thinking",
            variants=(
                ModelVariant(
                    display_name="High",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
    ]
    selection = detect_opus_high_thinking(models)
    assert selection is not None
    assert selection.id == "claude-opus-4-8-thinking-high"


def test_detect_opus_max_prefers_context_variant_over_legacy_id():
    models = [
        SDKModel(
            id="claude-opus-4-8",
            display_name="Claude Opus 4.8",
            variants=(
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
        SDKModel(id="claude-opus-4-8-max", display_name="Opus 4.8 Max"),
    ]
    selection = detect_opus_max(models)
    assert selection is not None
    assert selection.id == "claude-opus-4-8"
    assert selection.params == (
        ModelParameterValue(id="context", value="1m"),
        ModelParameterValue(id="thinking", value="true"),
        ModelParameterValue(id="effort", value="xhigh"),
    )


def test_detect_opus_max_falls_back_to_legacy_max_id():
    models = [
        SDKModel(id="claude-opus-4-8-max", display_name="Opus 4.8 Max"),
        SDKModel(id="claude-opus-4-8-thinking-high", display_name="Opus High Thinking"),
    ]
    selection = detect_opus_max(models)
    assert selection is not None
    assert selection.id == "claude-opus-4-8-max"
    assert selection.params == ()


def test_detect_sonnet_max_prefers_context_variant():
    models = [
        SDKModel(
            id="claude-sonnet-4-6",
            display_name="Claude Sonnet 4.6",
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
        SDKModel(id="claude-sonnet-4-6-max", display_name="Sonnet Max"),
    ]
    selection = detect_sonnet_max(models)
    assert selection is not None
    assert selection.id == "claude-sonnet-4-6"
    assert selection.params == (ModelParameterValue(id="context", value="1m"),)


def test_detect_sonnet_max_falls_back_to_legacy_max_id():
    models = [
        SDKModel(id="claude-sonnet-4-6", display_name="Sonnet"),
        SDKModel(id="claude-sonnet-4-6-max", display_name="Sonnet Max"),
    ]
    selection = detect_sonnet_max(models)
    assert selection is not None
    assert selection.id == "claude-sonnet-4-6-max"


def test_detect_sonnet_max_accepts_max_toggle_param():
    models = [
        SDKModel(
            id="claude-sonnet-4-6",
            display_name="Claude Sonnet 4.6",
            variants=(
                ModelVariant(
                    display_name="Extended",
                    params=(ModelParameterValue(id="max", value="true"),),
                ),
            ),
        ),
    ]
    selection = detect_sonnet_max(models)
    assert selection is not None
    assert selection.id == "claude-sonnet-4-6"
    assert selection.params == (ModelParameterValue(id="max", value="true"),)


def test_detect_opus_from_variant_params_when_names_differ():
    """Account may expose non-standard display names; params should win."""
    models = [
        SDKModel(
            id="claude-opus-4-8",
            display_name="Premium Reasoning",
            variants=(
                ModelVariant(
                    display_name="Deep analysis",
                    params=(ModelParameterValue(id="thinking", value="high"),),
                ),
            ),
        ),
    ]
    selection = detect_opus_high_thinking(models)
    assert selection is not None
    assert selection.id == "claude-opus-4-8"
    assert selection.params == (ModelParameterValue(id="thinking", value="high"),)


def test_detect_opus_returns_none_when_no_opus_models():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5"),
        SDKModel(id="gpt-4", display_name="GPT-4"),
    ]
    assert detect_opus_high_thinking(models) is None


def test_detect_composer_from_listings():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5"),
        SDKModel(id="other-model", display_name="Other"),
    ]
    assert detect_composer(models).id == "composer-2.5"


def test_detect_composer_falls_back_when_missing():
    models = [SDKModel(id="gpt-4", display_name="GPT-4")]
    assert detect_composer(models).id == COMPOSER_MODEL_ID


def test_discover_model_capabilities_from_models():
    caps = discover_model_capabilities(_opus_models())
    assert caps.opus_available is True
    assert caps.opus is not None
    assert caps.opus.id == "claude-opus-4-8-thinking-high"
    assert caps.composer.id == COMPOSER_MODEL_ID


def test_discover_model_capabilities_without_opus():
    models = [SDKModel(id="composer-2.5", display_name="Composer 2.5")]
    caps = discover_model_capabilities(models)
    assert caps.opus_available is False
    assert caps.opus is None
    assert caps.composer.id == COMPOSER_MODEL_ID


def test_discover_model_capabilities_uses_injected_list_fn():
    called: list[str] = []

    def fake_list(**kwargs: object) -> list[SDKModel]:
        called.append("list")
        if kwargs.get("api_key") == "test-key":
            called.append("api_key")
        return _opus_models()

    caps = discover_model_capabilities(list_models=fake_list, api_key="test-key")

    assert called == ["list", "api_key"]
    assert caps.opus_available is True
    assert isinstance(caps, ModelCapabilities)


def test_fetch_model_inventory_uses_injected_list_fn():
    inventory = fetch_model_inventory(list_models=lambda **_kw: _opus_models())
    assert len(inventory.models) == 2
    assert inventory.capabilities.opus_available is True


def test_fetch_model_inventory_raises_on_cursor_error():
    def failing_list(**_kwargs: object) -> list[SDKModel]:
        raise CursorAgentError("auth failed", is_retryable=False)

    with pytest.raises(ModelListingError, match="Cursor.models.list\\(\\) failed"):
        fetch_model_inventory(list_models=failing_list)


def test_format_models_diagnostic_lists_inventory_and_opus_route():
    inventory = fetch_model_inventory(list_models=lambda **_kw: _opus_models())
    report = format_models_diagnostic(inventory)

    assert "Cursor model inventory (2 models)" in report
    assert "composer-2.5" in report
    assert "claude-opus-4-8-thinking-high" in report
    assert "reasoning=high" in report
    assert "Composer (complexity 1-8):" in report
    assert "Opus high-thinking (complexity 9-10):" in report
    assert "Opus route available: yes" in report

    composer_pos = report.index("Composer (complexity 1-8):")
    opus_pos = report.index("Opus high-thinking (complexity 9-10):")
    inventory_pos = report.index("Model ID")
    assert inventory_pos < composer_pos < opus_pos


def test_format_models_diagnostic_shows_fallback_when_opus_missing():
    models = [SDKModel(id="composer-2.5", display_name="Composer 2.5")]
    inventory = fetch_model_inventory(list_models=lambda **_kw: models)
    report = format_models_diagnostic(inventory)

    assert "Opus route available: no" in report
    assert "Fallback for complexity 9-10:" in report
    assert "composer-2.5" in report


def test_discover_model_capabilities_graceful_when_preset_names_differ():
    """Non-standard Opus naming still resolves via id/variant heuristics."""
    models = [
        SDKModel(
            id="claude-opus-4.8-thinking-high",
            display_name="Claude Opus (extended)",
            variants=(
                ModelVariant(
                    display_name="Reasoning effort",
                    params=(
                        ModelParameterValue(id="reasoning_effort", value="high"),
                    ),
                ),
            ),
        ),
    ]
    caps = discover_model_capabilities(models)
    assert caps.opus_available is True
    assert caps.opus is not None
    assert caps.opus.id == "claude-opus-4.8-thinking-high"
