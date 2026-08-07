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
GROK_MODEL_ID = "grok-4.5"
COMPOSER_COMPLEXITY_MAX = 5
GROK_COMPLEXITY_MIN = 6
GROK_COMPLEXITY_MAX = 8
FABLE_COMPLEXITY_MIN = 9
OPUS_COMPLEXITY_MIN = 9  # retained for diagnostics / custom opus rules

ComposerTierName = Literal["standard", "fast"]
GrokTierName = Literal["standard", "fast"]

logger = logging.getLogger(__name__)

_COMPOSER_FAST_UNAVAILABLE_WARNING = (
    "Composer fast tier is not available on this account; "
    "falling back to standard Composer"
)
_GROK_FAST_UNAVAILABLE_WARNING = (
    "Grok fast tier is not available on this account; "
    "falling back to standard Grok"
)
_GROK_UNAVAILABLE_WARNING = (
    "Grok preset is not available on this account; "
    "mid-complexity tasks will fall back to Composer"
)

_TRUTHY_PARAM_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSY_PARAM_VALUES = frozenset({"false", "0", "no", "off"})
_EFFORT_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
    "max": 5,
}


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
    grok_available: bool = False
    grok: ModelSelection | None = None
    grok_standard: ModelSelection | None = None
    grok_fast: ModelSelection | None = None
    fable_available: bool = False
    fable: ModelSelection | None = None


@dataclass(frozen=True)
class ModelInventory:
    """Raw SDK listings plus normalized routing capabilities."""

    models: tuple[SDKModel, ...]
    capabilities: ModelCapabilities


def _normalized_text(*parts: str) -> str:
    return " ".join(part.lower() for part in parts if part)


def _param_map(params: Sequence[ModelParameterValue]) -> dict[str, str]:
    return {param.id.lower(): param.value.lower().strip() for param in params}


def _params_indicate_fast(params: Sequence[ModelParameterValue]) -> bool:
    """Return True only when a variant explicitly enables fast mode."""
    value = _param_map(params).get("fast")
    return value in _TRUTHY_PARAM_VALUES if value is not None else False


def _params_indicate_non_fast(params: Sequence[ModelParameterValue]) -> bool:
    value = _param_map(params).get("fast")
    return value in _FALSY_PARAM_VALUES if value is not None else False


def _effort_rank(params: Sequence[ModelParameterValue]) -> int:
    value = _param_map(params).get("effort")
    if value is None:
        return 0
    return _EFFORT_RANK.get(value, 0)


def _is_fast_model_id(*parts: str) -> bool:
    """True for legacy flat fast-tier catalog ids (e.g. ``composer-2.5-fast``)."""
    blob = _normalized_text(*parts)
    model_id = parts[0].lower() if parts else ""
    if model_id.endswith("-fast") or model_id.endswith("_fast"):
        return True
    if "composer" in blob and "fast" in blob and "false" not in blob:
        return "-fast" in model_id or model_id.endswith("fast")
    return False


def _is_disqualified_tier(*parts: str) -> bool:
    """Exclude Max Mode and legacy flat fast-tier model ids from high presets."""
    blob = _normalized_text(*parts)
    if _is_fast_model_id(*parts):
        return True
    if "max" in blob and "thinking" not in blob:
        return True
    return False


def _variant_rank(model_id: str, variant: ModelVariant) -> int:
    if _params_indicate_fast(variant.params) or _context_is_max_mode(variant.params):
        return -1
    param_blob = " ".join(
        f"{param.id} {param.value}" for param in variant.params
    )
    blob = _normalized_text(
        model_id,
        variant.display_name or "",
        variant.description or "",
        param_blob,
    )
    if _is_disqualified_tier(model_id, variant.display_name or "", variant.description or ""):
        return -1
    if "opus" not in blob:
        return -1

    params = _param_map(variant.params)
    score = 0
    if "4-8" in blob or "4.8" in blob or "opus-5" in blob or "opus 5" in blob:
        score += 20
    if params.get("thinking") in _TRUTHY_PARAM_VALUES:
        score += 30
    elif "thinking" in blob and "thinking false" not in blob:
        score += 20
    effort = _effort_rank(variant.params)
    if effort >= 3:
        score += 25 + min(effort, 3)
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


def _is_fable_model_blob(blob: str) -> bool:
    return "fable" in blob


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


def _context_is_max_mode(params: Sequence[ModelParameterValue]) -> bool:
    return any(_is_extended_context_param(param) for param in params)


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


def _fable_model_rank(model: SDKModel) -> int:
    blob = _normalized_text(model.id, model.display_name, model.description)
    if _is_disqualified_tier(blob) or not _is_fable_model_blob(blob):
        return -1
    score = 0
    if "5" in model.id or "fable-5" in blob or "fable 5" in blob:
        score += 20
    if "thinking" in blob:
        score += 30
    if "high" in blob:
        score += 25
    return score


