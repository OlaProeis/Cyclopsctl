"""CLI entrypoint: ``cyclopsctl run``, ``doctor``, and ``models``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from cursor_sdk import SDKModel

from cyclopsctl.version import get_package_version
from cyclopsctl.config import (
    ConfigError,
    load_doctor_config,
    load_launch_config,
    load_run_config,
    load_status_config,
)
from cyclopsctl.doctor import run_doctor
from cyclopsctl.launcher import run_launch
from cyclopsctl.env import EnvLoadError, load_project_env
from cyclopsctl.errors import INTERRUPT_EXIT_CODE, KNOWN_RUN_ERRORS, exit_code_for
from cyclopsctl.interrupt import RunInterruptController, RunInterruptedError
from cyclopsctl.logging import log_error
from cyclopsctl.loop import RunLoopResult, run_cycles
from cyclopsctl.models import (
    ModelListingError,
    fetch_model_inventory,
    format_models_diagnostic,
)
from cyclopsctl.preflight import PreflightError, run_preflight
from cyclopsctl.runner import AgentRunError, STARTUP_EXIT_CODE
from cyclopsctl.sdk_bridge import SdkBridgeError, managed_sdk_bridge
from cyclopsctl.history import format_history_summary, read_history
from cyclopsctl.bootstrap import BootstrapError, run_bootstrap, resolve_bootstrap_config
from cyclopsctl.state import RunStateTracker, format_state_summary, read_state
from cyclopsctl.tui import managed_cycle_display, print_run_summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyclopsctl",
        description="Orchestrate Cursor agent runs with handover verification.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_package_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--no-env",
        action="store_true",
        help="Skip loading .env from the project root",
    )

    run_parser = subparsers.add_parser(
        "run",
        parents=[shared],
        help="Run a configured number of implement -> update cycles",
    )
    run_parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Path to cyclopsctl.toml (CLI flags override file values)",
    )
    run_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Named profile table from cyclopsctl.toml (profile.<NAME>)",
    )
    run_parser.add_argument(
        "--composer-tier",
        choices=["standard", "fast"],
        metavar="TIER",
        help="Composer tier override for this run (standard or fast)",
    )
    run_parser.add_argument(
        "--no-opus",
        action="store_true",
        help="Disable Opus routing for high-complexity scores",
    )
    run_parser.add_argument(
        "--cycles",
        type=int,
        help="Number of verified task cycles to run",
    )
    run_parser.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        help="Project repository root (default: current working directory)",
    )
    run_parser.add_argument(
        "--first-prompt",
        type=Path,
        metavar="PATH",
        help="First-run implementation prompt file",
    )
    run_parser.add_argument(
        "--current-handover",
        type=Path,
        metavar="PATH",
        help="Path to current-handover-prompt.md",
    )
    run_parser.add_argument(
        "--update-handover",
        type=Path,
        metavar="PATH",
        help="Path to update-handover-prompt.md",
    )
    run_parser.add_argument(
        "--complexity-report",
        type=Path,
        metavar="PATH",
        help="Complexity report JSON (default: .cyclopsctl/reports/complexity-report.json)",
    )
    run_parser.add_argument(
        "--ai-context",
        type=Path,
        metavar="PATH",
        help="AI context file to inject into every agent prompt (default: ai-context.md)",
    )
    run_parser.add_argument(
        "--require-ai-context",
        action="store_true",
        help="Fail if the ai-context file is missing",
    )
    run_parser.add_argument(
        "--ai-context-max-chars",
        type=int,
        metavar="N",
        help="Truncate ai-context attachment above this size with a warning",
    )
    run_parser.add_argument(
        "--tag",
        help="Task queue tag context (optional)",
    )
    run_parser.add_argument(
        "--default-model",
        metavar="MODEL",
        help="Fallback model when routing or Opus is unavailable (default: composer-2.5)",
    )
    run_parser.add_argument(
        "--strict-handover",
        action="store_true",
        help="Fail when handover Task ID does not match backend next (default: warn)",
    )
    run_parser.add_argument(
        "--task-source",
        choices=["handover", "sequential"],
        metavar="MODE",
        help=(
            "How to choose the task for each cycle: handover (default) or "
            "sequential (lowest pending id)"
        ),
    )
    run_parser.add_argument(
        "--task-id",
        type=int,
        metavar="N",
        help="Pin the next cycle to a specific pending task (validated via tasks show)",
    )
    run_parser.add_argument(
        "--plain",
        action="store_true",
        help="Disable Rich live dashboard; emit plain text logs (for CI and log capture)",
    )
    run_parser.add_argument(
        "--retry-on",
        choices=["transient"],
        metavar="MODE",
        help="Retry transient SDK/network failures during agent send/wait (default: off)",
    )
    run_parser.add_argument(
        "--retry-max-attempts",
        type=int,
        metavar="N",
        help="Total cycle attempts when --retry-on transient is enabled (default: 3)",
    )
    run_parser.add_argument(
        "--retry-backoff-seconds",
        metavar="SECONDS",
        help="Comma-separated backoff delays between retries (default: 5,15)",
    )
    run_parser.add_argument(
        "--state-file",
        type=Path,
        metavar="PATH",
        help="Run state file for crash recovery (default: .cyclopsctl/state.json)",
    )
    run_parser.add_argument(
        "--no-state",
        action="store_true",
        help="Disable run state persistence",
    )
    run_parser.add_argument(
        "--history-file",
        type=Path,
        metavar="PATH",
        help="Run history file for resume-aware startup (default: .cyclopsctl/run-history.json)",
    )
    run_parser.add_argument(
        "--no-history",
        action="store_true",
        help="Disable run history persistence",
    )
    run_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore run history and force first-prompt bootstrap for cycle 1",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip parent tasks already recorded as completed in run history",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve task, model, and handover alignment without starting an agent",
    )
    run_parser.add_argument(
        "--git-summary",
        action="store_true",
        help="Include per-cycle git diff --stat summaries in logs and post-run output",
    )
    run_parser.add_argument(
        "--export-transcript-dir",
        type=Path,
        metavar="PATH",
        help="Write JSON transcript sidecar files per completed cycle",
    )

    path_flags = argparse.ArgumentParser(add_help=False)
    path_flags.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Path to cyclopsctl.toml (CLI flags override file values)",
    )
    path_flags.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        help="Project repository root (default: current working directory)",
    )
    path_flags.add_argument(
        "--current-handover",
        type=Path,
        metavar="PATH",
        help="Path to current-handover-prompt.md",
    )
    path_flags.add_argument(
        "--complexity-report",
        type=Path,
        metavar="PATH",
        help="Complexity report JSON (default: .cyclopsctl/reports/complexity-report.json)",
    )
    path_flags.add_argument(
        "--tag",
        help="Task queue tag context (optional)",
    )
    path_flags.add_argument(
        "--plain",
        action="store_true",
        help="Disable Rich formatting; emit plain text output",
    )
    path_flags.add_argument(
        "--state-file",
        type=Path,
        metavar="PATH",
        help="Run state file for crash recovery (default: .cyclopsctl/state.json)",
    )
    path_flags.add_argument(
        "--history-file",
        type=Path,
        metavar="PATH",
        help="Run history file for resume-aware startup (default: .cyclopsctl/run-history.json)",
    )

    launch_parser = subparsers.add_parser(
        "launch",
        parents=[shared, path_flags],
        help="Interactive pre-run setup (default when no subcommand is given)",
    )
    launch_parser.add_argument(
        "--action",
        choices=["run", "bootstrap", "doctor", "models"],
        metavar="ACTION",
        help="Launcher entry action (required in non-interactive mode without --cycles)",
    )
    launch_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Named profile for run/bootstrap flows",
    )
    launch_parser.add_argument(
        "--composer-tier",
        choices=["standard", "fast"],
        metavar="TIER",
        help="Composer tier for run flow (standard or fast)",
    )
    launch_parser.add_argument(
        "--no-opus",
        action="store_true",
        help="Disable Opus routing for high-complexity scores in run flow",
    )
    launch_parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip completed parent tasks recorded in run history",
    )
    launch_parser.add_argument(
        "--from-prd",
        type=Path,
        metavar="PATH",
        help="PRD file path for bootstrap flow (default: prd.md)",
    )
    launch_parser.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Skip analyze-complexity during bootstrap flow",
    )
    launch_parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe auto-fixes when launching doctor action",
    )
    launch_parser.add_argument(
        "--first-prompt",
        type=Path,
        metavar="PATH",
        help="First-run implementation prompt file",
    )
    launch_parser.add_argument(
        "--update-handover",
        type=Path,
        metavar="PATH",
        help="Path to update-handover-prompt.md",
    )
    launch_parser.add_argument(
        "--ai-context",
        type=Path,
        metavar="PATH",
        help="AI context file to inject into every agent prompt (default: ai-context.md)",
    )
    launch_parser.add_argument(
        "--cycles",
        type=int,
        metavar="N",
        help="Number of cycles (required in non-interactive mode)",
    )
    launch_parser.add_argument(
        "--strict-handover",
        action="store_true",
        help="Fail when handover Task ID does not match backend next",
    )
    launch_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore run history and force first-prompt bootstrap for cycle 1",
    )
    launch_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation when launching with explicit flags",
    )

    for command_name in ("doctor", "check"):
        doctor_parser = subparsers.add_parser(
            command_name,
            parents=[shared, path_flags],
            help="Run preflight diagnostics before a long cyclopsctl run",
        )
        if command_name == "check":
            doctor_parser.description = "Alias for cyclopsctl doctor"
        doctor_parser.add_argument(
            "--fix",
            action="store_true",
            help="Apply safe auto-fixes (e.g. stub .env) for fixable failures",
        )

    status_parser = subparsers.add_parser(
        "status",
        parents=[path_flags],
        help="Show persisted run state for post-crash inspection",
    )
    status_parser.description = (
        "Read the optional run state file and print a human-readable summary."
    )

    subparsers.add_parser(
        "models",
        parents=[shared],
        help="List Cursor models available for routing (execution in task 6)",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        parents=[shared, path_flags],
        help="Parse PRD via native tasks backend, analyze complexity, and sync handover",
    )
    bootstrap_parser.add_argument(
        "--from-prd",
        type=Path,
        metavar="PATH",
        help="PRD file path (default: prd.md in project root)",
    )
    bootstrap_parser.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Skip analyze-complexity after parse-prd",
    )
    bootstrap_parser.add_argument(
        "--sync-handover-only",
        action="store_true",
        help="Only regenerate current-handover-prompt.md from existing tasks",
    )
    bootstrap_parser.add_argument(
        "--append",
        action="store_true",
        help="Append parsed PRD tasks to the existing queue",
    )
    bootstrap_parser.add_argument(
        "--with-workflow",
        action="store_true",
        help="Generate project-aware workflow files from PRD and repo metadata",
    )
    bootstrap_parser.add_argument(
        "--force-workflow",
        action="store_true",
        help="Overwrite workflow files even when customized",
    )
    bootstrap_parser.add_argument(
        "--force",
        action="append",
        dest="force_workflow_paths",
        metavar="PATH",
        default=None,
        help="Force overwrite a specific workflow file (repeatable; relative to project root)",
    )
    bootstrap_parser.add_argument(
        "--tech-stack",
        metavar="TEXT",
        help="Override tech stack hint used in generated workflow files",
    )
    bootstrap_parser.add_argument(
        "--test-cmd",
        metavar="CMD",
        help="Override test command used in generated workflow files",
    )
    bootstrap_parser.add_argument(
        "--ai-context",
        type=Path,
        metavar="PATH",
        help="AI context file path used for handover environment defaults",
    )

    init_parser = subparsers.add_parser(
        "init",
        parents=[shared],
        help="Scaffold cyclopsctl.toml and starter files (profile-aware seeding)",
    )
    init_parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Path for cyclopsctl.toml output (default: cyclopsctl.toml in project root)",
    )
    init_parser.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        help="Absolute path to the project repository (default: current directory)",
    )
    init_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Named profile whose defaults are merged into the seeded cyclopsctl.toml",
    )
    init_parser.add_argument(
        "--skip-templates",
        action="store_true",
        help="Only write cyclopsctl.toml; skip prompt templates and .gitignore updates",
    )
    init_parser.add_argument(
        "--force",
        action="append",
        metavar="PATH",
        default=None,
        help="Allow overwriting an existing relative path (repeatable)",
    )
    init_parser.add_argument(
        "--force-all",
        action="store_true",
        help="Allow overwriting all existing scaffold targets",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List scaffold paths that would be written without making changes",
    )
    init_parser.add_argument(
        "--refresh-workflow",
        action="store_true",
        help="Regenerate only workflow files that match staleness heuristics",
    )
    init_parser.add_argument(
        "--from-prd",
        type=Path,
        metavar="PATH",
        help="PRD file path when prd.md is absent or non-default (default: prd.md)",
    )
    init_parser.add_argument(
        "--attach",
        action="store_true",
        help="Attach to an existing task queue when PRD is missing (skip confirmation)",
    )
    init_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Non-interactive yes for attach and PRD-bootstrap confirmation",
    )
    init_parser.add_argument(
        "--bootstrap-model",
        metavar="MODEL",
        help=(
            "Bootstrap AI preset for PRD parse and complexity analysis: "
            "auto, composer, sonnet, opus-high-thinking, sonnet-max, opus-max, "
            "or explicit Cursor model id"
        ),
    )
    init_parser.add_argument(
        "--parse-model",
        metavar="MODEL",
        help="Override parse-prd model (default: bootstrap choice or [tasks].parse_model)",
    )
    init_parser.add_argument(
        "--analyze-model",
        metavar="MODEL",
        help="Override analyze-complexity model (default: same as parse model)",
    )
    init_parser.add_argument(
        "--max-tasks",
        type=int,
        metavar="N",
        help="Optional cap on parent tasks when parsing a PRD (default: model decides)",
    )

    from cyclopsctl.tasks.cli import add_tasks_subparser

    add_tasks_subparser(subparsers)

    return parser


def _parse_args(
    argv: list[str] | None, parser: argparse.ArgumentParser
) -> tuple[argparse.Namespace | None, int]:
    try:
        return parser.parse_args(argv), 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return None, 0
        if isinstance(code, int):
            return None, code
        return None, 2


def _log_and_exit(exc: BaseException, *, message: str, **context: object) -> int:
    code = exit_code_for(exc)
    log_error(message, error=str(exc), error_type=type(exc).__name__, exit_code=code, **context)
    print(f"cyclopsctl: error: {exc}", file=sys.stderr)
    return code


def _startup_load_env(*, project_root: Path, no_env: bool) -> None:
    """Load project-root ``.env`` before preflight when enabled."""
    if no_env:
        return
    load_project_env(project_root)


def _run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    state_file = "" if getattr(args, "no_state", False) else args.state_file
    history_file = "" if getattr(args, "no_history", False) else args.history_file
    opus_enabled = False if getattr(args, "no_opus", False) else None
    try:
        config = load_run_config(
            config_path=args.config,
            profile=args.profile,
            cycles=args.cycles,
            project_root=args.project_root,
            first_prompt=args.first_prompt,
            current_handover=args.current_handover,
            update_handover=args.update_handover,
            complexity_report=args.complexity_report,
            ai_context=args.ai_context,
            require_ai_context=args.require_ai_context,
            ai_context_max_chars=args.ai_context_max_chars,
            tag=args.tag,
            default_model=args.default_model,
            strict_handover=True if args.strict_handover else None,
            task_source=args.task_source,
            pinned_task_id=args.task_id,
            plain=True if args.plain else None,
            retry_on=args.retry_on,
            retry_max_attempts=args.retry_max_attempts,
            retry_backoff_seconds=args.retry_backoff_seconds,
            state_file=state_file,
            history_file=history_file,
            fresh=True if args.fresh else None,
            resume=True if args.resume else None,
            dry_run=True if getattr(args, "dry_run", False) else None,
            git_summary=True if getattr(args, "git_summary", False) else None,
            export_transcript_dir=getattr(args, "export_transcript_dir", None),
            composer_tier=getattr(args, "composer_tier", None),
            opus_enabled=opus_enabled,
        )
    except ConfigError as exc:
        return _log_and_exit(exc, message="Configuration failed")

    try:
        _startup_load_env(project_root=config.project_root, no_env=getattr(args, "no_env", False))
    except EnvLoadError as exc:
        return _log_and_exit(exc, message="Environment loading failed")

    try:
        api_key = run_preflight()
    except PreflightError as exc:
        return _log_and_exit(exc, message="Preflight check failed")

    interrupt = RunInterruptController()
    interrupt.register()
    cycle_logger = None
    state_tracker = RunStateTracker(config.state_file, project_root=config.project_root)
    result: RunLoopResult | None = None
    exit_code = 0
    run_context = (
        nullcontext()
        if config.dry_run
        else managed_sdk_bridge(config.project_root)
    )
    try:
        with run_context:
            with managed_cycle_display(plain=config.plain, interrupt=interrupt) as cycle_logger:
                from cyclopsctl.tasks.backend import resolve_run_task_hooks

                get_next_fn, list_pending_fn, get_task_by_id_fn = resolve_run_task_hooks(
                    config.task_backend,
                )
                result = run_cycles(
                    config,
                    api_key=api_key,
                    cycle_logger=cycle_logger,
                    interrupt=interrupt,
                    state_tracker=state_tracker,
                    get_next_task_fn=get_next_fn,
                    list_pending_tasks_fn=list_pending_fn,
                    get_task_by_id_fn=get_task_by_id_fn,
                )
    except RunInterruptedError as exc:
        result = exc.partial_result
        state_tracker.mark_interrupted(
            cycle_number=exc.cycle_number,
            phase=exc.phase,
            agent_id=exc.agent_id,
            run_id=exc.run_id,
        )
        if cycle_logger is not None:
            cycle_logger.log_interrupt(
                cycle_number=exc.cycle_number,
                phase=exc.phase,
                agent_id=exc.agent_id,
                run_id=exc.run_id,
            )
        log_error(
            "Cyclopsctl run interrupted",
            cycle_number=exc.cycle_number,
            phase=exc.phase,
            agent_id=exc.agent_id,
            run_id=exc.run_id,
            exit_code=INTERRUPT_EXIT_CODE,
        )
        print("cyclopsctl: interrupted by user", file=sys.stderr)
        exit_code = INTERRUPT_EXIT_CODE
    except SdkBridgeError as exc:
        return _log_and_exit(exc, message="Cursor SDK bridge startup failed")
    except AgentRunError as exc:
        return _log_and_exit(
            exc,
            message="Agent run failed",
            kind=exc.kind.value,
            agent_id=exc.agent_id,
            run_id=exc.run_id,
        )
    except KNOWN_RUN_ERRORS as exc:
        return _log_and_exit(exc, message="Cyclopsctl run failed")
    finally:
        interrupt.restore()

    if result is not None:
        print_run_summary(
            result.outcomes,
            skipped_tasks=result.skipped_tasks,
            plain=config.plain,
        )

    if exit_code != 0:
        return exit_code

    if result is None:
        return 0

    if result.dry_run:
        print(
            f"Dry-run complete; planned {result.completed_cycles} cycle(s) at "
            f"{config.project_root}. No agent sessions were started.",
            file=sys.stderr,
        )
        return 0

    if result.empty_queue:
        tag_part = f" (tag={result.empty_queue_tag!r})" if result.empty_queue_tag else ""
        skipped_part = ""
        if result.skipped_tasks:
            skipped_part = f" Skipped {len(result.skipped_tasks)} completed task(s)."
        print(
            f"Task queue empty{tag_part}. "
            f"Completed {result.completed_cycles} verified cycle(s) in this run "
            f"at {config.project_root}.{skipped_part}",
            file=sys.stderr,
        )
    else:
        skipped_part = ""
        if result.skipped_tasks:
            skipped_part = f" Skipped {len(result.skipped_tasks)} completed task(s)."
        print(
            f"Completed {result.completed_cycles} verified cycle(s) at {config.project_root}.{skipped_part}",
            file=sys.stderr,
        )
    return 0


def run_models_inspection(
    *,
    api_key: str | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
) -> int:
    """Setup-time model inventory and routing diagnostics."""
    resolved_key = api_key if api_key is not None else os.environ.get("CURSOR_API_KEY")
    try:
        inventory = fetch_model_inventory(list_models=list_models, api_key=resolved_key)
    except ModelListingError as exc:
        log_error("Model inspection failed", error=str(exc))
        print(f"cyclopsctl: error: {exc}", file=sys.stderr)
        return STARTUP_EXIT_CODE

    print(format_models_diagnostic(inventory))
    return 0


def _status_command(args: argparse.Namespace) -> int:
    try:
        config = load_status_config(
            config_path=args.config,
            project_root=args.project_root,
            state_file=args.state_file,
            history_file=args.history_file,
        )
    except ConfigError as exc:
        return _log_and_exit(exc, message="Configuration failed")

    sections: list[str] = []
    state_path = config.state_file
    if state_path is None:
        sections.append("Run state persistence is disabled.")
    else:
        sections.append(format_state_summary(read_state(state_path)))

    history_path = config.history_file
    if history_path is None:
        sections.append("Run history persistence is disabled.")
    else:
        sections.append(format_history_summary(read_history(history_path)))

    print("\n\n".join(sections))
    return 0


def _dispatch_launch_argv(
    argv: list[str],
    parser: argparse.ArgumentParser,
) -> int:
    """Parse assembled launcher argv and delegate to the matching subcommand."""
    sub_args, parse_code = _parse_args(argv, parser)
    if sub_args is None:
        return parse_code

    if sub_args.command == "run":
        return _run_command(sub_args, parser)
    if sub_args.command in ("doctor", "check"):
        return _doctor_command(sub_args)
    if sub_args.command == "models":
        return _models_command(sub_args)
    if sub_args.command == "bootstrap":
        return _bootstrap_command(sub_args)
    return 0


def _launch_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = load_launch_config(
            config_path=args.config,
            profile=args.profile,
            project_root=args.project_root,
            first_prompt=args.first_prompt,
            current_handover=args.current_handover,
            update_handover=args.update_handover,
            complexity_report=args.complexity_report,
            ai_context=args.ai_context,
            tag=args.tag,
            plain=True if args.plain else None,
            state_file=args.state_file,
            history_file=args.history_file,
        )
    except ConfigError as exc:
        return _log_and_exit(exc, message="Configuration failed")

    try:
        _startup_load_env(project_root=config.project_root, no_env=args.no_env)
    except EnvLoadError as exc:
        return _log_and_exit(exc, message="Environment loading failed")

    opus_enabled = False if args.no_opus else None
    dispatch = run_launch(
        config,
        action=args.action,
        cycles=args.cycles,
        plain=True if args.plain else None,
        strict_handover=True if args.strict_handover else None,
        fresh=True if args.fresh else None,
        resume=True if args.resume else None,
        tag=args.tag,
        profile=args.profile,
        composer_tier=args.composer_tier,
        opus_enabled=opus_enabled,
        from_prd=args.from_prd,
        skip_analyze=True if args.skip_analyze else None,
        doctor_fix=True if args.fix else None,
        assume_yes=args.yes,
    )
    if dispatch.argv is None:
        return dispatch.exit_code

    return _dispatch_launch_argv(dispatch.argv, parser)


def _doctor_command(args: argparse.Namespace) -> int:
    try:
        config = load_doctor_config(
            config_path=args.config,
            project_root=args.project_root,
            current_handover=args.current_handover,
            complexity_report=args.complexity_report,
            tag=args.tag,
            plain=True if args.plain else None,
            fix=True if args.fix else None,
        )
    except ConfigError as exc:
        return _log_and_exit(exc, message="Configuration failed")

    try:
        _startup_load_env(project_root=config.project_root, no_env=args.no_env)
    except EnvLoadError as exc:
        return _log_and_exit(exc, message="Environment loading failed")

    return run_doctor(config)


def _models_command(args: argparse.Namespace) -> int:
    try:
        _startup_load_env(project_root=Path.cwd(), no_env=args.no_env)
    except EnvLoadError as exc:
        return _log_and_exit(exc, message="Environment loading failed")

    try:
        with managed_sdk_bridge(Path.cwd()):
            return run_models_inspection()
    except SdkBridgeError as exc:
        return _log_and_exit(exc, message="Cursor SDK bridge startup failed")


def _bootstrap_command(args: argparse.Namespace) -> int:
    try:
        config = resolve_bootstrap_config(
            project_root=args.project_root,
            from_prd=args.from_prd,
            current_handover=args.current_handover,
            complexity_report=args.complexity_report,
            ai_context=args.ai_context,
            tag=args.tag,
            skip_analyze=args.skip_analyze,
            sync_handover_only=args.sync_handover_only,
            append=args.append,
            with_workflow=args.with_workflow,
            force_workflow=args.force_workflow,
            force_workflow_paths=args.force_workflow_paths,
            tech_stack=args.tech_stack,
            test_cmd=args.test_cmd,
        )
    except BootstrapError as exc:
        return _log_and_exit(exc, message="Bootstrap configuration failed")

    needs_sdk = not config.sync_handover_only
    try:
        run_context = (
            managed_sdk_bridge(config.project_root)
            if needs_sdk
            else nullcontext()
        )
        with run_context:
            result = run_bootstrap(config)
    except SdkBridgeError as exc:
        return _log_and_exit(exc, message="Cursor SDK bridge startup failed")
    except BootstrapError as exc:
        return _log_and_exit(exc, message="Bootstrap pipeline failed")

    task_part = (
        f"task {result.task_id}"
        if result.task_id is not None
        else "empty queue (Task ID: 0)"
    )
    print(
        f"Bootstrap complete: synced {result.handover_path} for {task_part} "
        f"at {config.project_root}",
        file=sys.stderr,
    )
    if result.copied_templates:
        copied = ", ".join(result.copied_templates)
        print(f"Generated workflow files: {copied}", file=sys.stderr)
    print(
        "Next: Run `cyclopsctl doctor`, then `cyclopsctl run --cycles N`.",
        file=sys.stderr,
    )
    return 0


def _init_command(args: argparse.Namespace) -> int:
    from cyclopsctl.project_setup import (
        ProjectSetupError,
        format_ready_message,
        resolve_project_setup_config,
        run_project_setup,
    )

    try:
        config = resolve_project_setup_config(
            project_root=args.project_root,
            prd_path=args.from_prd,
            config_path=args.config,
            profile=args.profile,
            skip_templates=args.skip_templates,
            force_paths=args.force,
            force_all=args.force_all,
            dry_run=args.dry_run,
            refresh_workflow=args.refresh_workflow,
            attach=args.attach,
            assume_yes=args.yes,
            bootstrap_model=args.bootstrap_model,
            parse_model=args.parse_model,
            analyze_model=args.analyze_model,
            max_tasks=args.max_tasks,
        )
    except ProjectSetupError as exc:
        return _log_and_exit(exc, message="Init setup failed")

    try:
        _startup_load_env(project_root=config.project_root, no_env=args.no_env)
    except EnvLoadError as exc:
        return _log_and_exit(exc, message="Environment loading failed")

    try:
        run_context = (
            nullcontext()
            if config.dry_run
            else managed_sdk_bridge(config.project_root)
        )
        with run_context:
            result = run_project_setup(config)
    except SdkBridgeError as exc:
        return _log_and_exit(exc, message="Cursor SDK bridge startup failed")
    except ProjectSetupError as exc:
        return _log_and_exit(exc, message="Init setup failed")

    if result.dry_run:
        print("Dry run — would apply:", file=sys.stderr)
        for item in result.planned_paths:
            print(f"  {item}", file=sys.stderr)
        return 0

    if config.profile is not None:
        print(f"Applied profile: {config.profile}", file=sys.stderr)
    if result.repairs:
        print(f"Applied: {', '.join(result.repairs)}", file=sys.stderr)
    print(format_ready_message(result), file=sys.stderr)
    return 0


_KNOWN_COMMANDS = frozenset(
    {"run", "launch", "doctor", "check", "status", "models", "bootstrap", "init", "tasks"}
)


def _normalize_argv(argv: list[str] | None) -> list[str] | None:
    """Default bare ``cyclopsctl`` invocations to ``cyclopsctl launch``."""
    if argv is None:
        return None
    if not argv:
        return ["launch"]
    if argv[0] in ("-h", "--help", "--version", "-V"):
        return argv
    if argv[0] not in _KNOWN_COMMANDS:
        return ["launch", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    """Console script entrypoint."""
    parser = _build_parser()
    args, exit_code = _parse_args(_normalize_argv(argv), parser)
    if args is None:
        return exit_code

    if args.command == "run":
        return _run_command(args, parser)

    if args.command == "launch":
        return _launch_command(args, parser)

    if args.command in ("doctor", "check"):
        return _doctor_command(args)

    if args.command == "status":
        return _status_command(args)

    if args.command == "models":
        return _models_command(args)

    if args.command == "bootstrap":
        return _bootstrap_command(args)

    if args.command == "init":
        return _init_command(args)

    if args.command == "tasks":
        from cyclopsctl.tasks.cli import run_tasks_command

        return run_tasks_command(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
