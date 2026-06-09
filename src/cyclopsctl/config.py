"""CLI flags, TOML config, path normalization, and validation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

if TYPE_CHECKING:
    from cyclopsctl.routing import RoutingConfig
    from cyclopsctl.tasks.backend import TaskBackendKind

TaskSource = Literal["handover", "sequential"]
RetryOn = Literal["off", "transient"]

VALID_TASK_SOURCES: frozenset[str] = frozenset({"handover", "sequential"})
DEFAULT_TASK_SOURCE: TaskSource = "handover"


def normalize_task_source(value: str) -> TaskSource:
    """Validate and normalize a task-source setting."""
    normalized = str(value).strip().lower()
    if normalized not in VALID_TASK_SOURCES:
        allowed = ", ".join(sorted(VALID_TASK_SOURCES))
        raise ValueError(
            f"task-source must be one of: {allowed} (got: {value!r})"
        )
    return normalized  # type: ignore[return-value]

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_COMPLEXITY_REPORT = Path(".cyclopsctl/reports/complexity-report.json")
DEFAULT_CURRENT_HANDOVER = Path("current-handover-prompt.md")
DEFAULT_FIRST_PROMPT = Path("prompts/setup-ai-workflow.md")
DEFAULT_UPDATE_HANDOVER = Path("update-handover-prompt.md")
DEFAULT_AI_CONTEXT = Path("ai-context.md")
DEFAULT_AI_CONTEXT_MAX_CHARS = 100_000
DEFAULT_MODEL = "composer-2.5"
DEFAULT_RETRY_ON: RetryOn = "transient"
DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 15)
DEFAULT_STATE_FILE = Path(".cyclopsctl/state.json")
DEFAULT_HISTORY_FILE = Path(".cyclopsctl/run-history.json")

class ConfigError(ValueError):
    """Invalid or incomplete cyclopsctl configuration."""


@dataclass(frozen=True)
class TasksConfig:
    """Task backend and bootstrap model settings from ``[tasks]`` in cyclopsctl.toml."""

    backend: TaskBackendKind = "native"  # type: ignore[assignment]
    parse_model: str = "auto"
    analyze_model: str = "auto"
    max_tasks: int | None = None


def parse_tasks_config(
    cli: Mapping[str, Any],
    file_cfg: Mapping[str, Any],
    *,
    project_root: Path | None = None,
) -> TasksConfig:
    """Parse ``[tasks]`` settings with CLI overrides."""
    from cyclopsctl.tasks.backend import resolve_task_backend
    from cyclopsctl.tasks.models import (
        DEFAULT_ANALYZE_MODEL,
        DEFAULT_PARSE_MODEL,
    )

    backend = resolve_task_backend(cli, file_cfg, project_root=project_root)

    tasks_section = file_cfg.get("tasks")
    section: Mapping[str, Any] = (
        tasks_section if isinstance(tasks_section, dict) else {}
    )

    def _section_pick(key: str) -> Any:
        if key in cli and cli[key] is not None:
            return cli[key]
        if key in section and section[key] is not None:
            return section[key]
        if key in file_cfg and file_cfg[key] is not None:
            return file_cfg[key]
        return None

    parse_model_raw = _section_pick("parse_model")
    parse_model = (
        str(parse_model_raw).strip()
        if parse_model_raw is not None and str(parse_model_raw).strip()
        else DEFAULT_PARSE_MODEL
    )

    analyze_model_raw = _section_pick("analyze_model")
    analyze_model = (
        str(analyze_model_raw).strip()
        if analyze_model_raw is not None and str(analyze_model_raw).strip()
        else DEFAULT_ANALYZE_MODEL
    )

    max_tasks_raw = _section_pick("max_tasks")
    if max_tasks_raw is None:
        max_tasks_raw = _section_pick("default_num_tasks")
    max_tasks: int | None = None
    if max_tasks_raw is not None:
        try:
            parsed_max = int(max_tasks_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"tasks.max-tasks must be a positive integer (got: {max_tasks_raw!r})"
            ) from exc
        if parsed_max < 1:
            raise ConfigError(
                f"tasks.max-tasks must be at least 1 (got: {parsed_max})"
            )
        max_tasks = parsed_max

    return TasksConfig(
        backend=backend,
        parse_model=parse_model,
        analyze_model=analyze_model,
        max_tasks=max_tasks,
    )


@dataclass(frozen=True)
class DoctorConfig:
    """Validated runtime configuration for ``cyclopsctl doctor``."""

    project_root: Path
    current_handover: Path
    complexity_report: Path
    task_backend: TaskBackendKind = "native"  # type: ignore[assignment]
    tag: str | None = None
    plain: bool = False
    fix: bool = False
    state_file: Path | None = None


@dataclass(frozen=True)
class LaunchConfig:
    """Validated runtime configuration for ``cyclopsctl launch``."""

    project_root: Path
    first_prompt: Path
    current_handover: Path
    update_handover: Path
    complexity_report: Path
    ai_context: Path
    config_path: Path | None = None
    task_backend: TaskBackendKind = "native"  # type: ignore[assignment]
    tag: str | None = None
    plain: bool = False
    state_file: Path | None = None
    history_file: Path | None = None


@dataclass(frozen=True)
class StatusConfig:
    """Validated runtime configuration for ``cyclopsctl status``."""

    project_root: Path
    state_file: Path | None = None
    history_file: Path | None = None


@dataclass(frozen=True)
class CyclopsctlConfig:
    """Validated runtime configuration for ``cyclopsctl run``."""

    project_root: Path
    cycles: int
    first_prompt: Path
    current_handover: Path
    update_handover: Path
    complexity_report: Path
    ai_context: Path
    require_ai_context: bool = False
    ai_context_max_chars: int | None = DEFAULT_AI_CONTEXT_MAX_CHARS
    tag: str | None = None
    default_model: str = DEFAULT_MODEL
    strict_handover: bool = False
    task_source: TaskSource = DEFAULT_TASK_SOURCE
    pinned_task_id: int | None = None
    plain: bool = False
    retry_on: RetryOn = DEFAULT_RETRY_ON
    retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    retry_backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS
    state_file: Path | None = None
    history_file: Path | None = None
    fresh: bool = False
    resume: bool = False
    dry_run: bool = False
    git_summary: bool = False
    export_transcript_dir: Path | None = None
    cycle_log: Path | None = None
    routing: RoutingConfig | None = None
    task_backend: TaskBackendKind = "native"  # type: ignore[assignment]

    @property
    def retry_transient_enabled(self) -> bool:
        return self.retry_on == "transient"

    def first_prompt_text(self) -> str:
        return self.first_prompt.read_text(encoding="utf-8")

    def current_handover_text(self) -> str:
        return self.current_handover.read_text(encoding="utf-8")

    def update_handover_text(self) -> str:
        return self.update_handover.read_text(encoding="utf-8")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a TOML table: {path}")
    return data


def load_config_file(path: Path | None) -> dict[str, Any]:
    """Load ``cyclopsctl.toml`` (or path given by ``--config``)."""
    if path is None:
        return {}
    return _read_toml(path.resolve())


def load_effective_config(
    path: Path | None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Load TOML and merge base settings with an optional named profile."""
    from cyclopsctl.profiles import ProfileError, merge_effective_file_config

    if profile is not None and path is None:
        raise ConfigError("Cannot use --profile without --config")
    raw = load_config_file(path)
    try:
        return merge_effective_file_config(raw, profile)
    except ProfileError as exc:
        raise ConfigError(str(exc)) from exc


