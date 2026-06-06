"""Task pipeline model resolution for parse-prd and analyze-complexity."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from cursor_sdk import ModelSelection, SDKModel

from cyclopsctl.models import (
    ModelListingError,
    _list_cursor_models,
    detect_composer,
)

DEFAULT_PARSE_MODEL = "auto"
DEFAULT_ANALYZE_MODEL = "auto"
DEFAULT_MAX_TASKS: int | None = None
FALLBACK_PARSE_MODEL_ID = "composer-2.5"
FALLBACK_ANALYZE_MODEL_ID = "composer-2.5"

def _find_listed_composer(models: Sequence[SDKModel]) -> ModelSelection | None:
    """Return a standard Composer model when present in account listings."""
    for model in models:
        blob = _normalized_model_blob(
            model.id,
            model.display_name or "",
            model.description or "",
        )
        if "composer" in blob and "fast" not in blob:
            return ModelSelection(id=model.id)
    return None


def _normalized_model_blob(*parts: str) -> str:
    return " ".join(part.lower() for part in parts if part)


def _is_sonnet_model(model: SDKModel) -> bool:
    blob = _normalized_model_blob(
        model.id,
        model.display_name or "",
        model.description or "",
    )
    return "sonnet" in blob


def detect_sonnet_model(models: Sequence[SDKModel]) -> ModelSelection | None:
    """Prefer a Sonnet-class model from Cursor model listings."""
    best: tuple[int, ModelSelection] | None = None
    for model in models:
        if not _is_sonnet_model(model):
            continue
        blob = _normalized_model_blob(
            model.id,
            model.display_name or "",
            model.description or "",
        )
        score = 0
        if "claude" in blob:
            score += 10
        if "4" in model.id:
            score += 5
        selection = ModelSelection(id=model.id)
        if best is None or score > best[0]:
            best = (score, selection)
    return best[1] if best else None


def _log_progress(message: str, *, log_fn: Callable[[str], None] | None) -> None:
    if log_fn is not None:
        log_fn(message)
    else:
        print(message, file=sys.stderr)


def resolve_parse_model(
    parse_model: str,
    *,
    api_key: str | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> ModelSelection:
    """Resolve the Cursor model used for structured PRD parsing."""
    configured = parse_model.strip() or DEFAULT_PARSE_MODEL
    if configured.lower() != "auto":
        return ModelSelection(id=configured)

    try:
        models = _list_cursor_models(list_models=list_models, api_key=api_key)
    except ModelListingError:
        _log_progress(
            f"model listing unavailable; falling back to {FALLBACK_PARSE_MODEL_ID}",
            log_fn=log_fn,
        )
        return ModelSelection(id=FALLBACK_PARSE_MODEL_ID)

    # Native cyclopsctl runs always use local Cursor SDK agents. Sonnet-class
    # models are often listed in the account catalog but fail on local runtime;
    # Composer is the reliable default for parse-prd (same as analyze-complexity).
    composer = _find_listed_composer(models)
    if composer is not None:
        _log_progress(
            f"using Composer model {composer.id!r} for structured PRD parsing",
            log_fn=log_fn,
        )
        return composer

    _log_progress(
        f"no Composer model in account listings; using default "
        f"{FALLBACK_PARSE_MODEL_ID!r} for structured PRD parsing",
        log_fn=log_fn,
    )
    return ModelSelection(id=FALLBACK_PARSE_MODEL_ID)


def resolve_analyze_model(
    analyze_model: str,
    *,
    api_key: str | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> ModelSelection:
    """Resolve the Cursor model used for complexity analysis."""
    configured = analyze_model.strip() or DEFAULT_ANALYZE_MODEL
    if configured.lower() != "auto":
        return ModelSelection(id=configured)

    try:
        models = _list_cursor_models(list_models=list_models, api_key=api_key)
    except ModelListingError:
        _log_progress(
            f"model listing unavailable; falling back to {FALLBACK_ANALYZE_MODEL_ID}",
            log_fn=log_fn,
        )
        return ModelSelection(id=FALLBACK_ANALYZE_MODEL_ID)

    return detect_composer(models, composer_tier="standard")
