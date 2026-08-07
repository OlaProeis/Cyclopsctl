"""Interactive pre-run launcher for cyclopsctl setup (task 14 + task 23)."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from cyclopsctl.bootstrap import BootstrapConfig, sync_current_handover
from cyclopsctl.config import LaunchConfig, load_config_file, load_effective_config
from cyclopsctl.doctor import (
    DiagnosticCheck,
    exit_code_for_checks,
    run_launch_diagnostics,
)
from cyclopsctl.errors import GENERAL_EXIT_CODE
from cyclopsctl.history import handover_ready_for_implementation, resolve_startup
from cyclopsctl.profiles import list_profile_names
from cyclopsctl.project_setup import (
    ATTACH_LAUNCH_INFO_MESSAGE,
    LaunchPrdChangeError,
    LaunchReadinessError,
    format_init_required_message,
    handover_needs_sync,
    handle_launch_prd_change,
    is_brownfield_attach_context,
    is_project_initialized_for_launch,
    load_last_parsed_prd,
)
from cyclopsctl.prompt import PromptError, parse_task_id, read_prompt_text
from cyclopsctl.tasks.backend import TaskBackend, TaskBackendConfig, get_task_backend
from cyclopsctl.tasks.cli import TasksCliError
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.tui import print_launch_status

DEFAULT_SUGGESTED_CYCLES_CAP = 5
NON_TTY_EXIT_CODE = 2
DEFAULT_PRD = Path("prd.md")


class LaunchAction(str, Enum):
    """Top-level launcher entry actions."""

    RUN = "run"
    BOOTSTRAP = "bootstrap"
    DOCTOR = "doctor"
    MODELS = "models"


LAUNCH_ACTION_LABELS: dict[LaunchAction, str] = {
    LaunchAction.RUN: "Run cyclopsctl cycles",
    LaunchAction.BOOTSTRAP: "Bootstrap from PRD",
    LaunchAction.DOCTOR: "Run doctor diagnostics",
    LaunchAction.MODELS: "Inspect available models",
}


class LaunchPrompts(Protocol):
    """Injectable prompts for interactive launcher tests."""

    def ask_int(self, prompt: str, *, default: int, minimum: int = 1) -> int: ...

    def ask_bool(self, prompt: str, *, default: bool) -> bool: ...

    def ask_choice(self, prompt: str, choices: list[str], *, default_index: int) -> int: ...

    def ask_str(self, prompt: str, *, default: str) -> str: ...

    def ask_confirm(self, prompt: str, *, default: bool = False) -> bool: ...


@dataclass(frozen=True)
class LaunchChoices:
    """User-selected run options assembled by the launcher."""

    cycles: int
    plain: bool
    strict_handover: bool
    fresh: bool
    resume: bool
    tag: str | None
    profile: str | None = None
    composer_tier: str | None = None
    grok_tier: str | None = None
    opus_enabled: bool | None = None
    fable_enabled: bool | None = None


@dataclass(frozen=True)
class BootstrapChoices:
    """User-selected bootstrap options assembled by the launcher."""

    from_prd: Path
    tag: str | None
    skip_analyze: bool


@dataclass(frozen=True)
class LaunchDispatch:
    """Resolved launcher action and assembled subcommand argv."""

    action: LaunchAction | None
    argv: list[str] | None
    exit_code: int = 0


@dataclass(frozen=True)
class LaunchStatus:
    """Project snapshot shown before starting a run."""

    config: LaunchConfig
    checks: list[DiagnosticCheck]
    handover_task_id: int | None
    next_task: NextTaskLookup | None
    pending_count: int | None
    suggested_cycles: int
    resume_available: bool
    profile_names: tuple[str, ...] = ()
    default_composer_tier: str = "standard"
    default_grok_tier: str = "standard"
    default_opus_enabled: bool = True
    default_fable_enabled: bool = True
    next_task_error: str | None = None
    active_tag: str | None = None


def suggest_cycles(pending_count: int | None) -> int:
    """Suggest a cycle count from the pending queue size."""
    if pending_count is None or pending_count < 1:
        return 1
    return min(pending_count, DEFAULT_SUGGESTED_CYCLES_CAP)


def format_launch_summary_line(status: LaunchStatus) -> str:
    """Render a concise brownfield preflight summary for launch overview."""
    from cyclopsctl.workflow_gen import workflow_upgrade_recommended

    parts: list[str] = []
    pending_count = getattr(status, "pending_count", None)
    if pending_count is not None:
        parts.append(f"Pending: {pending_count}")
    handover_task_id = getattr(status, "handover_task_id", None)
    if handover_task_id is not None:
        parts.append(f"Handover: task {handover_task_id}")
    else:
        parts.append("Handover: —")
    next_task = getattr(status, "next_task", None)
    if next_task is not None and next_task.found and next_task.task is not None:
        parts.append(f"Next: task {next_task.task.numeric_id}")
    elif next_task is not None and not next_task.found:
        parts.append("Next: —")
    active_tag = getattr(status, "active_tag", None) or getattr(status.config, "tag", None)
    if active_tag:
        parts.append(f"Tag: {active_tag}")
    if workflow_upgrade_recommended(status.checks):
        parts.append("Workflow: upgrade available")
    return " · ".join(parts)


def _parse_handover_task_id(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        raw = read_prompt_text(path)
        return parse_task_id(raw, required=False)
    except PromptError:
        return None


def _resolve_launch_backend(config: LaunchConfig) -> TaskBackend:
    _ = config
    return get_task_backend(TaskBackendConfig())


def _pending_count_from_checks(checks: list[DiagnosticCheck]) -> int | None:
    for check in checks:
        if check.name != "Task queue list" or not check.passed:
            continue
        match = re.search(r"^(\d+) pending", check.detail)
        if match:
            return int(match.group(1))
        if "0 pending" in check.detail:
            return 0
    return None


def _next_task_from_checks(checks: list[DiagnosticCheck]) -> NextTaskLookup | None:
    for check in checks:
        if check.name != "Backend next" or not check.passed:
            continue
        match = re.search(r"Next task #(\d+):\s*(.+)$", check.detail)
        if match:
            return NextTaskLookup.from_task(
                NextTaskResult(
                    task_id=match.group(1),
                    title=match.group(2).strip().strip("'"),
                    status=None,
                    priority=None,
                    complexity=None,
                    tag=None,
                )
            )
        if "queue is empty" in check.detail:
            tag_match = re.search(r"tag='([^']*)'", check.detail)
            tag = tag_match.group(1) if tag_match else None
            return NextTaskLookup.empty(tag=tag)
    return None


def discover_profile_names(config: LaunchConfig) -> list[str]:
    """Return profile names from the launcher config file, if any."""
    if config.config_path is None:
        return []
    raw = load_config_file(config.config_path)
    return list_profile_names(raw)


def infer_launch_defaults(
    status: LaunchStatus,
    *,
    profile: str | None = None,
    composer_tier: str | None = None,
    grok_tier: str | None = None,
    opus_enabled: bool | None = None,
    fable_enabled: bool | None = None,
    strict_handover: bool | None = None,
    fresh: bool | None = None,
    resume: bool | None = None,
    plain: bool | None = None,
    tag: str | None = None,
) -> LaunchChoices:
    """Infer run options from config, history, and optional CLI overrides."""
    config = status.config

    if strict_handover is not None:
        resolved_strict = bool(strict_handover)
    elif config.config_path is not None:
        file_cfg = load_config_file(config.config_path)
        resolved_strict = bool(file_cfg.get("strict_handover", False))
    else:
        resolved_strict = False

    if fresh:
        resolved_fresh, resolved_resume = True, False
    elif resume is not None:
        resolved_fresh, resolved_resume = False, bool(resume)
    elif status.resume_available:
        resolved_fresh, resolved_resume = False, True
    else:
        resolved_fresh, resolved_resume = False, False

    defaults = resolve_routing_defaults(config, profile=profile)

    return LaunchChoices(
        cycles=status.suggested_cycles,
        plain=plain if plain is not None else config.plain,
        strict_handover=resolved_strict,
        fresh=resolved_fresh,
        resume=resolved_resume,
        tag=tag if tag is not None else config.tag,
        profile=profile,
        composer_tier=composer_tier or defaults.composer_tier,
        grok_tier=grok_tier or defaults.grok_tier,
        opus_enabled=opus_enabled if opus_enabled is not None else defaults.opus_enabled,
        fable_enabled=(
            fable_enabled if fable_enabled is not None else defaults.fable_enabled
        ),
    )


def prepare_launch_workspace(
    config: LaunchConfig,
) -> str | None:
    """
    Repair missing workflow scaffold for attach-ready repos before diagnostics.

    Returns a user-facing message when handover or workflow files were synced.
    """
    root = config.project_root.resolve()
    if not is_brownfield_attach_context(root):
        return None
    if not is_project_initialized_for_launch(
        root,
        current_handover=config.current_handover,
        update_handover=config.update_handover,
        ai_context=config.ai_context,
        tag=config.tag,
    ):
        return None

    backend = _resolve_launch_backend(config)
    from cyclopsctl.workflow_gen import WorkflowGenConfig, generate_workflow_files

    if not config.ai_context.is_file() or not config.update_handover.is_file():
        generate_workflow_files(
            WorkflowGenConfig(
                project_root=root,
                prd_path=None,
            )
        )

    try:
        next_lookup = backend.get_next(root, tag=config.tag)
    except (TasksCliError, ValueError, RuntimeError):
        return None

    if not handover_needs_sync(config.current_handover, next_lookup=next_lookup):
        return None

    bootstrap_cfg = BootstrapConfig(
        project_root=root,
        from_prd=None,
        current_handover=config.current_handover,
        complexity_report=config.complexity_report,
        ai_context=config.ai_context,
        tag=config.tag,
    )
    try:
        _, task_id = sync_current_handover(
            bootstrap_cfg,
            backend=backend,
        )
    except Exception:
        return None

    title = ""
    if next_lookup.found and next_lookup.task is not None:
        title = next_lookup.task.title
    message = f"Synced handover for task {task_id}"
    if title:
        message = f"{message} — {title}"
    return message


def maybe_repair_handover_at_launch(
    config: LaunchConfig,
    status: LaunchStatus,
    *,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    bridge_manager: Callable | None = None,
) -> tuple[LaunchConfig, LaunchStatus, str | None]:
    """
    Sync handover when pending tasks exist and handover is missing or stale.

    Returns ``(config, status, sync_message)``; ``sync_message`` is set when
    repair ran successfully.
    """
    if status.pending_count is None or status.pending_count < 1:
        return config, status, None
    if status.next_task is None:
        return config, status, None
    if not handover_needs_sync(config.current_handover, next_lookup=status.next_task):
        return config, status, None

    bootstrap_cfg = BootstrapConfig(
        project_root=config.project_root,
        from_prd=None,
        current_handover=config.current_handover,
        complexity_report=config.complexity_report,
        ai_context=config.ai_context,
        tag=config.tag,
    )
    launch_backend = _resolve_launch_backend(config)
    try:
        _, task_id = sync_current_handover(
            bootstrap_cfg,
            backend=launch_backend,
        )
    except Exception as exc:
        print(
            f"cyclopsctl: warning: could not sync handover: {exc}",
            file=sys.stderr,
        )
        return config, status, None

    title = ""
    if status.next_task.found and status.next_task.task is not None:
        title = status.next_task.task.title

    message = f"Synced handover for task {task_id}"
    if title:
        message = f"{message} — {title}"

    refreshed = gather_launch_status(
        config,
        env=env,
        which=which,
        bridge_manager=bridge_manager,
    )
    return config, refreshed, message


@dataclass(frozen=True)
class RoutingLaunchDefaults:
    """Default routing choices shown / applied at launch."""

    composer_tier: str = "standard"
    grok_tier: str = "standard"
    opus_enabled: bool = True
    fable_enabled: bool = True


def resolve_routing_defaults(
    config: LaunchConfig,
    *,
    profile: str | None = None,
) -> RoutingLaunchDefaults:
    """Return default composer/grok tiers and Fable/Opus enablement for prompts."""
    if config.config_path is None:
        return RoutingLaunchDefaults()
    from cyclopsctl.config import _parse_routing_section

    file_cfg = load_effective_config(config.config_path, profile)
    routing = _parse_routing_section(file_cfg, config.project_root)
    if routing is None:
        return RoutingLaunchDefaults()
    return RoutingLaunchDefaults(
        composer_tier=routing.composer_tier,
        grok_tier=routing.grok_tier,
        opus_enabled=routing.opus_enabled,
        fable_enabled=routing.fable_enabled,
    )


def gather_launch_status(
    config: LaunchConfig,
    *,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    bridge_manager: Callable | None = None,
) -> LaunchStatus:
    """Load diagnostics and derive launcher menu fields."""
    checks = run_launch_diagnostics(
        config,
        env=env,
        which=which,
        bridge_manager=bridge_manager,
    )
    handover_task_id = _parse_handover_task_id(config.current_handover)
    pending_count = _pending_count_from_checks(checks)
    launch_backend = _resolve_launch_backend(config)
    if pending_count is None:
        try:
            pending_count = len(
                launch_backend.list_pending(config.project_root, tag=config.tag)
            )
        except (TasksCliError, ValueError, RuntimeError):
            pending_count = None

    next_task = _next_task_from_checks(checks)
    next_task_error = None
    if next_task is None:
        for check in checks:
            if check.name == "Backend next" and not check.passed:
                next_task_error = check.detail
                break
        if next_task is None and next_task_error is None:
            try:
                next_task = launch_backend.get_next(config.project_root, tag=config.tag)
            except (TasksCliError, ValueError, RuntimeError) as exc:
                next_task_error = str(exc)

    startup = resolve_startup(
        project_root=config.project_root,
        handover_path=config.current_handover,
        history_path=config.history_file,
        fresh=False,
    )
    routing_defaults = resolve_routing_defaults(config)
    active_tag = config.tag
    if active_tag is None:
        try:
            active_tag = launch_backend.current_tag(config.project_root)
        except (TasksCliError, ValueError, RuntimeError):
            active_tag = None
    if active_tag is None and next_task is not None:
        active_tag = next_task.tag

    return LaunchStatus(
        config=config,
        checks=checks,
        handover_task_id=handover_task_id,
        next_task=next_task,
        pending_count=pending_count,
        suggested_cycles=suggest_cycles(pending_count),
        resume_available=startup.resumed,
        profile_names=tuple(discover_profile_names(config)),
        default_composer_tier=routing_defaults.composer_tier,
        default_grok_tier=routing_defaults.grok_tier,
        default_opus_enabled=routing_defaults.opus_enabled,
        default_fable_enabled=routing_defaults.fable_enabled,
        next_task_error=next_task_error,
        active_tag=active_tag,
    )


def _shared_launch_argv(config: LaunchConfig) -> list[str]:
    argv: list[str] = ["--project-root", str(config.project_root)]
    if config.config_path is not None:
        argv.extend(["--config", str(config.config_path)])
    if config.tag:
        argv.extend(["--tag", config.tag])
    return argv


def build_run_argv(config: LaunchConfig, choices: LaunchChoices) -> list[str]:
    """Assemble argv for ``cyclopsctl run`` from launcher selections."""
    argv = ["run", "--cycles", str(choices.cycles), "--project-root", str(config.project_root)]
    if config.config_path is not None:
        argv.extend(["--config", str(config.config_path)])
    else:
        argv.extend(
            [
                "--current-handover",
                str(config.current_handover),
                "--update-handover",
                str(config.update_handover),
                "--complexity-report",
                str(config.complexity_report),
            ]
        )
        if not handover_ready_for_implementation(config.current_handover):
            argv.extend(["--first-prompt", str(config.first_prompt)])
    if choices.profile:
        argv.extend(["--profile", choices.profile])
    if choices.composer_tier:
        argv.extend(["--composer-tier", choices.composer_tier])
    if choices.grok_tier:
        argv.extend(["--grok-tier", choices.grok_tier])
    if choices.opus_enabled is False:
        argv.append("--no-opus")
    if choices.fable_enabled is False:
        argv.append("--no-fable")
    if choices.plain:
        argv.append("--plain")
    if choices.strict_handover:
        argv.append("--strict-handover")
    if choices.fresh:
        argv.append("--fresh")
    if choices.resume:
        argv.append("--resume")
    if choices.tag:
        argv.extend(["--tag", choices.tag])
    return argv


def build_bootstrap_argv(config: LaunchConfig, choices: BootstrapChoices) -> list[str]:
    """Assemble argv for ``cyclopsctl bootstrap`` from launcher selections."""
    argv = ["bootstrap", "--project-root", str(config.project_root)]
    if config.config_path is not None:
        argv.extend(["--config", str(config.config_path)])
    argv.extend(["--from-prd", str(choices.from_prd)])
    if choices.tag:
        argv.extend(["--tag", choices.tag])
    if choices.skip_analyze:
        argv.append("--skip-analyze")
    return argv


def build_doctor_argv(config: LaunchConfig, *, fix: bool = False) -> list[str]:
    """Assemble argv for ``cyclopsctl doctor`` from launcher context."""
    argv = ["doctor", *_shared_launch_argv(config)]
    if config.plain:
        argv.append("--plain")
    if fix:
        argv.append("--fix")
    return argv


def build_models_argv() -> list[str]:
    """Assemble argv for ``cyclopsctl models``."""
    return ["models"]


def format_non_tty_message() -> str:
    """Instructions printed when interactive prompts are unavailable."""
    return (
        "Interactive launcher requires a TTY or explicit --action with flags.\n"
        "Actions:\n"
        "  run       — cyclopsctl launch --action run --cycles N "
        "[--profile NAME] [--composer-tier standard|fast] "
        "[--grok-tier standard|fast] [--no-fable] [--no-opus] "
        "[--plain] [--strict-handover] [--fresh|--resume] [--tag TAG] --yes\n"
        "  bootstrap — cyclopsctl launch --action bootstrap "
        "[--from-prd PATH] [--tag TAG] [--skip-analyze] --yes\n"
        "  doctor    — cyclopsctl launch --action doctor [--fix]\n"
        "  models    — cyclopsctl launch --action models\n"
        "Or invoke subcommands directly: cyclopsctl run, bootstrap, doctor, models."
    )


def can_spawn_run(checks: list[DiagnosticCheck]) -> bool:
    """Return True when failed checks should not block launching a run."""
    return all(check.passed for check in checks)


def _normalize_action(action: str | LaunchAction | None) -> LaunchAction | None:
    if action is None:
        return None
    if isinstance(action, LaunchAction):
        return action
    normalized = str(action).strip().lower()
    try:
        return LaunchAction(normalized)
    except ValueError:
        raise ValueError(
            f"action must be one of: {', '.join(item.value for item in LaunchAction)} "
            f"(got: {action!r})"
        ) from None


def _action_from_index(index: int) -> LaunchAction:
    actions = list(LaunchAction)
    return actions[index]


class _DefaultLaunchPrompts:
    """stdin/stdout prompts for interactive launcher mode."""

    def ask_int(self, prompt: str, *, default: int, minimum: int = 1) -> int:
        while True:
            raw = input(f"{prompt} [{default}]: ").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                print(f"Enter an integer >= {minimum}.", file=sys.stderr)
                continue
            if value < minimum:
                print(f"Enter an integer >= {minimum}.", file=sys.stderr)
                continue
            return value

    def ask_bool(self, prompt: str, *, default: bool) -> bool:
        default_label = "Y/n" if default else "y/N"
        while True:
            raw = input(f"{prompt} [{default_label}]: ").strip().lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            print("Answer y or n.", file=sys.stderr)

    def ask_choice(self, prompt: str, choices: list[str], *, default_index: int) -> int:
        for index, label in enumerate(choices, start=1):
            print(f"  {index}. {label}", file=sys.stderr)
        while True:
            raw = input(f"{prompt} [{default_index + 1}]: ").strip()
            if not raw:
                return default_index
            try:
                picked = int(raw) - 1
            except ValueError:
                print("Enter a listed option number.", file=sys.stderr)
                continue
            if 0 <= picked < len(choices):
                return picked
            print("Enter a listed option number.", file=sys.stderr)

    def ask_str(self, prompt: str, *, default: str) -> str:
        raw = input(f"{prompt} [{default}]: ").strip()
        return raw if raw else default

    def ask_confirm(self, prompt: str, *, default: bool = False) -> bool:
        return self.ask_bool(prompt, default=default)


def prompt_launch_action(
    prompts: LaunchPrompts | None = None,
    *,
    default_index: int = 0,
) -> LaunchAction:
    """Collect the top-level launcher action interactively."""
    ask = prompts or _DefaultLaunchPrompts()
    labels = [LAUNCH_ACTION_LABELS[action] for action in LaunchAction]
    picked = ask.ask_choice("Launcher action", labels, default_index=default_index)
    return _action_from_index(picked)


LAUNCH_OPUS_PROMPT = "Use Opus on high-complexity tasks (when routing rules select Opus)?"
LAUNCH_FABLE_PROMPT = "Use Fable on high-complexity tasks (complexity 9-10)?"
LAUNCH_COMPOSER_TIER_CHOICES = (
    "Composer standard (recommended)",
    "Composer fast",
)
LAUNCH_GROK_TIER_CHOICES = (
    "Grok standard / not-fast (recommended for orchestration)",
    "Grok fast",
)


def _tier_default_index(tier: str | None) -> int:
    return 1 if (tier or "standard").lower() == "fast" else 0


def _tier_from_choice_index(index: int) -> str:
    return "fast" if index == 1 else "standard"


def _format_cycles_confirm_message(cycles: int, handover_task_id: int | None) -> str:
    cycle_label = "cycle" if cycles == 1 else "cycles"
    message = f"Start {cycles} {cycle_label}"
    if handover_task_id is not None:
        message = f"{message} on task {handover_task_id}"
    return f"{message}?"


def prompt_launch_choices(
    status: LaunchStatus,
    prompts: LaunchPrompts | None = None,
    *,
    inferred: LaunchChoices | None = None,
    profile: str | None = None,
    composer_tier: str | None = None,
    grok_tier: str | None = None,
    opus_enabled: bool | None = None,
    fable_enabled: bool | None = None,
    strict_handover: bool | None = None,
    fresh: bool | None = None,
    resume: bool | None = None,
    plain: bool | None = None,
    tag: str | None = None,
) -> LaunchChoices | None:
    """Collect cycle count and model tiers interactively; None when cancelled."""
    ask = prompts or _DefaultLaunchPrompts()
    defaults = inferred or infer_launch_defaults(
        status,
        profile=profile,
        composer_tier=composer_tier,
        grok_tier=grok_tier,
        opus_enabled=opus_enabled,
        fable_enabled=fable_enabled,
        strict_handover=strict_handover,
        fresh=fresh,
        resume=resume,
        plain=plain,
        tag=tag,
    )

    cycles = ask.ask_int(
        "Number of cycles",
        default=status.suggested_cycles,
        minimum=1,
    )

    resolved_composer_tier = composer_tier
    if resolved_composer_tier is None:
        default_composer = getattr(
            status,
            "default_composer_tier",
            defaults.composer_tier,
        )
        picked = ask.ask_choice(
            "Composer tier for complexity 1-5",
            list(LAUNCH_COMPOSER_TIER_CHOICES),
            default_index=_tier_default_index(default_composer),
        )
        resolved_composer_tier = _tier_from_choice_index(picked)

    resolved_grok_tier = grok_tier
    if resolved_grok_tier is None:
        default_grok = getattr(status, "default_grok_tier", defaults.grok_tier)
        picked = ask.ask_choice(
            "Grok tier for complexity 6-8",
            list(LAUNCH_GROK_TIER_CHOICES),
            default_index=_tier_default_index(default_grok),
        )
        resolved_grok_tier = _tier_from_choice_index(picked)

    resolved_fable = fable_enabled
    if resolved_fable is None:
        resolved_fable = ask.ask_bool(
            LAUNCH_FABLE_PROMPT,
            default=getattr(status, "default_fable_enabled", defaults.fable_enabled),
        )

    resolved_opus = opus_enabled
    if resolved_opus is None:
        resolved_opus = getattr(status, "default_opus_enabled", defaults.opus_enabled)

    if not ask.ask_confirm(
        _format_cycles_confirm_message(
            cycles,
            getattr(status, "handover_task_id", None),
        ),
        default=True,
    ):
        return None

    return LaunchChoices(
        cycles=cycles,
        plain=defaults.plain,
        strict_handover=defaults.strict_handover,
        fresh=defaults.fresh,
        resume=defaults.resume,
        tag=defaults.tag,
        profile=defaults.profile,
        composer_tier=resolved_composer_tier,
        grok_tier=resolved_grok_tier,
        opus_enabled=resolved_opus,
        fable_enabled=resolved_fable,
    )


def prompt_bootstrap_choices(
    status: LaunchStatus,
    prompts: LaunchPrompts | None = None,
    *,
    from_prd: Path | None = None,
    tag: str | None = None,
    skip_analyze: bool | None = None,
) -> BootstrapChoices | None:
    """Collect bootstrap options interactively; return None when cancelled."""
    ask = prompts or _DefaultLaunchPrompts()
    default_prd = from_prd or (status.config.project_root / DEFAULT_PRD)
    prd_raw = ask.ask_str("PRD file path", default=str(default_prd))
    prd_path = Path(prd_raw).expanduser()
    if not prd_path.is_absolute():
        prd_path = (status.config.project_root / prd_path).resolve()

    default_tag = tag if tag is not None else (status.config.tag or "")
    tag_raw = ask.ask_str("Task tag (leave empty to omit)", default=default_tag)
    resolved_tag = tag_raw.strip() or None

    if skip_analyze is None:
        analyze = ask.ask_bool("Run analyze-complexity after parse-prd", default=True)
        resolved_skip = not analyze
    else:
        resolved_skip = skip_analyze

    summary = (
        f"from_prd={prd_path}, tag={resolved_tag!r}, "
        f"skip_analyze={resolved_skip}"
    )
    if not ask.ask_confirm(f"Start bootstrap with {summary}?", default=False):
        return None

    return BootstrapChoices(
        from_prd=prd_path,
        tag=resolved_tag,
        skip_analyze=resolved_skip,
    )


def resolve_launch_choices(
    status: LaunchStatus,
    *,
    cycles: int | None,
    plain: bool | None,
    strict_handover: bool | None,
    fresh: bool | None,
    resume: bool | None,
    tag: str | None,
    profile: str | None,
    composer_tier: str | None,
    grok_tier: str | None,
    opus_enabled: bool | None,
    fable_enabled: bool | None,
    assume_yes: bool,
    stdin_is_tty: bool,
) -> LaunchChoices | None:
    """Use CLI flags when non-interactive; prompt when on a TTY."""
    interactive = stdin_is_tty and not status.config.plain
    if interactive and cycles is None:
        return prompt_launch_choices(
            status,
            profile=profile,
            composer_tier=composer_tier,
            grok_tier=grok_tier,
            opus_enabled=opus_enabled,
            fable_enabled=fable_enabled,
            strict_handover=strict_handover,
            fresh=fresh,
            resume=resume,
            plain=plain,
            tag=tag,
        )

    if cycles is None:
        return None

    if cycles < 1:
        raise ValueError("cycles must be at least 1")

    if fresh and resume:
        raise ValueError("--fresh and --resume are mutually exclusive")

    defaults = infer_launch_defaults(
        status,
        profile=profile,
        composer_tier=composer_tier,
        grok_tier=grok_tier,
        opus_enabled=opus_enabled,
        fable_enabled=fable_enabled,
        strict_handover=strict_handover,
        fresh=fresh,
        resume=resume,
        plain=plain,
        tag=tag,
    )
    resolved = LaunchChoices(
        cycles=cycles,
        plain=defaults.plain,
        strict_handover=defaults.strict_handover,
        fresh=defaults.fresh,
        resume=defaults.resume,
        tag=defaults.tag,
        profile=defaults.profile,
        composer_tier=defaults.composer_tier,
        grok_tier=defaults.grok_tier,
        opus_enabled=defaults.opus_enabled,
        fable_enabled=defaults.fable_enabled,
    )
    if interactive and not assume_yes:
        confirmed = _DefaultLaunchPrompts().ask_confirm(
            _format_cycles_confirm_message(
                resolved.cycles,
                getattr(status, "handover_task_id", None),
            ),
            default=True,
        )
        if not confirmed:
            return None
    return resolved


def resolve_bootstrap_choices(
    status: LaunchStatus,
    *,
    from_prd: Path | None,
    tag: str | None,
    skip_analyze: bool | None,
    assume_yes: bool,
    stdin_is_tty: bool,
) -> BootstrapChoices | None:
    """Use CLI flags when non-interactive; prompt when on a TTY."""
    interactive = stdin_is_tty and not status.config.plain
    if interactive and from_prd is None:
        return prompt_bootstrap_choices(
            status,
            from_prd=from_prd,
            tag=tag,
            skip_analyze=skip_analyze,
        )

    prd_path = from_prd or (status.config.project_root / DEFAULT_PRD).resolve()
    resolved = BootstrapChoices(
        from_prd=prd_path,
        tag=tag if tag is not None else status.config.tag,
        skip_analyze=bool(skip_analyze),
    )
    if interactive and not assume_yes:
        confirmed = _DefaultLaunchPrompts().ask_confirm(
            f"Start bootstrap with from_prd={resolved.from_prd}, "
            f"tag={resolved.tag!r}, skip_analyze={resolved.skip_analyze}?",
            default=False,
        )
        if not confirmed:
            return None
    return resolved


def _needs_explicit_flags(
    *,
    action: LaunchAction | None,
    cycles: int | None,
) -> bool:
    """Return True when non-interactive mode lacks required flags."""
    if action is None and cycles is None:
        return True
    resolved = action or LaunchAction.RUN
    return resolved is LaunchAction.RUN and cycles is None


def _prompt_new_tag_name(default: str, prompts: LaunchPrompts) -> str | None:
    """Collect a tag name for a changed PRD; return None when cancelled."""
    raw = prompts.ask_str("New task tag for updated PRD", default=default)
    resolved = raw.strip()
    return resolved if resolved else default


def format_phase_complete_hint(tag: str | None) -> str:
    """Return a nudge to start a new phase when the current tag's queue is done."""
    tag_label = f" '{tag}'" if tag else ""
    return (
        f"cyclopsctl: tag{tag_label} has no pending tasks — this phase looks "
        "complete. Start the next phase from a new PRD with "
        "`cyclopsctl launch --prd <new-prd.md>` (keeps this tag's history), or "
        "browse phases with `cyclopsctl tasks tags`."
    )