def _fable_variant_rank(model_id: str, variant: ModelVariant) -> int:
    if _params_indicate_fast(variant.params) or _context_is_max_mode(variant.params):
        return -1
    blob = _normalized_text(
        model_id,
        variant.display_name or "",
        variant.description or "",
    )
    if _is_disqualified_tier(model_id, variant.display_name or "", variant.description or ""):
        return -1
    if not _is_fable_model_blob(
        _normalized_text(model_id, variant.display_name or "", variant.description or "")
    ) and "fable" not in model_id.lower():
        return -1

    params = _param_map(variant.params)
    score = 0
    if "5" in model_id or "fable-5" in blob or "fable 5" in blob:
        score += 20
    if params.get("thinking") in _TRUTHY_PARAM_VALUES:
        score += 40
    elif "thinking" in blob:
        score += 20
    effort = _effort_rank(variant.params)
    # Prefer high (not xhigh/max) for the standard high-thinking routing preset.
    if effort == 3:
        score += 35
    elif effort == 2:
        score += 15
    elif effort >= 4:
        score += 10
    if any(
        param.id.lower() in {"reasoning", "reasoning_effort"}
        and "high" in param.value.lower()
        for param in variant.params
    ):
        score += 20
    return score


def detect_fable_high_thinking(models: Sequence[SDKModel]) -> ModelSelection | None:
    """
    Pick a Fable 5 high-thinking preset from account model listings.

    Prefers ``thinking=true`` + ``effort=high`` on non-Max context; excludes fast.
    """
    best: tuple[int, ModelSelection] | None = None

    for model in models:
        model_base_rank = _fable_model_rank(model)
        if model_base_rank < 0 and not _is_fable_model_blob(
            _normalized_text(model.id, model.display_name or "", model.description or "")
        ):
            continue
        candidates: list[tuple[int, ModelSelection]] = []

        for variant in model.variants:
            rank = _fable_variant_rank(model.id, variant)
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
            combined = rank + max(model_base_rank, 0)
            if best is None or combined > best[0]:
                best = (combined, selection)

    return best[1] if best else None


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


def _is_composer_model(model: SDKModel) -> bool:
    blob = _normalized_text(model.id, model.display_name or "", model.description or "")
    return "composer" in blob


def _pick_composer_variant(
    models: Sequence[SDKModel],
    *,
    want_fast: bool,
) -> ModelSelection | None:
    """Pick Composer with explicit ``fast`` variant params when listed."""
    best: tuple[int, ModelSelection] | None = None
    for model in models:
        if not _is_composer_model(model):
            continue
        model_id = model.id.lower()
        # Prefer composer-2.5 over composer-2.
        base = 20 if "2.5" in model_id or "2-5" in model_id else 10
        if model.variants:
            for variant in model.variants:
                is_fast = _params_indicate_fast(variant.params)
                is_non_fast = _params_indicate_non_fast(variant.params)
                if want_fast and not is_fast:
                    continue
                if not want_fast and is_fast:
                    continue
                if not want_fast and not is_non_fast and variant.params:
                    # Variant has params but no explicit fast=false — skip.
                    continue
                rank = base + (5 if is_fast == want_fast else 0)
                selection = ModelSelection(id=model.id, params=tuple(variant.params))
                if best is None or rank > best[0]:
                    best = (rank, selection)
            continue

        # Legacy flat catalog ids.
        if want_fast and _is_fast_model_id(model.id, model.display_name or ""):
            selection = ModelSelection(id=model.id)
            if best is None or base > best[0]:
                best = (base, selection)
        elif not want_fast and not _is_fast_model_id(model.id, model.display_name or ""):
            selection = ModelSelection(id=model.id)
            if best is None or base > best[0]:
                best = (base, selection)
    return best[1] if best else None


def _detect_composer_fast(models: Sequence[SDKModel]) -> ModelSelection | None:
    return _pick_composer_variant(models, want_fast=True)


def _detect_composer_standard(models: Sequence[SDKModel]) -> ModelSelection:
    selected = _pick_composer_variant(models, want_fast=False)
    if selected is not None:
        return selected
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
    Prefers explicit ``fast=false`` / ``fast=true`` variant params when listed.
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


def _is_grok_model(model: SDKModel) -> bool:
    blob = _normalized_text(model.id, model.display_name or "", model.description or "")
    return "grok" in blob


def _pick_grok_variant(
    models: Sequence[SDKModel],
    *,
    want_fast: bool,
) -> ModelSelection | None:
    """Pick Grok with ``effort=high`` and the requested ``fast`` flag."""
    best: tuple[int, ModelSelection] | None = None
    for model in models:
        if not _is_grok_model(model):
            continue
        if _is_disqualified_tier(model.id, model.display_name or "", model.description or ""):
            # Allow base grok ids; only skip explicitly disqualified flat max ids.
            if "max" in model.id.lower():
                continue
        base = 20 if "4.5" in model.id or "4-5" in model.id else 10
        if not model.variants:
            if want_fast == _is_fast_model_id(model.id, model.display_name or ""):
                selection = ModelSelection(id=model.id)
                if best is None or base > best[0]:
                    best = (base, selection)
            continue
        for variant in model.variants:
            is_fast = _params_indicate_fast(variant.params)
            is_non_fast = _params_indicate_non_fast(variant.params)
            if want_fast and not is_fast:
                continue
            if not want_fast and is_fast:
                continue
            if not want_fast and variant.params and not is_non_fast:
                continue
            effort = _effort_rank(variant.params)
            # Prefer high effort for orchestration cycles (not low/medium).
            rank = base + (30 if effort == 3 else effort * 5)
            selection = ModelSelection(id=model.id, params=tuple(variant.params))
            if best is None or rank > best[0]:
                best = (rank, selection)
    return best[1] if best else None