def _pick(
    key: str,
    cli: Mapping[str, Any],
    file_cfg: Mapping[str, Any],
) -> Any:
    if key in cli and cli[key] is not None:
        return cli[key]
    if key in file_cfg and file_cfg[key] is not None:
        return file_cfg[key]
    return None


def resolve_project_root(value: Path | None) -> Path:
    """Resolve project root from CLI/TOML; default to the current working directory."""
    if value is None:
        return Path.cwd().resolve()
    return Path(value).expanduser().resolve()


def _resolve_project_path(value: Path, project_root: Path, label: str) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_file():
        raise ConfigError(f"{label} not found: {candidate}")
    return candidate


def _resolve_ai_context_path(value: Path | None, project_root: Path) -> Path:
    if value is None:
        return (project_root / DEFAULT_AI_CONTEXT).resolve()
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def _resolve_project_relative_path(
    value: Path | None,
    project_root: Path,
    default: Path,
) -> Path:
    """Resolve a path under ``project_root`` without requiring the file to exist."""
    if value is None:
        return (project_root / default).resolve()
    candidate = value.expanduser()
    if not candidate.is_absolute():
        return (project_root / candidate).resolve()
    return candidate.resolve()


def _resolve_state_file_path(
    value: Path | str | None,
    project_root: Path,
) -> Path | None:
    """Resolve optional run-state path; empty string disables persistence."""
    if value is None:
        return (project_root / DEFAULT_STATE_FILE).resolve()
    if str(value).strip() in {"", "."}:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return (project_root / candidate).resolve()
    return candidate.resolve()


