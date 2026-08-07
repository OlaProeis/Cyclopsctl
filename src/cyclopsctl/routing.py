"""Complexity report lookup and model routing (task 5 + task 6 + task 17)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cursor_sdk import ModelSelection, SDKModel

from cyclopsctl.models import (
    COMPOSER_COMPLEXITY_MAX,
    FABLE_COMPLEXITY_MIN,
    GROK_COMPLEXITY_MAX,
    GROK_COMPLEXITY_MIN,
    ModelCapabilities,
    discover_model_capabilities,
)

DEFAULT_MODEL = "composer-2.5"

logger = logging.getLogger(__name__)

OPUS_COMPLEXITY_THRESHOLD = 9  # legacy alias for FABLE_COMPLEXITY_MIN
MIN_COMPLEXITY_SCORE = 1
MAX_COMPLEXITY_SCORE = 10

_OPUS_UNAVAILABLE_WARNING = (
    "Opus high-thinking preset is not available on this account; "
    "falling back to %s for high-complexity tasks"
)
_FABLE_UNAVAILABLE_WARNING = (
    "Fable high-thinking preset is not available on this account; "
    "falling back to %s for high-complexity tasks"
)
_GROK_UNAVAILABLE_WARNING = (
    "Grok preset is not available on this account; "
    "falling back to %s for mid-complexity tasks"
)

ModelAlias = Literal[
    "composer-standard",
    "composer-fast",
    "grok-standard",
    "grok-fast",
    "fable-high-thinking",
    "opus-high-thinking",
]

KNOWN_MODEL_ALIASES: frozenset[str] = frozenset(
    {
        "composer",
        "composer-standard",
        "composer-2.5",
        "composer-fast",
        "composer-2.5-fast",
        "grok",
        "grok-standard",
        "grok-4.5",
        "grok-fast",
        "fable",
        "fable-high-thinking",
        "fable-5",
        "opus",
        "opus-high-thinking",
    }
)

_ALIAS_TO_CANONICAL: dict[str, ModelAlias | str] = {
    "composer": "composer-standard",
    "composer-standard": "composer-standard",
    "composer-2.5": "composer-standard",
    "composer-fast": "composer-fast",
    "composer-2.5-fast": "composer-fast",
    "grok": "grok-standard",
    "grok-standard": "grok-standard",
    "grok-4.5": "grok-standard",
    "grok-fast": "grok-fast",
    "fable": "fable-high-thinking",
    "fable-high-thinking": "fable-high-thinking",
    "fable-5": "fable-high-thinking",
    "opus": "opus-high-thinking",
    "opus-high-thinking": "opus-high-thinking",
}

_EXPLICIT_MODEL_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]*$")


class RoutingConfigError(ValueError):
    """Invalid routing configuration."""


@dataclass(frozen=True)
class ComplexityReport:
    """Read-only task id → complexity score map (parent tasks only)."""

    scores: Mapping[int, int]

    def score_for(self, task_id: int) -> int | None:
        return self.scores.get(task_id)


@dataclass(frozen=True)
class RoutingRule:
    """Score band mapped to a model alias or explicit model id."""

    min_score: int
    max_score: int
    model: str


@dataclass(frozen=True)
class RoutingFallback:
    """Fallback models when routing cannot resolve a preset."""

    model: str | None = None
    missing_score: str | None = None


@dataclass(frozen=True)
class RoutingConfig:
    """Validated routing table and tier preferences."""

    rules: tuple[RoutingRule, ...] = ()
    fallback: RoutingFallback | None = None
    composer_tier: str = "standard"
    grok_tier: str = "standard"
    opus_enabled: bool = True
    fable_enabled: bool = True

    @property
    def uses_custom_rules(self) -> bool:
        return bool(self.rules)


@dataclass(frozen=True)
class RoutingDecision:
    """Model chosen for a cycle plus routing metadata."""

    model: ModelSelection
    complexity_score: int | None
    used_opus: bool
    used_fable: bool = False
    used_grok: bool = False
    fallback: bool = False

    @property
    def used_premium(self) -> bool:
        """True when the cycle used a frontier (Fable/Opus) model."""
        return self.used_opus or self.used_fable


def parse_complexity_payload(data: Mapping[str, Any]) -> dict[int, int]:
    """Extract parent ``taskId`` → ``complexityScore`` from a report object."""
    analysis = data.get("complexityAnalysis")
    if not isinstance(analysis, list):
        return {}

    scores: dict[int, int] = {}
    for item in analysis:
        if not isinstance(item, dict):
            continue
        task_id_raw = item.get("taskId")
        score_raw = item.get("complexityScore")
        if task_id_raw is None or score_raw is None:
            continue
        try:
            parent_id = int(task_id_raw)
            score = int(score_raw)
        except (TypeError, ValueError):
            continue
        scores[parent_id] = score
    return scores


def load_complexity_report(path: Path | None) -> ComplexityReport:
    """
    Load complexity scores read-only from the native complexity report file.

    Returns an empty report when the path is missing or unreadable.
    """
    if path is None or not path.is_file():
        return ComplexityReport(scores={})

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid complexity report JSON at %s; treating as empty", path)
        return ComplexityReport(scores={})

    if not isinstance(data, dict):
        logger.warning("Complexity report root must be an object: %s", path)
        return ComplexityReport(scores={})

    return ComplexityReport(scores=parse_complexity_payload(data))


def _normalize_model_reference(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingConfigError(f"{label} must be a non-empty string")
    stripped = value.strip()
    normalized = stripped.lower()
    if normalized in KNOWN_MODEL_ALIASES:
        return normalized
    if normalized.startswith(("composer", "opus", "grok", "fable")):
        raise RoutingConfigError(f"unknown model alias or id in {label}: {value!r}")
    if _EXPLICIT_MODEL_ID_PATTERN.fullmatch(stripped):
        return stripped
    raise RoutingConfigError(f"unknown model alias or id in {label}: {value!r}")


def _parse_score(value: object, *, label: str) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise RoutingConfigError(f"{label} must be an integer (got: {value!r})") from exc
    if score < MIN_COMPLEXITY_SCORE or score > MAX_COMPLEXITY_SCORE:
        raise RoutingConfigError(
            f"{label} must be between {MIN_COMPLEXITY_SCORE} and "
            f"{MAX_COMPLEXITY_SCORE} (got: {score})"
        )
    return score


def _parse_routing_rule(raw: object, *, index: int) -> RoutingRule:
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"routing.rules[{index}] must be a table")
    min_score = _parse_score(raw.get("min_score"), label=f"routing.rules[{index}].min_score")
    max_score = _parse_score(raw.get("max_score"), label=f"routing.rules[{index}].max_score")
    if min_score > max_score:
        raise RoutingConfigError(
            f"routing.rules[{index}] has reversed range: "
            f"min_score ({min_score}) > max_score ({max_score})"
        )
    model = _normalize_model_reference(
        raw.get("model"),
        label=f"routing.rules[{index}].model",
    )
    return RoutingRule(min_score=min_score, max_score=max_score, model=model)


def _validate_non_overlapping_rules(rules: Sequence[RoutingRule]) -> None:
    for left_index, left in enumerate(rules):
        for right in rules[left_index + 1 :]:
            if left.min_score <= right.max_score and right.min_score <= left.max_score:
                raise RoutingConfigError(
                    "routing rules have overlapping score bands: "
                    f"{left.min_score}-{left.max_score} and "
                    f"{right.min_score}-{right.max_score}"
                )


def _parse_routing_fallback(raw: object) -> RoutingFallback:
    if not isinstance(raw, dict):
        raise RoutingConfigError("routing.fallback must be a table")
    model_raw = raw.get("model")
    missing_raw = raw.get("missing_score")
    model = (
        _normalize_model_reference(model_raw, label="routing.fallback.model")
        if model_raw is not None
        else None
    )
    missing_score = (
        _normalize_model_reference(
            missing_raw,
            label="routing.fallback.missing_score",
        )
        if missing_raw is not None
        else None
    )
    if model is None and missing_score is None:
        raise RoutingConfigError(
            "routing.fallback must include at least one of model or missing_score"
        )
    return RoutingFallback(model=model, missing_score=missing_score)


def _parse_tier(raw: object | None, *, field: str) -> str:
    if raw is None:
        return "standard"
    if not isinstance(raw, str) or not raw.strip():
        raise RoutingConfigError(f"routing.{field} must be a non-empty string")
    tier = raw.strip()
    normalized = tier.lower()
    if normalized in {"standard", "fast"}:
        return normalized
    if _EXPLICIT_MODEL_ID_PATTERN.fullmatch(tier):
        return tier
    raise RoutingConfigError(
        f"routing.{field} must be 'standard', 'fast', or an explicit model id "
        f"(got: {raw!r})"
    )


def _parse_composer_tier(raw: object | None) -> str:
    return _parse_tier(raw, field="composer_tier")


def _parse_grok_tier(raw: object | None) -> str:
    return _parse_tier(raw, field="grok_tier")


def parse_routing_config(data: Mapping[str, Any]) -> RoutingConfig:
    """Parse and validate a routing config mapping from TOML or JSON."""
    rules_raw = data.get("rules", [])
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        raise RoutingConfigError("routing.rules must be an array")

    rules = tuple(_parse_routing_rule(item, index=index) for index, item in enumerate(rules_raw))
    _validate_non_overlapping_rules(rules)

    fallback_raw = data.get("fallback")
    fallback = _parse_routing_fallback(fallback_raw) if fallback_raw is not None else None

    composer_tier = _parse_composer_tier(data.get("composer_tier"))
    grok_tier = _parse_grok_tier(data.get("grok_tier"))
    opus_enabled_raw = data.get("opus_enabled", True)
    if not isinstance(opus_enabled_raw, bool):
        raise RoutingConfigError("routing.opus_enabled must be a boolean")
    opus_enabled = opus_enabled_raw
    fable_enabled_raw = data.get("fable_enabled", True)
    if not isinstance(fable_enabled_raw, bool):
        raise RoutingConfigError("routing.fable_enabled must be a boolean")
    fable_enabled = fable_enabled_raw

    return RoutingConfig(
        rules=rules,
        fallback=fallback,
        composer_tier=composer_tier,
        grok_tier=grok_tier,
        opus_enabled=opus_enabled,
        fable_enabled=fable_enabled,
    )


def load_routing_config_from_json(path: Path) -> RoutingConfig:
    """Load routing rules from a standalone JSON file."""
    if not path.is_file():
        raise RoutingConfigError(f"routing config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"invalid routing JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RoutingConfigError(f"routing JSON root must be an object: {path}")
    return parse_routing_config(data)


def resolve_model_alias(
    model_ref: str,
    *,
    capabilities: ModelCapabilities,
    grok_tier: str = "standard",
) -> tuple[ModelSelection, bool, bool, bool]:
    """
    Resolve a model alias or explicit id to a ``ModelSelection``.

    Returns ``(selection, used_opus, used_fable, used_grok)``.
    """
    canonical = _ALIAS_TO_CANONICAL.get(model_ref.lower(), model_ref)
    if canonical == "composer-standard":
        return capabilities.composer_standard, False, False, False
    if canonical == "composer-fast":
        if capabilities.composer_fast is not None:
            return capabilities.composer_fast, False, False, False
        return capabilities.composer_standard, False, False, False
    if canonical == "grok-standard":
        # Respect run-level grok_tier when rules say "grok" / "grok-standard".
        preferred = capabilities.grok
        if grok_tier.lower() == "fast" and capabilities.grok_fast is not None:
            preferred = capabilities.grok_fast
        elif capabilities.grok_standard is not None:
            preferred = capabilities.grok_standard
        if preferred is not None:
            return preferred, False, False, True
        return capabilities.composer, False, False, False
    if canonical == "grok-fast":
        if capabilities.grok_fast is not None:
            return capabilities.grok_fast, False, False, True
        if capabilities.grok_standard is not None:
            return capabilities.grok_standard, False, False, True
        if capabilities.grok is not None:
            return capabilities.grok, False, False, True
        return capabilities.composer, False, False, False
    if canonical == "fable-high-thinking":
        if capabilities.fable_available and capabilities.fable is not None:
            return capabilities.fable, False, True, False
        return capabilities.composer, False, False, False
    if canonical == "opus-high-thinking":
        if capabilities.opus_available and capabilities.opus is not None:
            return capabilities.opus, True, False, False
        return capabilities.composer, False, False, False
    return ModelSelection(id=model_ref), False, False, False


def resolve_model_for_score(
    score: int | None,
    *,
    routing_config: RoutingConfig | None,
    capabilities: ModelCapabilities,
    default_model: str,
    opus_enabled: bool = True,
    fable_enabled: bool = True,
) -> RoutingDecision:
    """Resolve a runtime model for a complexity score."""
    if routing_config is None or not routing_config.uses_custom_rules:
        return _resolve_legacy_score(
            score,
            capabilities=capabilities,
            default_model=default_model,
            fable_enabled=fable_enabled,
        )
    return _resolve_configured_score(
        score,
        routing_config=routing_config,
        capabilities=capabilities,
        default_model=default_model,
        opus_enabled=opus_enabled,
        fable_enabled=fable_enabled,
    )


def _resolve_legacy_score(
    score: int | None,
    *,
    capabilities: ModelCapabilities,
    default_model: str,
    fable_enabled: bool,
) -> RoutingDecision:
    """
    Default bands (no custom ``[[routing.rules]]``):

    - 1–5 → Composer (tier from ``composer_tier``)
    - 6–8 → Grok (tier from ``grok_tier``)
    - 9–10 → Fable high-thinking
    """
    if score is None:
        return RoutingDecision(
            model=capabilities.composer,
            complexity_score=None,
            used_opus=False,
            fallback=True,
        )

    if score <= COMPOSER_COMPLEXITY_MAX:
        return RoutingDecision(
            model=capabilities.composer,
            complexity_score=score,
            used_opus=False,
        )

    if GROK_COMPLEXITY_MIN <= score <= GROK_COMPLEXITY_MAX:
        if capabilities.grok_available and capabilities.grok is not None:
            return RoutingDecision(
                model=capabilities.grok,
                complexity_score=score,
                used_opus=False,
                used_grok=True,
            )
        return RoutingDecision(
            model=capabilities.composer,
            complexity_score=score,
            used_opus=False,
            fallback=True,
        )

    # 9–10 (and any score above grok band): Fable, else Grok, else default.
    if (
        fable_enabled
        and capabilities.fable_available
        and capabilities.fable is not None
    ):
        return RoutingDecision(
            model=capabilities.fable,
            complexity_score=score,
            used_opus=False,
            used_fable=True,
        )

    if capabilities.grok_available and capabilities.grok is not None:
        return RoutingDecision(
            model=capabilities.grok,
            complexity_score=score,
            used_opus=False,
            used_grok=True,
            fallback=True,
        )

    fallback = ModelSelection.from_value(default_model)
    return RoutingDecision(
        model=fallback,
        complexity_score=score,
        used_opus=False,
        fallback=True,
    )


def _resolve_configured_score(
    score: int | None,
    *,
    routing_config: RoutingConfig,
    capabilities: ModelCapabilities,
    default_model: str,
    opus_enabled: bool = True,
    fable_enabled: bool = True,
) -> RoutingDecision:
    fallback_ref = (
        routing_config.fallback.model if routing_config.fallback is not None else None
    )
    missing_ref = (
        routing_config.fallback.missing_score
        if routing_config.fallback is not None
        else None
    )
    grok_tier = routing_config.grok_tier

    if score is None:
        model_ref = missing_ref or fallback_ref or default_model
        model, used_opus, used_fable, used_grok = resolve_model_alias(
            model_ref,
            capabilities=capabilities,
            grok_tier=grok_tier,
        )
        return RoutingDecision(
            model=model,
            complexity_score=None,
            used_opus=used_opus,
            used_fable=used_fable,
            used_grok=used_grok,
            fallback=True,
        )

    matched_rule: RoutingRule | None = None
    for rule in routing_config.rules:
        if rule.min_score <= score <= rule.max_score:
            matched_rule = rule
            break

    if matched_rule is None:
        model_ref = fallback_ref or default_model
        model, used_opus, used_fable, used_grok = resolve_model_alias(
            model_ref,
            capabilities=capabilities,
            grok_tier=grok_tier,
        )
        return RoutingDecision(
            model=model,
            complexity_score=score,
            used_opus=used_opus,
            used_fable=used_fable,
            used_grok=used_grok,
            fallback=True,
        )

    canonical = _ALIAS_TO_CANONICAL.get(
        matched_rule.model.lower(),
        matched_rule.model,
    )
    wants_opus = canonical == "opus-high-thinking"
    wants_fable = canonical == "fable-high-thinking"

    def _premium_substitute() -> RoutingDecision:
        """When Fable/Opus is gated or unavailable, prefer Grok then fallback."""
        if capabilities.grok_available and capabilities.grok is not None:
            return RoutingDecision(
                model=capabilities.grok,
                complexity_score=score,
                used_opus=False,
                used_grok=True,
                fallback=True,
            )
        model_ref = fallback_ref or default_model
        model, used_opus, used_fable, used_grok = resolve_model_alias(
            model_ref,
            capabilities=capabilities,
            grok_tier=grok_tier,
        )
        return RoutingDecision(
            model=model,
            complexity_score=score,
            used_opus=used_opus,
            used_fable=used_fable,
            used_grok=used_grok,
            fallback=True,
        )

    if wants_opus and not opus_enabled:
        return _premium_substitute()
    if wants_fable and not fable_enabled:
        return _premium_substitute()

    model, used_opus, used_fable, used_grok = resolve_model_alias(
        matched_rule.model,
        capabilities=capabilities,
        grok_tier=grok_tier,
    )
    if wants_opus and not used_opus:
        return _premium_substitute()
    if wants_fable and not used_fable:
        return _premium_substitute()

    return RoutingDecision(
        model=model,
        complexity_score=score,
        used_opus=used_opus,
        used_fable=used_fable,
        used_grok=used_grok,
        fallback=False,
    )


class ModelRouter:
    """Resolve runtime model from complexity scores and account capabilities."""

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_MODEL,
        complexity_report: ComplexityReport | None = None,
        capabilities: ModelCapabilities | None = None,
        routing_config: RoutingConfig | None = None,
        list_models: Callable[..., list[SDKModel]] | None = None,
        api_key: str | None = None,
    ) -> None:
        self._default_model = (
            default_model.strip() if default_model.strip() else DEFAULT_MODEL
        )
        self._report = complexity_report or ComplexityReport(scores={})
        self._routing_config = routing_config
        composer_tier = (
            routing_config.composer_tier
            if routing_config is not None
            else "standard"
        )
        grok_tier = (
            routing_config.grok_tier if routing_config is not None else "standard"
        )
        self._capabilities = capabilities or discover_model_capabilities(
            list_models=list_models,
            api_key=api_key,
            composer_tier=composer_tier,
            grok_tier=grok_tier,
        )
        self._opus_warned = False
        self._fable_warned = False
        self._grok_warned = False
        self._opus_runtime_enabled = True
        self._fable_runtime_enabled = True

    @classmethod
    def from_paths(
        cls,
        *,
        complexity_report_path: Path | None,
        default_model: str = DEFAULT_MODEL,
        routing_config: RoutingConfig | None = None,
        list_models: Callable[..., list[SDKModel]] | None = None,
        api_key: str | None = None,
    ) -> ModelRouter:
        report = load_complexity_report(complexity_report_path)
        return cls(
            default_model=default_model,
            complexity_report=report,
            routing_config=routing_config,
            list_models=list_models,
            api_key=api_key,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @property
    def complexity_report(self) -> ComplexityReport:
        return self._report

    @property
    def routing_config(self) -> RoutingConfig | None:
        return self._routing_config

    def _warn_opus_unavailable_once(self) -> None:
        if self._opus_warned:
            return
        self._opus_warned = True
        logger.warning(_OPUS_UNAVAILABLE_WARNING, self._default_model)

    def _warn_fable_unavailable_once(self) -> None:
        if self._fable_warned:
            return
        self._fable_warned = True
        logger.warning(_FABLE_UNAVAILABLE_WARNING, self._default_model)

    def _warn_grok_unavailable_once(self) -> None:
        if self._grok_warned:
            return
        self._grok_warned = True
        logger.warning(_GROK_UNAVAILABLE_WARNING, self._default_model)

    def disable_opus_runtime(self) -> None:
        """Disable Opus for the remainder of this run (e.g. after billing failure)."""
        self._opus_runtime_enabled = False

    def disable_fable_runtime(self) -> None:
        """Disable Fable for the remainder of this run (e.g. after billing failure)."""
        self._fable_runtime_enabled = False

    def disable_premium_runtime(self) -> None:
        """Disable Fable and Opus after a premium-model billing failure."""
        self.disable_fable_runtime()
        self.disable_opus_runtime()

    @property
    def opus_runtime_enabled(self) -> bool:
        return self._opus_runtime_enabled

    @property
    def fable_runtime_enabled(self) -> bool:
        return self._fable_runtime_enabled

    def route(self, task_id: int) -> RoutingDecision:
        """
        Select a runtime model for a parent task id.

        Uses default Composer/Grok/Fable bands when no custom routing rules
        are configured; otherwise applies validated rules and fallbacks.
        """
        score = self._report.score_for(task_id)
        config_opus_enabled = (
            self._routing_config.opus_enabled
            if self._routing_config is not None
            else True
        )
        config_fable_enabled = (
            self._routing_config.fable_enabled
            if self._routing_config is not None
            else True
        )
        decision = resolve_model_for_score(
            score,
            routing_config=self._routing_config,
            capabilities=self._capabilities,
            default_model=self._default_model,
            opus_enabled=config_opus_enabled and self._opus_runtime_enabled,
            fable_enabled=config_fable_enabled and self._fable_runtime_enabled,
        )

        if (
            decision.fallback
            and decision.complexity_score is not None
            and (
                self._routing_config is None
                or not self._routing_config.uses_custom_rules
            )
        ):
            if (
                GROK_COMPLEXITY_MIN
                <= decision.complexity_score
                <= GROK_COMPLEXITY_MAX
                and not decision.used_grok
            ):
                self._warn_grok_unavailable_once()
            elif (
                decision.complexity_score >= FABLE_COMPLEXITY_MIN
                and not decision.used_fable
                and not decision.used_opus
                and not decision.used_grok
            ):
                self._warn_fable_unavailable_once()

        return decision