def detect_grok_standard(models: Sequence[SDKModel]) -> ModelSelection | None:
    """Pick non-fast Grok (``effort=high``, ``fast=false`` when listed)."""
    return _pick_grok_variant(models, want_fast=False)


def detect_grok_fast(models: Sequence[SDKModel]) -> ModelSelection | None:
    """Pick fast Grok (``effort=high``, ``fast=true`` when listed)."""
    return _pick_grok_variant(models, want_fast=True)


def detect_grok(
    models: Sequence[SDKModel],
    *,
    grok_tier: str = "standard",
) -> ModelSelection | None:
    """
    Resolve Grok preset from listings.

    ``grok_tier`` may be ``standard``, ``fast``, or an explicit model id.
    Standard prefers non-fast high-effort variants (catalog default is often fast).
    """
    tier = grok_tier.strip() if grok_tier else "standard"
    if not tier:
        tier = "standard"
    normalized = tier.lower()
    if normalized not in {"standard", "fast"}:
        return ModelSelection(id=tier)

    if normalized == "fast":
        fast = detect_grok_fast(models)
        if fast is not None:
            return fast
        logger.warning(_GROK_FAST_UNAVAILABLE_WARNING)
        return detect_grok_standard(models)

    return detect_grok_standard(models)


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
    grok_tier: str = "standard",
    list_models: Callable[..., list[SDKModel]] | None = None,
    api_key: str | None = None,
) -> ModelCapabilities:
    """Inspect Cursor models and expose Composer / Grok / Fable / Opus presets."""
    if models is None:
        models = _list_cursor_models(list_models=list_models, api_key=api_key)

    opus = detect_opus_high_thinking(models)
    fable = detect_fable_high_thinking(models)
    composer_standard = _detect_composer_standard(models)
    composer_fast = _detect_composer_fast(models)
    grok_standard = detect_grok_standard(models)
    grok_fast = detect_grok_fast(models)
    grok = detect_grok(models, grok_tier=grok_tier)
    if grok is None:
        logger.warning(_GROK_UNAVAILABLE_WARNING)
    return ModelCapabilities(
        composer=detect_composer(models, composer_tier=composer_tier),
        composer_standard=composer_standard,
        composer_fast=composer_fast,
        grok_available=grok is not None,
        grok=grok,
        grok_standard=grok_standard,
        grok_fast=grok_fast,
        opus_available=opus is not None,
        opus=opus,
        fable_available=fable is not None,
        fable=fable,
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

    if caps.grok_available and caps.grok is not None:
        lines.append(
            f"Grok (complexity {GROK_COMPLEXITY_MIN}-{GROK_COMPLEXITY_MAX}): "
            f"{_format_selection(caps.grok)}"
        )
        lines.append("Grok route available: yes")
    else:
        lines.append(
            f"Grok (complexity {GROK_COMPLEXITY_MIN}-{GROK_COMPLEXITY_MAX}): unavailable"
        )
        lines.append("Grok route available: no")
        lines.append(
            f"Fallback for complexity {GROK_COMPLEXITY_MIN}-{GROK_COMPLEXITY_MAX}: "
            f"{_format_selection(caps.composer)}"
        )

    if caps.fable_available and caps.fable is not None:
        lines.append(
            f"Fable high-thinking (complexity {FABLE_COMPLEXITY_MIN}-10): "
            f"{_format_selection(caps.fable)}"
        )
        lines.append("Fable route available: yes")
    else:
        lines.append(
            f"Fable high-thinking (complexity {FABLE_COMPLEXITY_MIN}-10): unavailable"
        )
        lines.append("Fable route available: no")

    if caps.opus_available and caps.opus is not None:
        lines.append(
            f"Opus high-thinking (optional custom rules): {_format_selection(caps.opus)}"
        )
        lines.append("Opus route available: yes")
    else:
        lines.append("Opus high-thinking (optional custom rules): unavailable")
        lines.append("Opus route available: no")

    if not caps.fable_available:
        lines.append(
            f"Fallback for complexity {FABLE_COMPLEXITY_MIN}-10: "
            f"{_format_selection(caps.composer)}"
        )

    lines.extend(
        [
            "",
            "Default bands: Composer 1-5, Grok 6-8, Fable 9-10.",
            "Set composer_tier / grok_tier (standard|fast) in [routing] or at launch.",
        ]
    )
    return "\n".join(lines)
