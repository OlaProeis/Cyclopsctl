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

from cyclopsctl.models import ModelCapabilities, discover_model_capabilities

DEFAULT_MODEL = "composer-2.5"

logger = logging.getLogger(__name__)

OPUS_COMPLEXITY_THRESHOLD = 9
MIN_COMPLEXITY_SCORE = 1
MAX_COMPLEXITY_SCORE = 10

_OPUS_UNAVAILABLE_WARNING = (
    "Opus high-thinking preset is not available on this account; "
    "falling back to %s for high-complexity tasks"
)

ModelAlias = Literal[
    "composer-standard",
    "composer-fast",
    "opus-high-thinking",
]

KNOWN_MODEL_ALIASES: frozenset[str] = frozenset(
    {
        "composer",
        "composer-standard",
        "composer-2.5",
        "composer-fast",
        "composer-2.5-fast",
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
    """Validated routing table and composer tier preferences."""

    rules: tuple[RoutingRule, ...] = ()
    fallback: RoutingFallback | None = None
    composer_tier: str = "standard"
    opus_enabled: bool = True

    @property
    def uses_custom_rules(self) -> bool:
        return bool(self.rules)


@dataclass(frozen=True)
class RoutingDecision:
    """Model chosen for a cycle plus routing metadata."""

    model: ModelSelection
    complexity_score: int | None
    used_opus: bool
    fallback: bool = False


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
    if normalized.startswith(("composer", "opus")):
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


def _parse_composer_tier(raw: object | None) -> str:
    if raw is None:
        return "standard"
    if not isinstance(raw, str) or not raw.strip():
        raise RoutingConfigError("routing.composer_tier must be a non-empty string")
    tier = raw.strip()
    normalized = tier.lower()
    if normalized in {"standard", "fast"}:
        return normalized
    if _EXPLICIT_MODEL_ID_PATTERN.fullmatch(tier):
        return tier
    raise RoutingConfigError(
        "routing.composer_tier must be 'standard', 'fast', or an explicit model id "
        f"(got: {raw!r})"
    )


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
    opus_enabled_raw = data.get("opus_enabled", True)
    if not isinstance(opus_enabled_raw, bool):
        raise RoutingConfigError("routing.opus_enabled must be a boolean")
    opus_enabled = opus_enabled_raw

    return RoutingConfig(
        rules=rules,
        fallback=fallback,
        composer_tier=composer_tier,
        opus_enabled=opus_enabled,
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
) -> tuple[ModelSelection, bool]:
    """
    Resolve a model alias or explicit id to a ``ModelSelection``.

    Returns ``(selection, used_opus)``.
    """
    canonical = _ALIAS_TO_CANONICAL.get(model_ref.lower(), model_ref)
    if canonical == "composer-standard":
        return capabilities.composer_standard, False
    if canonical == "composer-fast":
        if capabilities.composer_fast is not None:
            return capabilities.composer_fast, False
        return capabilities.composer_standard, False
    if canonical == "opus-high-thinking":
        if capabilities.opus_available and capabilities.opus is not None:
            return capabilities.opus, True
        return capabilities.composer, False
    return ModelSelection(id=model_ref), False


def resolve_model_for_score(
    score: int | None,
    *,
    routing_config: RoutingConfig | None,
    capabilities: ModelCapabilities,
    default_model: str,
    opus_enabled: bool = True,
) -> RoutingDecision:
    """Resolve a runtime model for a complexity score."""
    if routing_config is None or not routing_config.uses_custom_rules:
        return _resolve_legacy_score(
            score,
            capabilities=capabilities,
            default_model=default_model,
            opus_enabled=opus_enabled,
        )
    return _resolve_configured_score(
        score,
        routing_config=routing_config,
        capabilities=capabilities,
        default_model=default_model,
    )


def _resolve_legacy_score(
    score: int | None,
    *,
    capabilities: ModelCapabilities,
    default_model: str,
    opus_enabled: bool,
) -> RoutingDecision:
    if score is None or score < OPUS_COMPLEXITY_THRESHOLD:
        return RoutingDecision(
            model=capabilities.composer,
            complexity_score=score,
            used_opus=False,
            fallback=score is None,
        )

    if (
        opus_enabled
        and capabilities.opus_available
        and capabilities.opus is not None
    ):
        return RoutingDecision(
            model=capabilities.opus,
            complexity_score=score,
            used_opus=True,
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
) -> RoutingDecision:
    fallback_ref = (
        routing_config.fallback.model if routing_config.fallback is not None else None
    )
    missing_ref = (
        routing_config.fallback.missing_score
        if routing_config.fallback is not None
        else None
    )

    if score is None:
        model_ref = missing_ref or fallback_ref or default_model
        model, used_opus = resolve_model_alias(model_ref, capabilities=capabilities)
        return RoutingDecision(
            model=model,
            complexity_score=None,
            used_opus=used_opus,
            fallback=True,
        )

    matched_rule: RoutingRule | None = None
    for rule in routing_config.rules:
        if rule.min_score <= score <= rule.max_score:
            matched_rule = rule
            break

    if matched_rule is None:
        model_ref = fallback_ref or default_model
        model, used_opus = resolve_model_alias(model_ref, capabilities=capabilities)
        return RoutingDecision(
            model=model,
            complexity_score=score,
            used_opus=used_opus,
            fallback=True,
        )

    canonical = _ALIAS_TO_CANONICAL.get(
        matched_rule.model.lower(),
        matched_rule.model,
    )
    wants_opus = canonical == "opus-high-thinking"
    if wants_opus and not routing_config.opus_enabled:
        model_ref = fallback_ref or default_model
        model, _ = resolve_model_alias(model_ref, capabilities=capabilities)
        return RoutingDecision(
            model=model,
            complexity_score=score,
            used_opus=False,
            fallback=True,
        )

    model, used_opus = resolve_model_alias(matched_rule.model, capabilities=capabilities)
    if wants_opus and not used_opus:
        model_ref = fallback_ref or default_model
        model, _ = resolve_model_alias(model_ref, capabilities=capabilities)
        return RoutingDecision(
            model=model,
            complexity_score=score,
            used_opus=False,
            fallback=True,
        )

    return RoutingDecision(
        model=model,
        complexity_score=score,
        used_opus=used_opus,
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
        self._capabilities = capabilities or discover_model_capabilities(
            list_models=list_models,
            api_key=api_key,
            composer_tier=composer_tier,
        )
        self._opus_warned = False
        self._opus_runtime_enabled = True

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

    def disable_opus_runtime(self) -> None:
        """Disable Opus for the remainder of this run (e.g. after billing failure)."""
        self._opus_runtime_enabled = False

    @property
    def opus_runtime_enabled(self) -> bool:
        return self._opus_runtime_enabled

    def route(self, task_id: int) -> RoutingDecision:
        """
        Select a runtime model for a parent task id.

        Uses legacy thresholds when no custom routing rules are configured;
        otherwise applies validated routing rules and fallback settings.
        """
        score = self._report.score_for(task_id)
        config_opus_enabled = (
            self._routing_config.opus_enabled
            if self._routing_config is not None
            else True
        )
        decision = resolve_model_for_score(
            score,
            routing_config=self._routing_config,
            capabilities=self._capabilities,
            default_model=self._default_model,
            opus_enabled=config_opus_enabled and self._opus_runtime_enabled,
        )

        if (
            decision.fallback
            and decision.complexity_score is not None
            and decision.complexity_score >= OPUS_COMPLEXITY_THRESHOLD
            and not decision.used_opus
            and (
                self._routing_config is None
                or not self._routing_config.uses_custom_rules
            )
        ):
            self._warn_opus_unavailable_once()

        return decision
