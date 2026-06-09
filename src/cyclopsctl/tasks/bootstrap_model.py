"""Init-time bootstrap model selection and Sonnet→Composer fallback."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cursor_sdk import ModelSelection, SDKModel

from cyclopsctl.models import (
    COMPOSER_MODEL_ID,
    ModelListingError,
    _list_cursor_models,
    detect_composer,
    detect_fable_high_thinking,
    detect_opus_high_thinking,
    detect_opus_max,
    detect_sonnet_max,
)
from cyclopsctl.runner import AgentRunError, RunFailureKind, is_opus_billing_failure
from cyclopsctl.tasks.models import (
    DEFAULT_ANALYZE_MODEL,
    DEFAULT_PARSE_MODEL,
    detect_sonnet_model,
    resolve_analyze_model,
    resolve_parse_model,
)

BOOTSTRAP_PRESET_AUTO = "auto"
BOOTSTRAP_PRESET_COMPOSER = "composer"
BOOTSTRAP_PRESET_SONNET = "sonnet"
BOOTSTRAP_PRESET_FABLE = "fable-high-thinking"
BOOTSTRAP_PRESET_OPUS = "opus-high-thinking"
BOOTSTRAP_PRESET_SONNET_MAX = "sonnet-max"
BOOTSTRAP_PRESET_OPUS_MAX = "opus-max"

_BOOTSTRAP_PRESETS: tuple[tuple[str, str, str], ...] = (
    (
        "1",
        BOOTSTRAP_PRESET_AUTO,
        "Auto — try Sonnet first, fall back to Composer if unavailable",
    ),
    (
        "2",
        BOOTSTRAP_PRESET_COMPOSER,
        "Composer — included with Cursor; fine for simple PRDs (no extra API credits)",
    ),
    (
        "3",
        BOOTSTRAP_PRESET_SONNET,
        "Sonnet — strong JSON and decomposition; good quality/cost balance (API credits)",
    ),
    (
        "4",
        BOOTSTRAP_PRESET_FABLE,
        "Fable 5 — high-thinking preset for strong decomposition (API credits)",
    ),
    (
        "5",
        BOOTSTRAP_PRESET_OPUS,
        "Opus 4.8 — deepest decomposition for large, technical PRDs (higher API credits)",
    ),
    (
        "6",
        BOOTSTRAP_PRESET_SONNET_MAX,
        "Sonnet Max — extended context for very long PRDs (premium API credits)",
    ),
    (
        "7",
        BOOTSTRAP_PRESET_OPUS_MAX,
        "Opus 4.8 Max — highest quality for the most complex PRDs (premium API credits)",
    ),
)

_PRESET_ALIASES: dict[str, str] = {
    "a": BOOTSTRAP_PRESET_AUTO,
    "auto": BOOTSTRAP_PRESET_AUTO,
    "c": BOOTSTRAP_PRESET_COMPOSER,
    "composer": BOOTSTRAP_PRESET_COMPOSER,
    "s": BOOTSTRAP_PRESET_SONNET,
    "sonnet": BOOTSTRAP_PRESET_SONNET,
    "f": BOOTSTRAP_PRESET_FABLE,
    "fable": BOOTSTRAP_PRESET_FABLE,
    "fable-high-thinking": BOOTSTRAP_PRESET_FABLE,
    "fable-5": BOOTSTRAP_PRESET_FABLE,
    "o": BOOTSTRAP_PRESET_OPUS,
    "opus": BOOTSTRAP_PRESET_OPUS,
    "opus-high-thinking": BOOTSTRAP_PRESET_OPUS,
    "opus-4.8": BOOTSTRAP_PRESET_OPUS,
    "sonnet-max": BOOTSTRAP_PRESET_SONNET_MAX,
    "sonnet_max": BOOTSTRAP_PRESET_SONNET_MAX,
    "opus-max": BOOTSTRAP_PRESET_OPUS_MAX,
    "opus_max": BOOTSTRAP_PRESET_OPUS_MAX,
}

_MENU_KEY_TO_PRESET: dict[str, str] = {
    key: preset for key, preset, _label in _BOOTSTRAP_PRESETS
}

_LISTING_PRESETS = frozenset(
    {
        BOOTSTRAP_PRESET_AUTO,
        BOOTSTRAP_PRESET_COMPOSER,
        BOOTSTRAP_PRESET_SONNET,
        BOOTSTRAP_PRESET_FABLE,
        BOOTSTRAP_PRESET_OPUS,
        BOOTSTRAP_PRESET_SONNET_MAX,
        BOOTSTRAP_PRESET_OPUS_MAX,
    }
)


@dataclass(frozen=True)
class BootstrapTasksSettings:
    """Parse/analyze model settings chosen for init bootstrap."""

    parse_model: str
    analyze_model: str
    max_tasks: int | None = None


def bootstrap_preset_names() -> tuple[str, ...]:
    """Return configured bootstrap preset ids for CLI help and validation."""
    return tuple(preset for _key, preset, _label in _BOOTSTRAP_PRESETS)


def normalize_bootstrap_preset(value: str) -> str:
    """Normalize a bootstrap preset or explicit model id."""
    stripped = value.strip()
    if not stripped:
        return BOOTSTRAP_PRESET_AUTO
    lowered = stripped.lower()
    return _PRESET_ALIASES.get(lowered, stripped)


def format_bootstrap_model_menu() -> str:
    """Render the interactive init bootstrap model menu."""
    lines = [
        "Choose the AI model for PRD parsing and complexity analysis:",
        "(The same model is used for both steps.)",
        "",
    ]
    for key, _preset, label in _BOOTSTRAP_PRESETS:
        default = " [default]" if key == "1" else ""
        lines.append(f"  {key}) {label}{default}")
    lines.append("")
    return "\n".join(lines)


def prompt_bootstrap_model_choice(
    *,
    stdin_is_tty: bool | None = None,
) -> str:
    """Prompt on a TTY for bootstrap model preset; default ``auto``."""
    is_tty = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    if not is_tty:
        return BOOTSTRAP_PRESET_AUTO

    max_key = _BOOTSTRAP_PRESETS[-1][0]
    print(format_bootstrap_model_menu(), file=sys.stderr, end="")
    try:
        answer = input(f"Enter 1-{max_key} [1]: ").strip().lower()
    except EOFError:
        return BOOTSTRAP_PRESET_AUTO

    if not answer or answer in {"1", "a", "auto"}:
        return BOOTSTRAP_PRESET_AUTO
    if answer in _MENU_KEY_TO_PRESET:
        return _MENU_KEY_TO_PRESET[answer]
    return normalize_bootstrap_preset(answer)


def _resolve_composer_selection(
    models: Sequence[SDKModel] | None,
    *,
    api_key: str | None,
    list_models: Callable[..., list[SDKModel]] | None,
) -> ModelSelection:
    if models is None:
        try:
            models = _list_cursor_models(list_models=list_models, api_key=api_key)
        except ModelListingError:
            return ModelSelection(id=COMPOSER_MODEL_ID)
    return detect_composer(models, composer_tier="standard")


def _resolve_sonnet_selection(
    models: Sequence[SDKModel] | None,
    *,
    api_key: str | None,
    list_models: Callable[..., list[SDKModel]] | None,
) -> ModelSelection | None:
    if models is None:
        try:
            models = _list_cursor_models(list_models=list_models, api_key=api_key)
        except ModelListingError:
            return None
    return detect_sonnet_model(models)


def _dedupe_model_chain(chain: Sequence[ModelSelection]) -> list[ModelSelection]:
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    ordered: list[ModelSelection] = []
    for selection in chain:
        params_key = tuple(
            (param.id, param.value) for param in (selection.params or ())
        )
        key = (selection.id, params_key)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(selection)
    return ordered


def _credit_aware_fallback_chain(
    *candidates: ModelSelection | None,
    composer: ModelSelection,
) -> list[ModelSelection]:
    """Build an ordered attempt chain ending with Composer."""
    chain = _dedupe_model_chain([*candidates, composer])
    return chain if chain else [composer]


def bootstrap_model_attempt_chain(
    configured: str,
    *,
    api_key: str | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
) -> list[ModelSelection]:
    """
    Return ordered model attempts for a bootstrap preset or explicit id.

    ``auto`` and ``sonnet`` try Sonnet-class first, then Composer.
    ``composer`` and explicit Composer ids use Composer only.
    Premium presets fall back through weaker tiers, then Composer.
    Other explicit ids run once with no automatic fallback.
    """
    preset = normalize_bootstrap_preset(configured)

    if preset not in _LISTING_PRESETS:
        explicit = ModelSelection(id=preset)
        blob = preset.lower()
        if "sonnet" in blob or "fable" in blob:
            return [explicit, ModelSelection(id=COMPOSER_MODEL_ID)]
        if "opus" in blob:
            return _credit_aware_fallback_chain(
                explicit,
                composer=ModelSelection(id=COMPOSER_MODEL_ID),
            )
        return [explicit]

    models: Sequence[SDKModel] | None = None
    try:
        models = _list_cursor_models(list_models=list_models, api_key=api_key)
    except ModelListingError:
        models = None

    composer = _resolve_composer_selection(
        models, api_key=api_key, list_models=list_models
    )

    sonnet = _resolve_sonnet_selection(
        models, api_key=api_key, list_models=list_models
    )

    if preset == BOOTSTRAP_PRESET_COMPOSER:
        return [composer]

    if preset == BOOTSTRAP_PRESET_SONNET:
        if sonnet is not None:
            return [sonnet, composer]
        return [composer]

    if preset == BOOTSTRAP_PRESET_AUTO:
        if sonnet is not None:
            return [sonnet, composer]
        return [composer]

    if preset == BOOTSTRAP_PRESET_FABLE:
        fable = detect_fable_high_thinking(models) if models else None
        return _credit_aware_fallback_chain(fable, sonnet, composer=composer)

    if preset == BOOTSTRAP_PRESET_OPUS:
        opus = detect_opus_high_thinking(models) if models else None
        return _credit_aware_fallback_chain(opus, sonnet, composer=composer)

    if preset == BOOTSTRAP_PRESET_SONNET_MAX:
        sonnet_max = detect_sonnet_max(models) if models else None
        return _credit_aware_fallback_chain(sonnet_max, sonnet, composer=composer)

    if preset == BOOTSTRAP_PRESET_OPUS_MAX:
        opus_max = detect_opus_max(models) if models else None
        opus = detect_opus_high_thinking(models) if models else None
        return _credit_aware_fallback_chain(
            opus_max,
            opus,
            sonnet,
            composer=composer,
        )

    return [composer]


def should_fallback_bootstrap_model(
    exc: AgentRunError,
    *,
    attempted: ModelSelection,
    remaining: Sequence[ModelSelection],
) -> bool:
    """Return whether to retry bootstrap with the next model candidate."""
    if not remaining:
        return False
    if "composer" in attempted.id.lower():
        return False

    detail = str(exc.result_detail or exc).lower()
    if is_opus_billing_failure(detail):
        return True
    credit_hints = (
        "credit",
        "billing",
        "quota",
        "usage limit",
        "payment required",
        "insufficient",
    )
    if any(hint in detail for hint in credit_hints):
        return True
    return exc.kind == RunFailureKind.RUN


def resolve_bootstrap_tasks_settings(
    *,
    bootstrap_model: str | None,
    parse_model: str | None,
    analyze_model: str | None,
    max_tasks: int | None,
    file_cfg: dict,
    stdin_is_tty: bool | None = None,
    interactive: bool = True,
) -> BootstrapTasksSettings:
    """
    Resolve parse/analyze model strings for init bootstrap.

    ``bootstrap_model`` sets both parse and analyze when provided.
    Otherwise uses explicit parse/analyze overrides, file config, or prompt.
    """
    if bootstrap_model is not None and bootstrap_model.strip():
        preset = normalize_bootstrap_preset(bootstrap_model)
        return BootstrapTasksSettings(
            parse_model=preset,
            analyze_model=preset,
            max_tasks=max_tasks,
        )

    if parse_model is not None and parse_model.strip():
        chosen = normalize_bootstrap_preset(parse_model)
        analyze = (
            normalize_bootstrap_preset(analyze_model)
            if analyze_model is not None and analyze_model.strip()
            else chosen
        )
        return BootstrapTasksSettings(
            parse_model=chosen,
            analyze_model=analyze,
            max_tasks=max_tasks,
        )

    from cyclopsctl.config import parse_tasks_config

    tasks_cfg = parse_tasks_config({}, file_cfg)
    if (
        tasks_cfg.parse_model != DEFAULT_PARSE_MODEL
        or tasks_cfg.analyze_model != DEFAULT_ANALYZE_MODEL
    ):
        return BootstrapTasksSettings(
            parse_model=tasks_cfg.parse_model,
            analyze_model=tasks_cfg.analyze_model,
            max_tasks=max_tasks or tasks_cfg.max_tasks,
        )

    if interactive:
        preset = prompt_bootstrap_model_choice(stdin_is_tty=stdin_is_tty)
        return BootstrapTasksSettings(
            parse_model=preset,
            analyze_model=preset,
            max_tasks=max_tasks or tasks_cfg.max_tasks,
        )

    return BootstrapTasksSettings(
        parse_model=BOOTSTRAP_PRESET_AUTO,
        analyze_model=BOOTSTRAP_PRESET_AUTO,
        max_tasks=max_tasks or tasks_cfg.max_tasks,
    )


def persist_bootstrap_tasks_settings(
    config_path: Path,
    settings: BootstrapTasksSettings,
) -> None:
    """Write ``[tasks].parse_model`` and ``analyze_model`` into cyclopsctl.toml."""
    if not config_path.is_file():
        return

    text = config_path.read_text(encoding="utf-8")
    updates = {
        "parse_model": settings.parse_model,
        "analyze_model": settings.analyze_model,
    }
    if settings.max_tasks is not None:
        updates["max_tasks"] = str(settings.max_tasks)

    for key, value in updates.items():
        pattern = rf'^(\s*{re.escape(key)}\s*=\s*)(".*?"|\'.*?\'|[^\n#]+)'
        replacement = f'\\1"{value}"'
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
        elif "[tasks]" in text:
            text = text.replace(
                "[tasks]\n",
                f'[tasks]\n{key} = "{value}"\n',
                1,
            )
    config_path.write_text(text, encoding="utf-8")


def resolve_configured_bootstrap_model(
    configured: str,
    *,
    api_key: str | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
    log_fn: Callable[[str], None] | None = None,
    for_analyze: bool = False,
) -> ModelSelection:
    """Resolve the first model attempt for logging/display (not the full chain)."""
    chain = bootstrap_model_attempt_chain(
        configured,
        api_key=api_key,
        list_models=list_models,
    )
    if chain:
        return chain[0]
    if for_analyze:
        return resolve_analyze_model(
            configured,
            api_key=api_key,
            list_models=list_models,
            log_fn=log_fn,
        )
    return resolve_parse_model(
        configured,
        api_key=api_key,
        list_models=list_models,
        log_fn=log_fn,
    )
