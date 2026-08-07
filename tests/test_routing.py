"""Tests for task 5/17: complexity report reader and model routing."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from cursor_sdk import ModelParameterValue, ModelSelection, ModelVariant, SDKModel

from cyclopsctl.config import ConfigError, build_run_config, load_config_file
from cyclopsctl.models import (
    COMPOSER_FAST_MODEL_ID,
    COMPOSER_MODEL_ID,
    ModelCapabilities,
    detect_composer,
)
from cyclopsctl.routing import (
    ComplexityReport,
    ModelRouter,
    RoutingConfig,
    RoutingConfigError,
    RoutingFallback,
    RoutingRule,
    load_complexity_report,
    load_routing_config_from_json,
    parse_complexity_payload,
    parse_routing_config,
    resolve_model_for_score,
)


def _sample_report() -> dict:
    return {
        "meta": {"tasksAnalyzed": 2},
        "complexityAnalysis": [
            {"taskId": 3, "complexityScore": 4},
            {"taskId": 7, "complexityScore": 9},
            {"taskId": 8, "complexityScore": 10},
        ],
    }


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


def _capabilities(
    *,
    opus: ModelSelection | None = None,
    fable: ModelSelection | None = None,
    grok: ModelSelection | None = None,
    composer_id: str = COMPOSER_MODEL_ID,
    composer_fast: ModelSelection | None = None,
) -> ModelCapabilities:
    composer = ModelSelection(id=composer_id)
    return ModelCapabilities(
        composer=composer,
        composer_standard=composer,
        composer_fast=composer_fast,
        grok_available=grok is not None,
        grok=grok,
        grok_standard=grok,
        grok_fast=None,
        opus_available=opus is not None,
        opus=opus,
        fable_available=fable is not None,
        fable=fable,
    )


def test_parse_complexity_payload_parent_ids_only():
    scores = parse_complexity_payload(_sample_report())
    assert scores == {3: 4, 7: 9, 8: 10}


def test_load_complexity_report_read_only(tmp_path: Path):
    report_path = tmp_path / "task-complexity-report.json"
    report_path.write_text(json.dumps(_sample_report()), encoding="utf-8")
    before = report_path.read_text(encoding="utf-8")

    loaded = load_complexity_report(report_path)

    assert loaded.score_for(7) == 9
    assert report_path.read_text(encoding="utf-8") == before


def test_load_complexity_report_missing_file(tmp_path: Path):
    missing = tmp_path / "missing.json"
    report = load_complexity_report(missing)
    assert report.scores == {}


def test_route_low_complexity_uses_composer():
    router = ModelRouter(
        complexity_report=ComplexityReport(scores={3: 4}),
        capabilities=_capabilities(
            fable=ModelSelection(id="claude-fable-5"),
            grok=ModelSelection(id="grok-4.5"),
        ),
    )
    decision = router.route(3)
    assert decision.model.id == COMPOSER_MODEL_ID
    assert decision.used_opus is False
    assert decision.used_grok is False
    assert decision.complexity_score == 4


def test_route_mid_complexity_uses_grok():
    grok = ModelSelection(
        id="grok-4.5",
        params=(
            ModelParameterValue(id="effort", value="high"),
            ModelParameterValue(id="fast", value="false"),
        ),
    )
    router = ModelRouter(
        complexity_report=ComplexityReport(scores={5: 7}),
        capabilities=_capabilities(grok=grok, fable=ModelSelection(id="claude-fable-5")),
    )
    decision = router.route(5)
    assert decision.model == grok
    assert decision.used_grok is True
    assert decision.used_fable is False
    assert decision.fallback is False


def test_route_high_complexity_uses_fable():
    fable = ModelSelection(
        id="claude-fable-5",
        params=(
            ModelParameterValue(id="thinking", value="true"),
            ModelParameterValue(id="effort", value="high"),
        ),
    )
    router = ModelRouter(
        complexity_report=ComplexityReport(scores={7: 9}),
        capabilities=_capabilities(fable=fable, grok=ModelSelection(id="grok-4.5")),
    )
    decision = router.route(7)
    assert decision.model == fable
    assert decision.used_fable is True
    assert decision.used_opus is False
    assert decision.fallback is False


def test_route_missing_score_falls_back_to_composer():
    router = ModelRouter(
        complexity_report=ComplexityReport(scores={1: 5}),
        capabilities=_capabilities(
            fable=ModelSelection(id="claude-fable-5"),
            grok=ModelSelection(id="grok-4.5"),
        ),
    )
    decision = router.route(99)
    assert decision.model.id == COMPOSER_MODEL_ID
    assert decision.fallback is True
    assert decision.complexity_score is None


def test_route_missing_report_uses_composer():
    router = ModelRouter(
        complexity_report=ComplexityReport(scores={}),
        capabilities=_capabilities(
            fable=ModelSelection(id="claude-fable-5"),
            grok=ModelSelection(id="grok-4.5"),
        ),
    )
    decision = router.route(5)
    assert decision.model.id == COMPOSER_MODEL_ID
    assert decision.fallback is True


def test_route_fable_unavailable_falls_back_to_grok():
    grok = ModelSelection(id="grok-4.5")
    router = ModelRouter(
        default_model="my-default-model",
        complexity_report=ComplexityReport(scores={7: 10, 8: 9}),
        capabilities=_capabilities(fable=None, grok=grok),
    )

    first = router.route(7)
    second = router.route(8)

    assert first.model == grok
    assert second.model == grok
    assert first.used_grok is True
    assert first.fallback is True


def test_route_fable_and_grok_unavailable_falls_back_and_warns_once(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.WARNING)
    router = ModelRouter(
        default_model="my-default-model",
        complexity_report=ComplexityReport(scores={7: 10, 8: 9}),
        capabilities=_capabilities(fable=None, grok=None),
    )

    first = router.route(7)
    second = router.route(8)

    assert first.model.id == "my-default-model"
    assert second.model.id == "my-default-model"
    assert first.fallback is True
    assert second.fallback is True
    fable_warnings = [r for r in caplog.records if "Fable high-thinking" in r.message]
    assert len(fable_warnings) == 1


def test_router_from_paths_integration(tmp_path: Path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "complexityAnalysis": [
                    {"taskId": 3, "complexityScore": 4},
                    {"taskId": 7, "complexityScore": 8},
                    {"taskId": 8, "complexityScore": 10},
                ]
            }
        ),
        encoding="utf-8",
    )

    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5"),
        SDKModel(
            id="grok-4.5",
            display_name="Grok 4.5",
            variants=(
                ModelVariant(
                    display_name="High",
                    params=(
                        ModelParameterValue(id="effort", value="high"),
                        ModelParameterValue(id="fast", value="false"),
                    ),
                ),
            ),
        ),
        SDKModel(
            id="claude-fable-5",
            display_name="Fable 5",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(
                        ModelParameterValue(id="thinking", value="true"),
                        ModelParameterValue(id="context", value="300k"),
                        ModelParameterValue(id="effort", value="high"),
                    ),
                ),
            ),
        ),
    ]

    router = ModelRouter.from_paths(
        complexity_report_path=report_path,
        default_model="composer-2.5",
        list_models=lambda **_: models,
    )

    low = router.route(3)
    mid = router.route(7)
    high = router.route(8)

    assert low.model.id == COMPOSER_MODEL_ID
    assert mid.used_grok is True
    assert mid.model.id == "grok-4.5"
    assert high.used_fable is True
    assert high.model.id == "claude-fable-5"


def test_disable_premium_runtime_falls_back_to_grok_for_high_scores():
    fable = ModelSelection(id="claude-fable-5")
    grok = ModelSelection(id="grok-4.5")
    caps = _capabilities(fable=fable, grok=grok)
    router = ModelRouter(
        default_model=COMPOSER_MODEL_ID,
        complexity_report=ComplexityReport(scores={6: 9}),
        capabilities=caps,
    )

    before = router.route(6)
    assert before.used_fable is True

    router.disable_premium_runtime()
    after = router.route(6)

    assert after.used_fable is False
    assert after.used_grok is True
    assert after.model == grok
    assert after.fallback is True


def test_legacy_routing_golden_parity_across_score_bands():
    grok = ModelSelection(id="grok-4.5")
    fable = ModelSelection(id="claude-fable-5")
    caps = _capabilities(grok=grok, fable=fable)
    for score in range(1, 6):
        decision = resolve_model_for_score(
            score,
            routing_config=None,
            capabilities=caps,
            default_model="composer-2.5",
        )
        assert decision.model.id == COMPOSER_MODEL_ID
        assert decision.used_grok is False
        assert decision.used_fable is False
        assert decision.fallback is False

    for score in (6, 7, 8):
        decision = resolve_model_for_score(
            score,
            routing_config=None,
            capabilities=caps,
            default_model="composer-2.5",
        )
        assert decision.model == grok
        assert decision.used_grok is True

    for score in (9, 10):
        decision = resolve_model_for_score(
            score,
            routing_config=None,
            capabilities=caps,
            default_model="composer-2.5",
        )
        assert decision.model == fable
        assert decision.used_fable is True


def test_configured_rules_match_score_bands():
    routing = RoutingConfig(
        rules=(
            RoutingRule(min_score=1, max_score=5, model="composer-standard"),
            RoutingRule(min_score=6, max_score=8, model="composer-fast"),
            RoutingRule(min_score=9, max_score=10, model="opus-high-thinking"),
        ),
        fallback=RoutingFallback(model="composer-standard"),
    )
    caps = _capabilities(
        opus=ModelSelection(id="claude-opus-4-8-thinking-high"),
        composer_fast=ModelSelection(id=COMPOSER_FAST_MODEL_ID),
    )

    low = resolve_model_for_score(4, routing_config=routing, capabilities=caps, default_model="x")
    mid = resolve_model_for_score(7, routing_config=routing, capabilities=caps, default_model="x")
    high = resolve_model_for_score(9, routing_config=routing, capabilities=caps, default_model="x")

    assert low.model.id == COMPOSER_MODEL_ID
    assert mid.model.id == COMPOSER_FAST_MODEL_ID
    assert high.used_opus is True


def test_configured_fallback_for_missing_score_and_unmatched_band():
    routing = RoutingConfig(
        rules=(RoutingRule(min_score=1, max_score=5, model="composer-standard"),),
        fallback=RoutingFallback(model="my-fallback", missing_score="composer-standard"),
    )
    caps = _capabilities(opus=None)

    missing = resolve_model_for_score(
        None,
        routing_config=routing,
        capabilities=caps,
        default_model="ignored",
    )
    unmatched = resolve_model_for_score(
        10,
        routing_config=routing,
        capabilities=caps,
        default_model="ignored",
    )

    assert missing.model.id == COMPOSER_MODEL_ID
    assert missing.fallback is True
    assert unmatched.model.id == "my-fallback"
    assert unmatched.fallback is True


def test_opus_gating_disables_high_complexity_opus_route():
    routing = RoutingConfig(
        rules=(RoutingRule(min_score=9, max_score=10, model="opus-high-thinking"),),
        fallback=RoutingFallback(model="composer-standard"),
        opus_enabled=False,
    )
    caps = _capabilities(opus=ModelSelection(id="claude-opus-4-8-thinking-high"))

    decision = resolve_model_for_score(
        10,
        routing_config=routing,
        capabilities=caps,
        default_model="composer-2.5",
        opus_enabled=False,
    )

    assert decision.model.id == COMPOSER_MODEL_ID
    assert decision.used_opus is False
    assert decision.fallback is True


def test_fable_gating_falls_back_to_grok_then_composer():
    routing = RoutingConfig(
        rules=(RoutingRule(min_score=9, max_score=10, model="fable-high-thinking"),),
        fallback=RoutingFallback(model="composer-standard"),
        fable_enabled=False,
    )
    grok = ModelSelection(id="grok-4.5")
    caps = _capabilities(fable=ModelSelection(id="claude-fable-5"), grok=grok)

    decision = resolve_model_for_score(
        10,
        routing_config=routing,
        capabilities=caps,
        default_model="composer-2.5",
        fable_enabled=False,
    )

    assert decision.model == grok
    assert decision.used_fable is False
    assert decision.used_grok is True
    assert decision.fallback is True


def test_legacy_high_band_uses_grok_when_fable_disabled():
    grok = ModelSelection(id="grok-4.5")
    caps = _capabilities(fable=ModelSelection(id="claude-fable-5"), grok=grok)
    decision = resolve_model_for_score(
        10,
        routing_config=None,
        capabilities=caps,
        default_model="composer-2.5",
        fable_enabled=False,
    )
    assert decision.model == grok
    assert decision.used_grok is True
    assert decision.used_fable is False
    assert decision.fallback is True


def test_detect_composer_prefers_fast_tier_when_configured():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5"),
        SDKModel(id="composer-2.5-fast", display_name="Composer 2.5 Fast"),
    ]
    selection = detect_composer(models, composer_tier="fast")
    assert selection.id == COMPOSER_FAST_MODEL_ID


def test_detect_composer_fast_falls_back_to_standard_when_unavailable(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.WARNING)
    models = [SDKModel(id="composer-2.5", display_name="Composer 2.5")]
    selection = detect_composer(models, composer_tier="fast")
    assert selection.id == COMPOSER_MODEL_ID
    assert any("fast tier is not available" in record.message for record in caplog.records)


def test_router_uses_fast_composer_tier_from_routing_config():
    routing = RoutingConfig(composer_tier="fast")
    router = ModelRouter(
        complexity_report=ComplexityReport(scores={1: 4}),
        routing_config=routing,
        list_models=lambda **_: [
            SDKModel(id="composer-2.5", display_name="Composer 2.5"),
            SDKModel(id="composer-2.5-fast", display_name="Composer 2.5 Fast"),
        ],
    )
    decision = router.route(1)
    assert decision.model.id == COMPOSER_FAST_MODEL_ID


def test_parse_routing_config_rejects_overlapping_bands():
    with pytest.raises(RoutingConfigError, match="overlapping"):
        parse_routing_config(
            {
                "rules": [
                    {"min_score": 1, "max_score": 6, "model": "composer-standard"},
                    {"min_score": 5, "max_score": 8, "model": "composer-fast"},
                ]
            }
        )


def test_parse_routing_config_rejects_reversed_ranges():
    with pytest.raises(RoutingConfigError, match="reversed range"):
        parse_routing_config(
            {
                "rules": [
                    {"min_score": 8, "max_score": 3, "model": "composer-standard"},
                ]
            }
        )


def test_parse_routing_config_rejects_unknown_model_alias():
    with pytest.raises(RoutingConfigError, match="unknown model alias"):
        parse_routing_config(
            {
                "rules": [
                    {"min_score": 1, "max_score": 3, "model": "composer-ultra"},
                ]
            }
        )


def test_load_routing_config_from_json(tmp_path: Path):
    path = tmp_path / "routing.json"
    path.write_text(
        json.dumps(
            {
                "composer_tier": "standard",
                "rules": [
                    {"min_score": 1, "max_score": 8, "model": "composer-2.5"},
                    {"min_score": 9, "max_score": 10, "model": "opus"},
                ],
                "fallback": {"model": "composer-2.5", "missing_score": "composer-2.5"},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_routing_config_from_json(path)
    assert loaded.uses_custom_rules is True
    assert loaded.rules[1].model == "opus"


def test_build_run_config_loads_routing_section(project_tree_factory, tmp_path: Path):
    root = project_tree_factory(tmp_path)
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        f"""
        project_root = "{root.as_posix()}"
        cycles = 1
        first_prompt = "{(root / 'prompts' / 'first.md').as_posix()}"
        current_handover = "{(root / 'current-handover-prompt.md').as_posix()}"
        update_handover = "{(root / 'update-handover-prompt.md').as_posix()}"

        [routing]
        composer_tier = "fast"
        opus_enabled = false

        [[routing.rules]]
        min_score = 1
        max_score = 8
        model = "composer-fast"

        [[routing.rules]]
        min_score = 9
        max_score = 10
        model = "opus-high-thinking"

        [routing.fallback]
        model = "composer-standard"
        missing_score = "composer-standard"
        """,
        encoding="utf-8",
    )
    cfg = build_run_config(
        {
            "cycles": 1,
            "project_root": root,
            "first_prompt": root / "prompts" / "first.md",
            "current_handover": root / "current-handover-prompt.md",
            "update_handover": root / "update-handover-prompt.md",
        },
        load_config_file(toml_path.resolve()),
    )
    assert cfg.routing is not None
    assert cfg.routing.composer_tier == "fast"
    assert cfg.routing.opus_enabled is False
    assert len(cfg.routing.rules) == 2


@pytest.fixture
def project_tree_factory():
    def _make(tmp_path: Path) -> Path:
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

    return _make


def test_build_run_config_invalid_routing_raises(project_tree_factory, tmp_path: Path):
    root = project_tree_factory(tmp_path)
    toml_path = tmp_path / "bad-routing.toml"
    toml_path.write_text(
        f"""
        project_root = "{root.as_posix()}"
        cycles = 1
        first_prompt = "{(root / 'prompts' / 'first.md').as_posix()}"
        current_handover = "{(root / 'current-handover-prompt.md').as_posix()}"
        update_handover = "{(root / 'update-handover-prompt.md').as_posix()}"

        [[routing.rules]]
        min_score = 1
        max_score = 10
        model = "not valid alias"
        """,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown model alias"):
        build_run_config(
            {
                "cycles": 1,
                "project_root": root,
                "first_prompt": root / "prompts" / "first.md",
                "current_handover": root / "current-handover-prompt.md",
                "update_handover": root / "update-handover-prompt.md",
            },
            load_config_file(toml_path.resolve()),
        )