def _resolve_state_file_setting(
    raw: object | None,
    project_root: Path,
) -> Path | None:
    """Apply default, custom, or disabled run-state path settings."""
    if raw is not None and str(raw).strip() in {"", "."}:
        return None
    if raw is None:
        return _resolve_state_file_path(None, project_root)
    return _resolve_state_file_path(Path(raw), project_root)


def _resolve_history_file_path(
    value: Path | str | None,
    project_root: Path,
) -> Path | None:
    """Resolve optional run-history path; empty string disables persistence."""
    if value is None:
        return (project_root / DEFAULT_HISTORY_FILE).resolve()
    if str(value).strip() in {"", "."}:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return (project_root / candidate).resolve()
    return candidate.resolve()


def _resolve_history_file_setting(
    raw: object | None,
    project_root: Path,
) -> Path | None:
    """Apply default, custom, or disabled run-history path settings."""
    if raw is not None and str(raw).strip() in {"", "."}:
        return None
    if raw is None:
        return _resolve_history_file_path(None, project_root)
    return _resolve_history_file_path(Path(raw), project_root)


def _merge_run_section(file_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten optional ``[run]`` table into top-level config keys."""
    merged = dict(file_cfg)
    run_section = file_cfg.get("run")
    if isinstance(run_section, dict):
        for key in ("dry_run", "git_summary", "export_transcript_dir", "cycle_log"):
            if key in run_section and run_section[key] is not None:
                merged[key] = run_section[key]
    return merged


def _resolve_export_transcript_dir(
    raw: object | None,
    project_root: Path,
) -> Path | None:
    """Resolve optional transcript export directory under project_root when relative."""
    if raw is None:
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        return (project_root / candidate).resolve()
    return candidate.resolve()


def _resolve_cycle_log(
    raw: object | None,
    project_root: Path,
) -> Path | None:
    """Resolve the optional durable JSONL cycle-log path under project_root when relative."""
    if raw is None:
        return None
    if str(raw).strip() in {"", "."}:
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        return (project_root / candidate).resolve()
    return candidate.resolve()


def normalize_retry_on(value: str) -> RetryOn:
    """Validate and normalize a retry-on setting."""
    normalized = str(value).strip().lower()
    if normalized in {"", "off", "none", "false"}:
        return "off"
    if normalized == "transient":
        return "transient"
    raise ValueError(
        f"retry-on must be 'transient' or off (got: {value!r})"
    )


def _parse_retry_backoff_seconds(raw: object) -> tuple[int, ...]:
    if raw is None:
        return DEFAULT_RETRY_BACKOFF_SECONDS
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        values = [raw]
    if not values:
        return DEFAULT_RETRY_BACKOFF_SECONDS
    parsed: list[int] = []
    for item in values:
        try:
            seconds = int(item)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"retry-backoff-seconds values must be positive integers (got: {raw!r})"
            ) from exc
        if seconds < 1:
            raise ConfigError(
                f"retry-backoff-seconds values must be at least 1 (got: {seconds})"
            )
        parsed.append(seconds)
    return tuple(parsed)


def _apply_routing_cli_overrides(
    routing: RoutingConfig | None,
    cli: Mapping[str, Any],
) -> RoutingConfig | None:
    """Merge launcher/run CLI routing overrides onto file-based routing."""
    from dataclasses import replace

    from cyclopsctl.routing import RoutingConfig as ResolvedRoutingConfig

    composer_tier_raw = cli.get("composer_tier")
    opus_enabled_raw = cli.get("opus_enabled")
    if composer_tier_raw is None and opus_enabled_raw is None:
        return routing

    base = routing if routing is not None else ResolvedRoutingConfig()
    updates: dict[str, Any] = {}
    if composer_tier_raw is not None:
        tier = str(composer_tier_raw).strip()
        if not tier:
            raise ConfigError("composer-tier must be a non-empty string")
        updates["composer_tier"] = tier
    if opus_enabled_raw is not None:
        updates["opus_enabled"] = bool(opus_enabled_raw)
    return replace(base, **updates)


def _parse_routing_section(
    file_cfg: Mapping[str, Any],
    project_root: Path,
) -> RoutingConfig | None:
    """Parse optional ``[routing]`` table from cyclopsctl config."""
    from cyclopsctl.routing import (
        RoutingConfigError,
        load_routing_config_from_json,
        parse_routing_config,
    )

    routing_raw = file_cfg.get("routing")
    if routing_raw is None:
        return None
    if not isinstance(routing_raw, dict):
        raise ConfigError("routing section must be a TOML table")

    merged: dict[str, Any] = dict(routing_raw)
    rules_file = merged.pop("rules_file", None)
    if rules_file is not None:
        if not isinstance(rules_file, str) or not rules_file.strip():
            raise ConfigError("routing.rules_file must be a non-empty path string")
        candidate = Path(rules_file).expanduser()
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            file_rules = load_routing_config_from_json(candidate)
        except RoutingConfigError as exc:
            raise ConfigError(str(exc)) from exc
        merged.setdefault("rules", list(file_rules.rules))
        if file_rules.fallback is not None and "fallback" not in merged:
            fallback = file_rules.fallback
            merged["fallback"] = {
                key: value
                for key, value in {
                    "model": fallback.model,
                    "missing_score": fallback.missing_score,
                }.items()
                if value is not None
            }
        merged.setdefault("composer_tier", file_rules.composer_tier)
        merged.setdefault("opus_enabled", file_rules.opus_enabled)

    try:
        return parse_routing_config(merged)
    except RoutingConfigError as exc:
        raise ConfigError(str(exc)) from exc


def default_complexity_report_rel(task_backend: str = "native") -> Path:
    """Return the default complexity report path (native storage)."""
    _ = task_backend
    return DEFAULT_COMPLEXITY_REPORT


def resolve_default_complexity_report_path(
    project_root: Path,
    *,
    task_backend: str = "native",
) -> Path:
    """Pick an existing complexity report path or the native default."""
    _ = task_backend
    return (project_root / DEFAULT_COMPLEXITY_REPORT).resolve()


def _resolve_optional_report(
    value: Path | None,
    project_root: Path,
    *,
    task_backend: str = "native",
) -> Path:
    if value is None:
        candidate = resolve_default_complexity_report_path(
            project_root,
            task_backend=task_backend,
        )
    else:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
    if not candidate.is_file():
        raise ConfigError(f"complexity-report not found: {candidate}")
    return candidate


def build_run_config(
    cli: Mapping[str, Any],
    file_cfg: Mapping[str, Any] | None = None,
) -> CyclopsctlConfig:
    """Merge CLI and file settings, normalize paths, and validate inputs."""
    file_values = _merge_run_section(file_cfg or {})

    cycles_raw = _pick("cycles", cli, file_values)
    if cycles_raw is None:
        raise ConfigError("Missing required setting: cycles")
    try:
        cycles = int(cycles_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"cycles must be a positive integer (got: {cycles_raw!r})") from exc
    if cycles < 1:
        raise ConfigError(f"cycles must be at least 1 (got: {cycles})")

    project_root_raw = _pick("project_root", cli, file_values)
    project_root = resolve_project_root(
        Path(project_root_raw) if project_root_raw is not None else None
    )
    if not project_root.is_dir():
        raise ConfigError(f"project-root is not a directory: {project_root}")

    first_raw = _pick("first_prompt", cli, file_values)
    current_raw = _pick("current_handover", cli, file_values)
    if current_raw is None:
        raise ConfigError("Missing required setting: current-handover")
    update_raw = _pick("update_handover", cli, file_values)
    if update_raw is None:
        raise ConfigError("Missing required setting: update-handover")

    # first-prompt is only read for cycle 1 when handover is missing or --fresh;
    # do not require the file at config load time (see loop validation).
    first_prompt = _resolve_project_relative_path(
        Path(first_raw) if first_raw is not None else None,
        project_root,
        DEFAULT_FIRST_PROMPT,
    )
    current_handover = _resolve_project_path(
        Path(current_raw), project_root, "current-handover"
    )
    update_handover = _resolve_project_path(
        Path(update_raw), project_root, "update-handover"
    )

    from cyclopsctl.tasks.backend import resolve_task_backend

    task_backend = resolve_task_backend(cli, file_values, project_root=project_root)

    report_raw = _pick("complexity_report", cli, file_values)
    complexity_report = _resolve_optional_report(
        Path(report_raw) if report_raw is not None else None,
        project_root,
        task_backend=task_backend,
    )

    tag_raw = _pick("tag", cli, file_values)
    tag = str(tag_raw).strip() if tag_raw is not None and str(tag_raw).strip() else None

    model_raw = _pick("default_model", cli, file_values)
    default_model = (
        str(model_raw).strip() if model_raw is not None and str(model_raw).strip() else DEFAULT_MODEL
    )

    ai_context_raw = _pick("ai_context", cli, file_values)
    ai_context = _resolve_ai_context_path(
        Path(ai_context_raw) if ai_context_raw is not None else None,
        project_root,
    )

    require_ai_context = bool(_pick("require_ai_context", cli, file_values))

    max_chars_raw = _pick("ai_context_max_chars", cli, file_values)
    if max_chars_raw is None:
        ai_context_max_chars: int | None = DEFAULT_AI_CONTEXT_MAX_CHARS
    else:
        try:
            ai_context_max_chars = int(max_chars_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"ai-context-max-chars must be a positive integer (got: {max_chars_raw!r})"
            ) from exc
        if ai_context_max_chars < 1:
            raise ConfigError(
                f"ai-context-max-chars must be at least 1 (got: {ai_context_max_chars})"
            )

    if require_ai_context and not ai_context.is_file():
        raise ConfigError(f"ai-context not found (required): {ai_context}")

    plain_raw = _pick("plain", cli, file_values)
    plain = bool(plain_raw) if plain_raw is not None else False

    strict_handover = bool(_pick("strict_handover", cli, file_values))

    task_source_raw = _pick("task_source", cli, file_values)
    if task_source_raw is None:
        task_source: TaskSource = DEFAULT_TASK_SOURCE
    else:
        try:
            task_source = normalize_task_source(str(task_source_raw))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    pinned_raw = _pick("pinned_task_id", cli, file_values)
    if pinned_raw is None and "task_id" in file_values:
        pinned_raw = file_values["task_id"]
    pinned_task_id: int | None
    if pinned_raw is None:
        pinned_task_id = None
    else:
        try:
            pinned_task_id = int(pinned_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"task-id must be a positive integer (got: {pinned_raw!r})"
            ) from exc
        if pinned_task_id < 1:
            raise ConfigError(
                f"task-id must be at least 1 (got: {pinned_task_id})"
            )

    retry_on_raw = _pick("retry_on", cli, file_values)
    if retry_on_raw is None:
        retry_on: RetryOn = DEFAULT_RETRY_ON
    else:
        try:
            retry_on = normalize_retry_on(str(retry_on_raw))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    retry_max_attempts_raw = _pick("retry_max_attempts", cli, file_values)
    if retry_max_attempts_raw is None:
        retry_max_attempts = DEFAULT_RETRY_MAX_ATTEMPTS
    else:
        try:
            retry_max_attempts = int(retry_max_attempts_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"retry-max-attempts must be a positive integer (got: {retry_max_attempts_raw!r})"
            ) from exc
        if retry_max_attempts < 1:
            raise ConfigError(
                f"retry-max-attempts must be at least 1 (got: {retry_max_attempts})"
            )

    retry_backoff_raw = _pick("retry_backoff_seconds", cli, file_values)
    retry_backoff_seconds = _parse_retry_backoff_seconds(retry_backoff_raw)

    state_file_raw = _pick("state_file", cli, file_values)
    state_file = _resolve_state_file_setting(state_file_raw, project_root)

    history_file_raw = _pick("history_file", cli, file_values)
    history_file = _resolve_history_file_setting(history_file_raw, project_root)

    fresh = bool(_pick("fresh", cli, file_values))
    resume = bool(_pick("resume", cli, file_values))

    if resume and fresh:
        raise ConfigError("--resume and --fresh are mutually exclusive")
    if resume and history_file is None:
        raise ConfigError("--resume requires run history persistence; do not use --no-history")

    dry_run = bool(_pick("dry_run", cli, file_values))
    git_summary = bool(_pick("git_summary", cli, file_values))
    export_transcript_dir = _resolve_export_transcript_dir(
        _pick("export_transcript_dir", cli, file_values),
        project_root,
    )
    cycle_log = _resolve_cycle_log(
        _pick("cycle_log", cli, file_values),
        project_root,
    )

    routing = _parse_routing_section(file_values, project_root)
    routing = _apply_routing_cli_overrides(routing, cli)

    return CyclopsctlConfig(
        project_root=project_root,
        cycles=cycles,
        first_prompt=first_prompt,
        current_handover=current_handover,
        update_handover=update_handover,
        complexity_report=complexity_report,
        ai_context=ai_context,
        require_ai_context=require_ai_context,
        ai_context_max_chars=ai_context_max_chars,
        tag=tag,
        default_model=default_model,
        strict_handover=strict_handover,
        task_source=task_source,
        pinned_task_id=pinned_task_id,
        plain=plain,
        retry_on=retry_on,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        state_file=state_file,
        history_file=history_file,
        fresh=fresh,
        resume=resume,
        dry_run=dry_run,
        git_summary=git_summary,
        export_transcript_dir=export_transcript_dir,
        cycle_log=cycle_log,
        routing=routing,
        task_backend=task_backend,
    )


def build_launch_config(
    cli: Mapping[str, Any],
    file_cfg: Mapping[str, Any] | None = None,
) -> LaunchConfig:
    """Merge CLI and file settings for the interactive launcher."""
    file_values = file_cfg or {}

    project_root_raw = _pick("project_root", cli, file_values)
    project_root = resolve_project_root(
        Path(project_root_raw) if project_root_raw is not None else None
    )
    if not project_root.is_dir():
        raise ConfigError(f"project-root is not a directory: {project_root}")

    config_path_raw = _pick("config_path", cli, file_values)
    config_path = Path(config_path_raw).resolve() if config_path_raw is not None else None

    first_prompt = _resolve_project_relative_path(
        Path(_pick("first_prompt", cli, file_values))
        if _pick("first_prompt", cli, file_values) is not None
        else None,
        project_root,
        DEFAULT_FIRST_PROMPT,
    )
    current_handover = _resolve_project_relative_path(
        Path(_pick("current_handover", cli, file_values))
        if _pick("current_handover", cli, file_values) is not None
        else None,
        project_root,
        DEFAULT_CURRENT_HANDOVER,
    )
    update_handover = _resolve_project_relative_path(
        Path(_pick("update_handover", cli, file_values))
        if _pick("update_handover", cli, file_values) is not None
        else None,
        project_root,
        DEFAULT_UPDATE_HANDOVER,
    )
    from cyclopsctl.tasks.backend import resolve_task_backend

    task_backend = resolve_task_backend(cli, file_values, project_root=project_root)

    complexity_report_raw = _pick("complexity_report", cli, file_values)
    if complexity_report_raw is not None:
        complexity_report = _resolve_project_relative_path(
            Path(complexity_report_raw),
            project_root,
            default_complexity_report_rel(task_backend),
        )
    else:
        complexity_report = resolve_default_complexity_report_path(
            project_root,
            task_backend=task_backend,
        )
    ai_context = _resolve_ai_context_path(
        Path(_pick("ai_context", cli, file_values))
        if _pick("ai_context", cli, file_values) is not None
        else None,
        project_root,
    )

    tag_raw = _pick("tag", cli, file_values)
    tag = str(tag_raw).strip() if tag_raw is not None and str(tag_raw).strip() else None

    plain_raw = _pick("plain", cli, file_values)
    plain = bool(plain_raw) if plain_raw is not None else False

    state_file_raw = _pick("state_file", cli, file_values)
    state_file = _resolve_state_file_setting(state_file_raw, project_root)

    history_file_raw = _pick("history_file", cli, file_values)
    history_file = _resolve_history_file_setting(history_file_raw, project_root)

    return LaunchConfig(
        project_root=project_root,
        first_prompt=first_prompt,
        current_handover=current_handover,
        update_handover=update_handover,
        complexity_report=complexity_report,
        ai_context=ai_context,
        config_path=config_path,
        task_backend=task_backend,
        tag=tag,
        plain=plain,
        state_file=state_file,
        history_file=history_file,
    )


def build_doctor_config(
    cli: Mapping[str, Any],
    file_cfg: Mapping[str, Any] | None = None,
) -> DoctorConfig:
    """Merge CLI and file settings for doctor diagnostics."""
    file_values = file_cfg or {}

    project_root_raw = _pick("project_root", cli, file_values)
    project_root = resolve_project_root(
        Path(project_root_raw) if project_root_raw is not None else None
    )
    if not project_root.is_dir():
        raise ConfigError(f"project-root is not a directory: {project_root}")

    current_raw = _pick("current_handover", cli, file_values)
    current_handover = _resolve_project_relative_path(
        Path(current_raw) if current_raw is not None else None,
        project_root,
        DEFAULT_CURRENT_HANDOVER,
    )

    from cyclopsctl.tasks.backend import resolve_task_backend

    task_backend = resolve_task_backend(cli, file_values, project_root=project_root)

    report_raw = _pick("complexity_report", cli, file_values)
    if report_raw is not None:
        complexity_report = _resolve_project_relative_path(
            Path(report_raw),
            project_root,
            default_complexity_report_rel(task_backend),
        )
    else:
        complexity_report = resolve_default_complexity_report_path(
            project_root,
            task_backend=task_backend,
        )

    tag_raw = _pick("tag", cli, file_values)
    tag = str(tag_raw).strip() if tag_raw is not None and str(tag_raw).strip() else None

    plain_raw = _pick("plain", cli, file_values)
    plain = bool(plain_raw) if plain_raw is not None else False

    fix_raw = _pick("fix", cli, file_values)
    fix = bool(fix_raw) if fix_raw is not None else False

    state_file_raw = _pick("state_file", cli, file_values)
    state_file = _resolve_state_file_setting(state_file_raw, project_root)

    return DoctorConfig(
        project_root=project_root,
        current_handover=current_handover,
        complexity_report=complexity_report,
        task_backend=task_backend,
        tag=tag,
        plain=plain,
        fix=fix,
        state_file=state_file,
    )


def build_status_config(
    cli: Mapping[str, Any],
    file_cfg: Mapping[str, Any] | None = None,
) -> StatusConfig:
    """Merge CLI and file settings for status inspection."""
    file_values = file_cfg or {}

    project_root_raw = _pick("project_root", cli, file_values)
    project_root = resolve_project_root(
        Path(project_root_raw) if project_root_raw is not None else None
    )
    if not project_root.is_dir():
        raise ConfigError(f"project-root is not a directory: {project_root}")

    state_file_raw = _pick("state_file", cli, file_values)
    state_file = _resolve_state_file_setting(state_file_raw, project_root)

    history_file_raw = _pick("history_file", cli, file_values)
    history_file = _resolve_history_file_setting(history_file_raw, project_root)

    return StatusConfig(
        project_root=project_root,
        state_file=state_file,
        history_file=history_file,
    )


def load_launch_config(
    *,
    config_path: Path | None = None,
    profile: str | None = None,
    project_root: Path | None = None,
    first_prompt: Path | None = None,
    current_handover: Path | None = None,
    update_handover: Path | None = None,
    complexity_report: Path | None = None,
    ai_context: Path | None = None,
    tag: str | None = None,
    plain: bool | None = None,
    state_file: Path | None = None,
    history_file: Path | None = None,
    task_backend: str | None = None,
) -> LaunchConfig:
    """Load TOML (if any), merge with CLI values (CLI wins), validate."""
    file_cfg = load_effective_config(config_path, profile)
    cli_values = {
        "config_path": config_path,
        "project_root": project_root,
        "first_prompt": first_prompt,
        "current_handover": current_handover,
        "update_handover": update_handover,
        "complexity_report": complexity_report,
        "ai_context": ai_context,
        "tag": tag,
        "plain": plain,
        "state_file": state_file,
        "history_file": history_file,
        "task_backend": task_backend,
    }
    return build_launch_config(cli_values, file_cfg)


def load_doctor_config(
    *,
    config_path: Path | None = None,
    profile: str | None = None,
    project_root: Path | None = None,
    current_handover: Path | None = None,
    complexity_report: Path | None = None,
    tag: str | None = None,
    plain: bool | None = None,
    fix: bool | None = None,
    state_file: Path | None = None,
    task_backend: str | None = None,
) -> DoctorConfig:
    """Load TOML (if any), merge with CLI values (CLI wins), validate."""
    file_cfg = load_effective_config(config_path, profile)
    cli_values = {
        "project_root": project_root,
        "current_handover": current_handover,
        "complexity_report": complexity_report,
        "tag": tag,
        "plain": plain,
        "fix": fix,
        "state_file": state_file,
        "task_backend": task_backend,
    }
    return build_doctor_config(cli_values, file_cfg)


def load_status_config(
    *,
    config_path: Path | None = None,
    profile: str | None = None,
    project_root: Path | None = None,
    state_file: Path | None = None,
    history_file: Path | None = None,
) -> StatusConfig:
    """Load TOML (if any), merge with CLI values (CLI wins), validate."""
    file_cfg = load_effective_config(config_path, profile)
    cli_values = {
        "project_root": project_root,
        "state_file": state_file,
        "history_file": history_file,
    }
    return build_status_config(cli_values, file_cfg)


def load_run_config(
    *,
    config_path: Path | None = None,
    profile: str | None = None,
    cycles: int | None = None,
    project_root: Path | None = None,
    first_prompt: Path | None = None,
    current_handover: Path | None = None,
    update_handover: Path | None = None,
    complexity_report: Path | None = None,
    ai_context: Path | None = None,
    require_ai_context: bool | None = None,
    ai_context_max_chars: int | None = None,
    tag: str | None = None,
    default_model: str | None = None,
    strict_handover: bool | None = None,
    task_source: str | None = None,
    pinned_task_id: int | None = None,
    plain: bool | None = None,
    retry_on: str | None = None,
    retry_max_attempts: int | None = None,
    retry_backoff_seconds: str | tuple[int, ...] | list[int] | None = None,
    state_file: Path | str | None = None,
    history_file: Path | str | None = None,
    fresh: bool | None = None,
    resume: bool | None = None,
    dry_run: bool | None = None,
    git_summary: bool | None = None,
    export_transcript_dir: Path | None = None,
    cycle_log: Path | str | None = None,
    composer_tier: str | None = None,
    opus_enabled: bool | None = None,
) -> CyclopsctlConfig:
    """Load TOML (if any), merge with CLI values (CLI wins), validate."""
    file_cfg = load_effective_config(config_path, profile)
    cli_values = {
        "cycles": cycles,
        "project_root": project_root,
        "first_prompt": first_prompt,
        "current_handover": current_handover,
        "update_handover": update_handover,
        "complexity_report": complexity_report,
        "ai_context": ai_context,
        "require_ai_context": require_ai_context,
        "ai_context_max_chars": ai_context_max_chars,
        "tag": tag,
        "default_model": default_model,
        "strict_handover": strict_handover,
        "task_source": task_source,
        "pinned_task_id": pinned_task_id,
        "plain": plain,
        "retry_on": retry_on,
        "retry_max_attempts": retry_max_attempts,
        "retry_backoff_seconds": retry_backoff_seconds,
        "state_file": state_file,
        "history_file": history_file,
        "fresh": fresh,
        "resume": resume,
        "dry_run": dry_run,
        "git_summary": git_summary,
        "export_transcript_dir": export_transcript_dir,
        "cycle_log": cycle_log,
        "composer_tier": composer_tier,
        "opus_enabled": opus_enabled,
    }
    return build_run_config(cli_values, file_cfg)
