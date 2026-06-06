"""Cursor SDK model listing and capability discovery for routing."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from cursor_sdk import (
    Cursor,
    CursorAgentError,
    ModelParameterValue,
    ModelSelection,
    ModelVariant,
    SDKModel,
)

COMPOSER_MODEL_ID = "composer-2.5"
COMPOSER_FAST_MODEL_ID = "composer-2.5-fast"
OPUS_COMPLEXITY_MIN = 9
COMPOSER_COMPLEXITY_MAX = 8

ComposerTierName = Literal["standard", "fast"]

logger = logging.getLogger(__name__)

_COMPOSER_FAST_UNAVAILABLE_WARNING = (
    "Composer fast tier is not available on this account; "
    "falling back to standard Composer"
)


class ModelListingError(RuntimeError):
    """``Cursor.models.list()`` failed during setup-time inspection."""


@dataclass(frozen=True)
class ModelCapabilities:
    """Normalized model presets discovered from ``Cursor.models.list()``."""

    composer: ModelSelection
    composer_standard: ModelSelection
    opus_available: bool
    opus: ModelSelection | None = None
    composer_fast: ModelSelection | None = None


@dataclass(frozen=True)
class ModelInventory:
    """Raw SDK listings plus normalized routing capabilities."""

    models: tuple[SDKModel, ...]
    capabilities: ModelCapabilities


def _normalized_text(*parts: str) -> str:
    return " ".join(part.lower() for part in parts if part)


def _is_disqualified_tier(*parts: str) -> bool:
    """Exclude Max Mode and fast-tier presets (PRD)."""
    blob = _normalized_text(*parts)
    if "fast" in blob:
        return True
    if "max" in blob and "thinking" not in blob:
        return True
    return False


def _variant_rank(model_id: str, variant: ModelVariant) -> int:
    param_blob = " ".join(
        f"{param.id} {param.value}" for param in variant.params
    )
    blob = _normalized_text(
        model_id,
        variant.display_name,
        variant.description,
        param_blob,
    )
    if _is_disqualified_tier(blob):
        return -1
    if "opus" not in blob:
        return -1

    score = 0
    if "4-8" in blob or "4.8" in blob:
        score += 20
    if "thinking" in blob:
        score += 30
    if "high" in blob:
        score += 25
    if any(
        param.id.lower() in {"reasoning", "reasoning_effort", "thinking"}
        and "high" in param.value.lower()
        for param in variant.params
    ):
        score += 40
    return score


def _model_rank(model: SDKModel) -> int:
    blob = _normalized_text(model.id, model.display_name, model.description)
    if _is_disqualified_tier(blob) or "opus" not in blob:
        return -1
    score = 0
    if "4-8" in blob or "4.8" in blob:
        score += 20
    if "thinking" in blob:
        score += 30
    if "high" in blob:
        score += 25
    return score


def _is_sonnet_model_blob(blob: str) -> bool:
    return "sonnet" in blob


def _is_opus_model_blob(blob: str) -> bool:
    return "opus" in blob


_MAX_MODE_PARAM_IDS = frozenset({"max", "max_mode", "maxmode"})
_MAX_MODE_TRUTHY = frozenset({"true", "1", "yes", "on", "max", "enabled"})
_EXTENDED_CONTEXT_PARAM_IDS = frozenset({"context", "context_window", "contextwindow"})
_EXTENDED_CONTEXT_VALUES = frozenset({"1m", "1000k", "1024k", "1000000"})


def _is_extended_context_param(param: ModelParameterValue) -> bool:
    """Return whether a variant param selects extended (Max Mode) context."""
    param_id = param.id.lower()
    value = param.value.lower().strip()
    if param_id not in _EXTENDED_CONTEXT_PARAM_IDS:
        return False
    if value in _EXTENDED_CONTEXT_VALUES:
        return True
    if value.endswith("m") and value[:-1].isdigit():
        try:
            return int(value[:-1]) >= 1000
        except ValueError:
            return False
    return False


def _is_max_mode_toggle_param(param: ModelParameterValue) -> bool:
    """Return whether a variant param explicitly enables Max Mode."""
    param_id = param.id.lower()
    value = param.value.lower().strip()
    if param_id not in _MAX_MODE_PARAM_IDS:
        return False
    return value in _MAX_MODE_TRUTHY


def _max_mode_variant_signal(
    params: Sequence[ModelParameterValue],
    *,
    variant_display: str = "",
    variant_description: str = "",
) -> int:
    """Score how strongly a variant represents Max Mode (0 = not Max Mode)."""
    score = 0
    for param in params:
        if _is_max_mode_toggle_param(param):
            score += 50
        if _is_extended_context_param(param):
            score += 45

    label = _normalized_text(variant_display, variant_description)
    if "max mode" in label:
        score += 20
    elif label.endswith(" max") or " max " in label:
        score += 10
    return score


def _is_legacy_max_mode_model(model: SDKModel) -> bool:
    """Fallback when the catalog exposes Max Mode as a flat model id."""
    blob = _normalized_text(model.id, model.display_name, model.description)
    if "fast" in blob:
        return False
    model_id = model.id.lower()
    if model_id.endswith("-max") or model_id.endswith("_max"):
        return True
    display = _normalized_text(model.display_name or "")
    return display.endswith(" max") or " max mode" in display


def _detect_max_mode_selection(
    models: Sequence[SDKModel],
    *,
    family_check: Callable[[str], bool],
    prefer_thinking_on_opus: bool = False,
) -> ModelSelection | None:
    """
    Pick a Max Mode preset from listings.

    Prefers SDK variant params (``context=1m``, ``max=true``) on a base model id.
    Falls back to legacy flat ``*-max`` catalog ids when variants are absent.
    """
    best: tuple[int, ModelSelection] | None = None

    for model in models:
        blob = _normalized_text(model.id, model.display_name, model.description)
        if not family_check(blob):
            continue
        if "fast" in blob:
            continue

        base_score = _version_score(blob, model.id)

        for variant in model.variants:
            variant_blob = _normalized_text(
                variant.display_name or "",
                variant.description or "",
            )
            if "fast" in variant_blob:
                continue
            signal = _max_mode_variant_signal(
                variant.params,
                variant_display=variant.display_name or "",
                variant_description=variant.description or "",
            )
            if signal <= 0:
                continue

            rank = signal + base_score
            if prefer_thinking_on_opus:
                for param in variant.params:
                    pid = param.id.lower()
                    val = param.value.lower()
                    if pid in {"thinking", "reasoning", "reasoning_effort"} and val in {
                        "true",
                        "high",
                        "xhigh",
                        "extra-high",
                    }:
                        rank += 15
                    if pid == "effort" and val in {"high", "xhigh", "extra-high"}:
                        rank += 15

            selection = ModelSelection(id=model.id, params=tuple(variant.params))
            if best is None or rank > best[0]:
                best = (rank, selection)

        if _is_legacy_max_mode_model(model):
            rank = 35 + base_score
            selection = ModelSelection(id=model.id)
            if best is None or rank > best[0]:
                best = (rank, selection)

    return best[1] if best else None


def _version_score(blob: str, model_id: str) -> int:
    score = 0
    if "4-8" in blob or "4.8" in blob:
        score += 20
    elif "4-6" in blob or "4.6" in blob:
        score += 15
    elif "4" in model_id:
        score += 5
    return score


def detect_sonnet_max(models: Sequence[SDKModel]) -> ModelSelection | None:
    """Pick a Sonnet Max Mode preset (extended context) from account listings."""
    return _detect_max_mode_selection(models, family_check=_is_sonnet_model_blob)


def detect_opus_max(models: Sequence[SDKModel]) -> ModelSelection | None:
    """Pick an Opus Max Mode preset (extended context) from account listings."""
    return _detect_max_mode_selection(
        models,
        family_check=_is_opus_model_blob,
        prefer_thinking_on_opus=True,
    )


def detect_opus_high_thinking(models: Sequence[SDKModel]) -> ModelSelection | None:
    """
    Pick an Opus 4.8 high-thinking preset from account model listings.

    Prefers variant params over hardcoded ids; excludes Max Mode and fast tiers.
    """
    best: tuple[int, ModelSelection] | None = None

    for model in models:
        model_base_rank = _model_rank(model)
        candidates: list[tuple[int, ModelSelection]] = []

        for variant in model.variants:
            rank = _variant_rank(model.id, variant)
            if rank < 0:
                continue
            candidates.append(
                (
                    rank,
                    ModelSelection(id=model.id, params=tuple(variant.params)),
                )
            )

        if not candidates and model_base_rank >= 0:
            candidates.append((model_base_rank, ModelSelection(id=model.id)))

        for rank, selection in candidates:
            combined = rank + model_base_rank
            if best is None or combined > best[0]:
                best = (combined, selection)

    return best[1] if best else None


def _is_fast_composer(*parts: str) -> bool:
    blob = _normalized_text(*parts)
    return "composer" in blob and "fast" in blob


def _is_standard_composer(*parts: str) -> bool:
    blob = _normalized_text(*parts)
    if "composer" not in blob:
        return False
    if "fast" in blob:
        return False
    return not _is_disqualified_tier(blob)


def _detect_composer_fast(models: Sequence[SDKModel]) -> ModelSelection | None:
    for model in models:
        if _is_fast_composer(model.id, model.display_name, model.description):
            return ModelSelection(id=model.id)
    return None


def _detect_composer_standard(models: Sequence[SDKModel]) -> ModelSelection:
    for model in models:
        if _is_standard_composer(model.id, model.display_name, model.description):
            return ModelSelection(id=model.id)
    return ModelSelection(id=COMPOSER_MODEL_ID)


def detect_composer(
    models: Sequence[SDKModel],
    *,
    composer_tier: str = "standard",
) -> ModelSelection:
    """
    Resolve Composer preset from listings.

    ``composer_tier`` may be ``standard``, ``fast``, or an explicit model id.
    Fast tier falls back to standard when unavailable.
    """
    tier = composer_tier.strip()
    if not tier:
        tier = "standard"
    normalized = tier.lower()
    if normalized not in {"standard", "fast"}:
        return ModelSelection(id=tier)

    if normalized == "fast":
        fast = _detect_composer_fast(models)
        if fast is not None:
            return fast
        logger.warning(_COMPOSER_FAST_UNAVAILABLE_WARNING)
        return _detect_composer_standard(models)

    return _detect_composer_standard(models)


def _list_cursor_models(
    *,
    list_models: Callable[..., list[SDKModel]] | None = None,
    api_key: str | None = None,
) -> list[SDKModel]:
    list_fn = list_models or Cursor.models.list
    try:
        return list_fn(api_key=api_key) if api_key is not None else list_fn()
    except CursorAgentError as exc:
        raise ModelListingError(f"Cursor.models.list() failed: {exc}") from exc


def discover_model_capabilities(
    models: Sequence[SDKModel] | None = None,
    *,
    composer_tier: str = "standard",
    list_models: Callable[..., list[SDKModel]] | None = None,
    api_key: str | None = None,
) -> ModelCapabilities:
    """Inspect Cursor models and expose Composer / Opus presets for routing."""
    if models is None:
        models = _list_cursor_models(list_models=list_models, api_key=api_key)

    opus = detect_opus_high_thinking(models)
    composer_standard = _detect_composer_standard(models)
    composer_fast = _detect_composer_fast(models)
    return ModelCapabilities(
        composer=detect_composer(models, composer_tier=composer_tier),
        composer_standard=composer_standard,
        composer_fast=composer_fast,
        opus_available=opus is not None,
        opus=opus,
    )


def fetch_model_inventory(
    *,
    list_models: Callable[..., list[SDKModel]] | None = None,
    api_key: str | None = None,
) -> ModelInventory:
    """Fetch account model listings for setup-time diagnostics."""
    models = tuple(_list_cursor_models(list_models=list_models, api_key=api_key))
    return ModelInventory(
        models=models,
        capabilities=discover_model_capabilities(models),
    )


def _format_variant(variant: ModelVariant) -> str:
    label = variant.display_name or "variant"
    if not variant.params:
        return label
    params = ", ".join(f"{param.id}={param.value}" for param in variant.params)
    return f"{label} ({params})"


def _format_model_variants(model: SDKModel) -> str:
    if not model.variants:
        return "-"
    return "; ".join(_format_variant(variant) for variant in model.variants)


def _format_selection(selection: ModelSelection) -> str:
    if not selection.params:
        return selection.id
    params = ", ".join(f"{param.id}={param.value}" for param in selection.params)
    return f"{selection.id} [{params}]"


def format_models_diagnostic(inventory: ModelInventory) -> str:
    """Render a human-readable routing diagnostic report."""
    caps = inventory.capabilities
    lines = [
        f"Cursor model inventory ({len(inventory.models)} models)",
        "",
        f"{'Model ID':<36} {'Display name':<28} Variants",
        "-" * 96,
    ]

    for model in inventory.models:
        lines.append(
            f"{model.id:<36} {(model.display_name or '-'):<28} {_format_model_variants(model)}"
        )

    lines.extend(
        [
            "",
            "Routing diagnostics",
            "-" * 40,
            (
                f"Composer (complexity 1-{COMPOSER_COMPLEXITY_MAX}): "
                f"{_format_selection(caps.composer)}"
            ),
        ]
    )

    if caps.opus_available and caps.opus is not None:
        lines.append(
            f"Opus high-thinking (complexity {OPUS_COMPLEXITY_MIN}-10): "
            f"{_format_selection(caps.opus)}"
        )
        lines.append("Opus route available: yes")
    else:
        lines.append(
            f"Opus high-thinking (complexity {OPUS_COMPLEXITY_MIN}-10): unavailable"
        )
        lines.append("Opus route available: no")
        lines.append(
            f"Fallback for complexity {OPUS_COMPLEXITY_MIN}-10: "
            f"{_format_selection(caps.composer)}"
        )

    lines.extend(
        [
            "",
            "Use the Opus preset above for high-complexity routing when your account",
            "exposes non-standard model ids or variant names.",
        ]
    )
    return "\n".join(lines)