def _apply_prd_change_at_launch(
    config: LaunchConfig,
    *,
    tag: str | None,
    no_new_tag: bool,
    assume_yes: bool,
    interactive: bool,
    prompts: LaunchPrompts | None,
    plain: bool,
    stderr_is_tty: bool | None,
    prd_path: Path | None = None,
    bootstrap_model: str | None = None,
    parse_model: str | None = None,
    analyze_model: str | None = None,
    stdin_is_tty: bool | None = None,
) -> tuple[LaunchConfig, LaunchStatus | None, LaunchDispatch | None]:
    """
    Run PRD-change detection for launch RUN actions.

    Returns ``(config, status, None)`` on success. When ``status`` is not None the
    caller should use it instead of re-gathering. Returns early ``LaunchDispatch``
    when launch must stop. ``prd_path`` overrides the default ``prd.md`` so an
    explicit ``--prd new-file.md`` starts a fresh phase tag.
    """
    if not is_project_initialized_for_launch(
        config.project_root,
        current_handover=config.current_handover,
        update_handover=config.update_handover,
        ai_context=config.ai_context,
        tag=config.tag,
    ):
        message = format_init_required_message(config.project_root)
        print(f"cyclopsctl: {message}", file=sys.stderr)
        return config, None, LaunchDispatch(
            action=LaunchAction.RUN,
            argv=None,
            exit_code=1,
        )

    if prd_path is not None:
        resolved_prd = prd_path
        if not resolved_prd.is_absolute():
            resolved_prd = (config.project_root / resolved_prd).resolve()
        if not resolved_prd.is_file():
            print(f"cyclopsctl: error: PRD file not found: {resolved_prd}", file=sys.stderr)
            return config, None, LaunchDispatch(
                action=LaunchAction.RUN,
                argv=None,
                exit_code=GENERAL_EXIT_CODE,
            )
    else:
        # Bare launch tracks the PRD that produced the current tag (falling back
        # to prd.md), so a renamed phase file does not look like an attach repo.
        last = load_last_parsed_prd(config.project_root)
        current_prd = (
            (config.project_root / last.path) if last is not None else (config.project_root / DEFAULT_PRD)
        )
        if not current_prd.is_file():
            print(f"cyclopsctl: {ATTACH_LAUNCH_INFO_MESSAGE}", file=sys.stderr)
            return config, None, None

    tag_prompt = None
    if interactive and not assume_yes:
        ask = prompts or _DefaultLaunchPrompts()
        tag_prompt = lambda default: _prompt_new_tag_name(default, ask)

    try:
        prd_result = handle_launch_prd_change(
            config.project_root,
            prd_path=prd_path,
            current_handover=config.current_handover,
            complexity_report=config.complexity_report,
            ai_context=config.ai_context,
            explicit_tag=tag,
            no_new_tag=no_new_tag,
            assume_yes=assume_yes,
            bootstrap_model=bootstrap_model,
            parse_model=parse_model,
            analyze_model=analyze_model,
            stdin_is_tty=stdin_is_tty,
            tag_prompt=tag_prompt,
            backend=_resolve_launch_backend(config),
        )
    except LaunchReadinessError as exc:
        print(f"cyclopsctl: {exc}", file=sys.stderr)
        return config, None, LaunchDispatch(
            action=LaunchAction.RUN,
            argv=None,
            exit_code=1,
        )
    except LaunchPrdChangeError as exc:
        print(f"cyclopsctl: error: {exc}", file=sys.stderr)
        return config, None, LaunchDispatch(
            action=LaunchAction.RUN,
            argv=None,
            exit_code=GENERAL_EXIT_CODE,
        )

    if prd_result is None:
        return config, None, None

    if prd_result.new_tag_created:
        print(
            f"PRD changed — created tag '{prd_result.active_tag}' "
            "and synced handover for the new task list.",
            file=sys.stderr,
        )
    else:
        print(
            f"PRD changed — parsed into tag '{prd_result.active_tag}' "
            "and synced handover.",
            file=sys.stderr,
        )

    updated_config = LaunchConfig(
        project_root=config.project_root,
        first_prompt=config.first_prompt,
        current_handover=config.current_handover,
        update_handover=config.update_handover,
        complexity_report=config.complexity_report,
        ai_context=config.ai_context,
        config_path=config.config_path,
        tag=prd_result.active_tag,
        plain=config.plain,
        state_file=config.state_file,
        history_file=config.history_file,
    )
    refreshed_status = gather_launch_status(updated_config)
    print_launch_status(refreshed_status, plain=plain, stderr_is_tty=stderr_is_tty)
    return updated_config, refreshed_status, None


def run_launch(
    config: LaunchConfig,
    *,
    action: str | LaunchAction | None = None,
    cycles: int | None = None,
    plain: bool | None = None,
    strict_handover: bool | None = None,
    fresh: bool | None = None,
    resume: bool | None = None,
    tag: str | None = None,
    profile: str | None = None,
    composer_tier: str | None = None,
    grok_tier: str | None = None,
    opus_enabled: bool | None = None,
    fable_enabled: bool | None = None,
    from_prd: Path | None = None,
    prd: Path | None = None,
    skip_analyze: bool | None = None,
    doctor_fix: bool | None = None,
    no_new_tag: bool = False,
    assume_yes: bool = False,
    bootstrap_model: str | None = None,
    parse_model: str | None = None,
    analyze_model: str | None = None,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    bridge_manager: Callable | None = None,
    stdin_is_tty: bool | None = None,
    stderr_is_tty: bool | None = None,
    prompts: LaunchPrompts | None = None,
) -> LaunchDispatch:
    """
    Gather status, resolve user choices, and return assembled subcommand argv.

    Returns ``LaunchDispatch`` with ``argv=None`` when the user cancels or when
    non-interactive mode lacks required flags.
    """
    is_tty = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    use_plain = plain if plain is not None else config.plain
    display_config = LaunchConfig(
        project_root=config.project_root,
        first_prompt=config.first_prompt,
        current_handover=config.current_handover,
        update_handover=config.update_handover,
        complexity_report=config.complexity_report,
        ai_context=config.ai_context,
        config_path=config.config_path,
        task_backend=config.task_backend,
        tag=tag if tag is not None else config.tag,
        plain=use_plain,
        state_file=config.state_file,
        history_file=config.history_file,
    )
    workspace_message = prepare_launch_workspace(display_config)
    if workspace_message:
        print(workspace_message, file=sys.stderr)
    status = gather_launch_status(
        display_config,
        env=env,
        which=which,
        bridge_manager=bridge_manager,
    )
    print_launch_status(status, plain=use_plain, stderr_is_tty=stderr_is_tty)

    interactive = is_tty and not use_plain
    inferred_action = _normalize_action(action)
    if inferred_action is None and cycles is not None:
        inferred_action = LaunchAction.RUN

    if not interactive and _needs_explicit_flags(action=inferred_action, cycles=cycles):
        print(format_non_tty_message(), file=sys.stderr)
        return LaunchDispatch(action=None, argv=None, exit_code=NON_TTY_EXIT_CODE)

    try:
        resolved_action = inferred_action or LaunchAction.RUN
    except ValueError as exc:
        print(f"cyclopsctl: error: {exc}", file=sys.stderr)
        return LaunchDispatch(action=None, argv=None, exit_code=GENERAL_EXIT_CODE)

    if resolved_action is LaunchAction.DOCTOR:
        return LaunchDispatch(
            action=resolved_action,
            argv=build_doctor_argv(display_config, fix=bool(doctor_fix)),
            exit_code=0,
        )

    if resolved_action is LaunchAction.MODELS:
        return LaunchDispatch(action=resolved_action, argv=build_models_argv(), exit_code=0)

    if resolved_action is LaunchAction.BOOTSTRAP:
        try:
            bootstrap_choices = resolve_bootstrap_choices(
                status,
                from_prd=from_prd,
                tag=tag,
                skip_analyze=skip_analyze,
                assume_yes=assume_yes,
                stdin_is_tty=is_tty,
            )
        except ValueError as exc:
            print(f"cyclopsctl: error: {exc}", file=sys.stderr)
            return LaunchDispatch(action=resolved_action, argv=None, exit_code=GENERAL_EXIT_CODE)

        if bootstrap_choices is None:
            print("Launch cancelled.", file=sys.stderr)
            return LaunchDispatch(action=resolved_action, argv=None, exit_code=0)

        return LaunchDispatch(
            action=resolved_action,
            argv=build_bootstrap_argv(display_config, bootstrap_choices),
            exit_code=0,
        )

    display_config, prd_status, prd_dispatch = _apply_prd_change_at_launch(
        display_config,
        tag=tag if tag is not None else display_config.tag,
        no_new_tag=no_new_tag,
        assume_yes=assume_yes,
        interactive=interactive,
        prompts=prompts,
        plain=use_plain,
        stderr_is_tty=stderr_is_tty,
        prd_path=prd,
        bootstrap_model=bootstrap_model,
        parse_model=parse_model,
        analyze_model=analyze_model,
        stdin_is_tty=is_tty,
    )
    if prd_dispatch is not None:
        return prd_dispatch
    if prd_status is not None:
        status = prd_status

    display_config, status, sync_message = maybe_repair_handover_at_launch(
        display_config,
        status,
        env=env,
        which=which,
        bridge_manager=bridge_manager,
    )
    if sync_message:
        print(sync_message, file=sys.stderr)

    effective_tag = tag if tag is not None else display_config.tag

    if getattr(status, "pending_count", None) == 0:
        hint_tag = effective_tag or getattr(status, "active_tag", None)
        print(format_phase_complete_hint(hint_tag), file=sys.stderr)

    try:
        if interactive and cycles is None:
            choices = prompt_launch_choices(
                status,
                prompts=prompts,
                profile=profile,
                composer_tier=composer_tier,
                grok_tier=grok_tier,
                opus_enabled=opus_enabled,
                fable_enabled=fable_enabled,
                strict_handover=strict_handover,
                fresh=fresh,
                resume=resume,
                plain=plain,
                tag=effective_tag,
            )
            if choices is not None and effective_tag and choices.tag != effective_tag:
                choices = LaunchChoices(
                    cycles=choices.cycles,
                    plain=choices.plain,
                    strict_handover=choices.strict_handover,
                    fresh=choices.fresh,
                    resume=choices.resume,
                    tag=effective_tag,
                    profile=choices.profile,
                    composer_tier=choices.composer_tier,
                    grok_tier=choices.grok_tier,
                    opus_enabled=choices.opus_enabled,
                    fable_enabled=choices.fable_enabled,
                )
        else:
            choices = resolve_launch_choices(
                status,
                cycles=cycles,
                plain=plain,
                strict_handover=strict_handover,
                fresh=fresh,
                resume=resume,
                tag=effective_tag,
                profile=profile,
                composer_tier=composer_tier,
                grok_tier=grok_tier,
                opus_enabled=opus_enabled,
                fable_enabled=fable_enabled,
                assume_yes=assume_yes,
                stdin_is_tty=is_tty,
            )
    except ValueError as exc:
        print(f"cyclopsctl: error: {exc}", file=sys.stderr)
        return LaunchDispatch(action=LaunchAction.RUN, argv=None, exit_code=GENERAL_EXIT_CODE)

    if choices is None:
        print("Launch cancelled.", file=sys.stderr)
        return LaunchDispatch(action=LaunchAction.RUN, argv=None, exit_code=0)

    if not can_spawn_run(status.checks):
        print(
            "Cannot start run until all preflight checks pass. "
            "Fix the failures above or run ``cyclopsctl doctor``.",
            file=sys.stderr,
        )
        return LaunchDispatch(
            action=LaunchAction.RUN,
            argv=None,
            exit_code=exit_code_for_checks(status.checks),
        )

    return LaunchDispatch(
        action=LaunchAction.RUN,
        argv=build_run_argv(display_config, choices),
        exit_code=0,
    )
