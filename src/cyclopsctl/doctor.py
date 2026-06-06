"""Preflight-only diagnostics before a long cyclopsctl run (task 5)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from rich.console import Console
from rich.table import Table

from cyclopsctl.alignment import compare_handover_to_backend_next
from cyclopsctl.config import DoctorConfig, LaunchConfig
from cyclopsctl.errors import GENERAL_EXIT_CODE, STARTUP_EXIT_CODE
from cyclopsctl.preflight import PreflightError, check_cursor_api_key
from cyclopsctl.prompt import PromptError, parse_task_id, read_prompt_text
from cyclopsctl.routing import parse_complexity_payload
from cyclopsctl.sdk_bridge import (
    SdkBridgeError,
    bridge_env_configured,
    managed_sdk_bridge,
    needs_windows_bridge_bootstrap,
)
from cyclopsctl.tasks.cli import get_next_task, list_pending_tasks
from cyclopsctl.tasks.types import NextTaskLookup
from cyclopsctl.project_setup import (
    is_brownfield_attach_context,
    project_has_non_empty_tasks,
)
from cyclopsctl.tasks.backend import TaskBackend, TaskBackendConfig, get_task_backend
from cyclopsctl.tasks.cli import TasksCliError
from cyclopsctl.tasks.store import native_tasks_storage_exists
from cyclopsctl.tasks.store import NATIVE_TASKS_JSON_REL, TaskStoreCorruptError, load_tasks_document

_BROWNFIELD_BACKEND_ERRORS = (TasksCliError, ValueError, RuntimeError)

_STARTUP_CHECKS = frozenset({"CURSOR_API_KEY", "Cursor SDK bridge"})


_ENV_STUB_CONTENT = """# Cyclopsctl — add your API key below
# Get a Cursor key from https://cursor.com/settings
CURSOR_API_KEY=
"""


@dataclass(frozen=True)
class DiagnosticCheck:
    """Single doctor diagnostic outcome."""

    name: str
    passed: bool
    detail: str
    informational: bool = False
    remediation: str | None = None


def _project_root_hint(project_root: Path) -> str:
    return str(project_root)


def _attach_init_remediation(project_root: Path) -> str:
    root = _project_root_hint(project_root)
    return (
        f"Run `cyclopsctl init --attach --yes --project-root {root}` "
        "to scaffold from the existing task queue."
    )


def _suggests_attach_init(project_root: Path) -> bool:
    return project_has_non_empty_tasks(project_root) and is_brownfield_attach_context(
        project_root
    )


def remediation_for_check(check: DiagnosticCheck, project_root: Path) -> str | None:
    """Return context-aware remediation text for a check when applicable."""
    root = _project_root_hint(project_root)

    if check.name == "CURSOR_API_KEY" and not check.passed:
        return (
            "Create `.env` in the project root with `CURSOR_API_KEY=...`, or export the key "
            f"in your shell. Run `cyclopsctl init --project-root {root}` to scaffold starter files."
        )

    if check.name == "Cursor SDK bridge" and not check.passed:
        return (
            "Ensure the Cursor SDK bridge is running or set bridge environment variables. "
            "On Windows, restart the terminal and retry `cyclopsctl doctor`."
        )

    if check.name == "Handover Task ID":
        if not check.passed:
            if "not found" in check.detail.lower():
                if _suggests_attach_init(project_root):
                    return _attach_init_remediation(project_root)
                return (
                    f"Run `cyclopsctl init --project-root {root}` to scaffold handover files, "
                    f"or `cyclopsctl bootstrap --from-prd prd.md --project-root {root}`."
                )
            return (
                "Add `# Task ID: <n>` (parent task only) to the handover file. "
                f"Run `cyclopsctl bootstrap --sync-handover-only --project-root {root}` "
                "to regenerate from the task queue."
            )
        return None

    if check.name == "Native tasks" and not check.passed:
        if _suggests_attach_init(project_root):
            return _attach_init_remediation(project_root)
        return (
            f"Run `cyclopsctl init --project-root {root}` "
            f"or `cyclopsctl bootstrap --from-prd prd.md --project-root {root}`."
        )

    if check.name == "Complexity report" and not check.passed:
        if "not found" in check.detail.lower():
            return (
                f"Run `cyclopsctl bootstrap --project-root {root}` "
                "to analyze complexity after parse-prd."
            )
        return (
            f"Fix the JSON syntax or re-run `cyclopsctl bootstrap --project-root {root}`."
        )

    if check.name == "Backend next":
        if not check.passed:
            if _suggests_attach_init(project_root):
                return _attach_init_remediation(project_root)
            return (
                f"Run `cyclopsctl bootstrap --from-prd prd.md --project-root {root}` "
                "to initialize the native task queue."
            )
        if check.informational and "queue is empty" in check.detail.lower():
            return (
                f"Run `cyclopsctl bootstrap --from-prd prd.md --project-root {root}` "
                "or `cyclopsctl tasks parse-prd --append --input=prd.md`."
            )
        return None

    if check.name == "Task queue list" and check.informational and "0 pending" in check.detail:
        return (
            f"Run `cyclopsctl bootstrap --from-prd prd.md --project-root {root}` "
            "or `cyclopsctl tasks parse-prd --append --input=prd.md`."
        )

    if check.name in {"Update handover", "ai-context"} and not check.passed:
        if _suggests_attach_init(project_root):
            return _attach_init_remediation(project_root)
        return f"Run `cyclopsctl init --project-root {root}` to scaffold missing workflow files."

    if check.name == "Handover drift" and check.informational:
        return (
            f"Run `cyclopsctl bootstrap --sync-handover-only --project-root {root}`, "
            "or `cyclopsctl tasks show <id>` to inspect queue state. "
            "See docs/guides/testing-guide.md."
        )

    if check.name == "Handover in queue" and check.informational:
        return (
            f"Sync handover from the lowest pending parent with "
            f"`cyclopsctl bootstrap --sync-handover-only --project-root {root}`."
        )

    if check.name == "Handover task status" and check.informational:
        return (
            f"Rewrite `current-handover-prompt.md` for the next pending parent task "
            f"or run `cyclopsctl bootstrap --sync-handover-only --project-root {root}`."
        )

    if check.name == "Stale workflow" and check.informational:
        return (
            f"Run `cyclopsctl init --refresh-workflow --project-root {root}` "
            "to safely regenerate stale workflow files, or merge native "
            "`cyclopsctl tasks` CLI references manually."
        )

    if check.name == "Active tag" and check.informational:
        return (
            f"Align tag with `cyclopsctl tasks use-tag <name> --project-root {root}` "
            "or pass `--tag` matching the active queue."
        )

    if check.name == "Complexity report" and check.informational and "not found" in check.detail.lower():
        return (
            f"Run `cyclopsctl bootstrap --project-root {root}` "
            "to analyze complexity after parse-prd."
        )

    return None


def enrich_checks_with_remediation(
    checks: list[DiagnosticCheck],
    project_root: Path,
) -> list[DiagnosticCheck]:
    """Attach remediation hints to checks that need them."""
    enriched: list[DiagnosticCheck] = []
    for check in checks:
        remediation = check.remediation or remediation_for_check(check, project_root)
        if remediation is None:
            enriched.append(check)
        else:
            enriched.append(replace(check, remediation=remediation))
    return enriched


def format_check_detail(check: DiagnosticCheck) -> str:
    """Render check detail plus optional remediation for plain and Rich output."""
    if check.remediation is None:
        return check.detail
    return f"{check.detail}\n  Remediation: {check.remediation}"


def check_api_key(*, env: Mapping[str, str] | None = None) -> DiagnosticCheck:
    """Verify ``CURSOR_API_KEY`` is present after optional ``.env`` loading."""
    try:
        check_cursor_api_key(env=env)
    except PreflightError as exc:
        return DiagnosticCheck(name="CURSOR_API_KEY", passed=False, detail=str(exc))
    return DiagnosticCheck(
        name="CURSOR_API_KEY",
        passed=True,
        detail="CURSOR_API_KEY is set",
    )


def check_path_file(path: Path, *, name: str) -> DiagnosticCheck:
    """Verify a project file exists at ``path``."""
    if path.is_file():
        return DiagnosticCheck(
            name=name,
            passed=True,
            detail=f"Found {path.name} at {path}",
        )
    return DiagnosticCheck(
        name=name,
        passed=False,
        detail=f"{name} not found: {path}",
    )


def check_update_handover(path: Path) -> DiagnosticCheck:
    """Verify the update handover prompt file exists."""
    return check_path_file(path, name="Update handover")


def check_ai_context(path: Path) -> DiagnosticCheck:
    """Verify ``ai-context.md`` (or configured path) exists."""
    return check_path_file(path, name="ai-context")


def check_handover_task_id(path: Path) -> DiagnosticCheck:
    """Verify the handover file exists and contains a parseable parent Task ID."""
    if not path.is_file():
        return DiagnosticCheck(
            name="Handover Task ID",
            passed=False,
            detail=f"Handover file not found: {path}",
        )
    try:
        raw = read_prompt_text(path)
        task_id = parse_task_id(raw, required=True)
    except PromptError as exc:
        return DiagnosticCheck(
            name="Handover Task ID",
            passed=False,
            detail=str(exc),
        )
    if task_id == 0:
        return DiagnosticCheck(
            name="Handover Task ID",
            passed=False,
            detail=(
                f"Handover has placeholder Task ID 0 in {path.name}. "
                "Sync the handover from the task queue before starting a run."
            ),
        )
    return DiagnosticCheck(
        name="Handover Task ID",
        passed=True,
        detail=f"Parsed Task ID {task_id} from {path.name}",
    )


def check_native_tasks_directory(project_root: Path) -> DiagnosticCheck:
    """Verify native tasks storage is readable when present; greenfield passes informatively."""
    tasks_json = (project_root / NATIVE_TASKS_JSON_REL).resolve()
    if not tasks_json.is_file():
        tasks_dir = tasks_json.parent
        if tasks_dir.is_dir():
            return DiagnosticCheck(
                name="Native tasks",
                passed=False,
                detail=f"Native tasks directory exists but tasks.json is missing: {tasks_json}",
            )
        return DiagnosticCheck(
            name="Native tasks",
            passed=True,
            detail="No native tasks yet (greenfield)",
            informational=True,
        )

    try:
        load_tasks_document(tasks_json)
    except TaskStoreCorruptError as exc:
        return DiagnosticCheck(
            name="Native tasks",
            passed=False,
            detail=str(exc),
        )
    except OSError as exc:
        return DiagnosticCheck(
            name="Native tasks",
            passed=False,
            detail=f"Cannot read native tasks at {tasks_json}: {exc}",
        )

    return DiagnosticCheck(
        name="Native tasks",
        passed=True,
        detail=f"Readable native tasks at {tasks_json.name}",
    )


def check_complexity_report_readable(
    path: Path,
    *,
    warn_missing: bool = False,
) -> DiagnosticCheck:
    """Verify the complexity report file exists and contains valid JSON."""
    if not path.is_file():
        if warn_missing:
            return DiagnosticCheck(
                name="Complexity report",
                passed=True,
                detail=f"Complexity report not found: {path}",
                informational=True,
            )
        return DiagnosticCheck(
            name="Complexity report",
            passed=False,
            detail=f"Complexity report not found: {path}",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return DiagnosticCheck(
            name="Complexity report",
            passed=False,
            detail=f"Cannot read complexity report at {path}: {exc}",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return DiagnosticCheck(
            name="Complexity report",
            passed=False,
            detail=f"Invalid JSON in complexity report at {path}: {exc}",
        )
    if not isinstance(data, dict):
        return DiagnosticCheck(
            name="Complexity report",
            passed=False,
            detail=f"Complexity report root must be a JSON object: {path}",
        )

    scores = parse_complexity_payload(data)
    count = len(scores)
    noun = "entry" if count == 1 else "entries"
    return DiagnosticCheck(
        name="Complexity report",
        passed=True,
        detail=f"Readable JSON with {count} complexity {noun} at {path.name}",
    )


def check_tasks_pending_list(
    project_root: Path,
    *,
    tag: str | None = None,
    runner: Callable | None = None,
    backend: TaskBackend | None = None,
) -> DiagnosticCheck:
    """Verify pending task listing succeeds and summarize the queue."""
    try:
        if backend is not None:
            tasks = backend.list_pending(project_root, tag=tag)
        else:
            tasks = list_pending_tasks(project_root, tag=tag)
    except (TasksCliError, ValueError, RuntimeError) as exc:
        return DiagnosticCheck(
            name="Task queue list",
            passed=False,
            detail=str(exc),
        )

    count = len(tasks)
    noun = "task" if count == 1 else "tasks"
    if count == 0:
        tag_part = f" (tag={tag!r})" if tag else ""
        return DiagnosticCheck(
            name="Task queue list",
            passed=True,
            detail=f"0 pending tasks{tag_part}",
            informational=True,
        )

    tag_part = f", tag={tag!r}" if tag else ""
    return DiagnosticCheck(
        name="Task queue list",
        passed=True,
        detail=f"{count} pending {noun}{tag_part}",
    )


def check_tasks_next(
    project_root: Path,
    *,
    tag: str | None = None,
    runner: Callable | None = None,
    backend: TaskBackend | None = None,
) -> DiagnosticCheck:
    """Verify next-task lookup succeeds and reports a task or an empty queue."""
    try:
        if backend is not None:
            lookup = backend.get_next(project_root, tag=tag)
        else:
            lookup = get_next_task(project_root, tag=tag)
    except (TasksCliError, ValueError, RuntimeError) as exc:
        return DiagnosticCheck(
            name="Backend next",
            passed=False,
            detail=str(exc),
        )

    return _format_next_task_check(lookup)


def _format_next_task_check(lookup: NextTaskLookup) -> DiagnosticCheck:
    if lookup.found and lookup.task is not None:
        task = lookup.task
        tag_part = f", tag={lookup.tag!r}" if lookup.tag else ""
        return DiagnosticCheck(
            name="Backend next",
            passed=True,
            detail=(
                f"Next task #{task.task_id}: {task.title!r}{tag_part}"
            ),
        )

    tag_part = f" (tag={lookup.tag!r})" if lookup.tag else ""
    return DiagnosticCheck(
        name="Backend next",
        passed=True,
        detail=f"Task queue is empty{tag_part}",
        informational=True,
    )


def check_sdk_bridge(
    project_root: Path,
    *,
    bridge_manager: Callable[[Path], Iterator[object | None]] | None = None,
) -> DiagnosticCheck:
    """Verify Cursor SDK bridge reachability where applicable."""
    if bridge_env_configured():
        return DiagnosticCheck(
            name="Cursor SDK bridge",
            passed=True,
            detail="Bridge endpoint configured via environment",
        )
    if not needs_windows_bridge_bootstrap():
        return DiagnosticCheck(
            name="Cursor SDK bridge",
            passed=True,
            detail="Bridge bootstrap not required on this platform",
        )

    manager = bridge_manager or managed_sdk_bridge
    try:
        with manager(project_root):
            pass
    except SdkBridgeError as exc:
        return DiagnosticCheck(
            name="Cursor SDK bridge",
            passed=False,
            detail=str(exc),
        )
    return DiagnosticCheck(
        name="Cursor SDK bridge",
        passed=True,
        detail="Windows bridge bootstrap succeeded",
    )


def check_stale_workflow_files(
    project_root: Path,
    *,
    ai_context: Path | None = None,
    update_handover: Path | None = None,
    current_handover: Path | None = None,
) -> DiagnosticCheck:
    """Warn when workflow files predate native ``cyclopsctl tasks`` conventions."""
    from cyclopsctl.workflow_gen import detect_stale_workflow_files

    stale_entries = detect_stale_workflow_files(
        project_root,
        ai_context=ai_context,
        update_handover=update_handover,
        current_handover=current_handover,
    )

    if stale_entries:
        details = [
            f"{entry.relative_path} ({', '.join(entry.reasons)})"
            for entry in stale_entries
        ]
        return DiagnosticCheck(
            name="Stale workflow",
            passed=True,
            detail=f"Workflow files need native CLI upgrade: {'; '.join(details)}",
            informational=True,
        )
    return DiagnosticCheck(
        name="Stale workflow",
        passed=True,
        detail="Workflow files use native cyclopsctl tasks CLI references",
    )


def check_active_tag_mismatch(
    project_root: Path,
    backend: TaskBackend,
    *,
    config_tag: str | None = None,
) -> DiagnosticCheck | None:
    """Warn when configured tag differs from backend active tag."""
    if not config_tag:
        return None

    active_tag = backend.current_tag(project_root)
    if active_tag == config_tag:
        return DiagnosticCheck(
            name="Active tag",
            passed=True,
            detail=f"Active tag {active_tag!r} matches config",
        )

    return DiagnosticCheck(
        name="Active tag",
        passed=True,
        detail=(
            f"Config tag {config_tag!r} differs from active tag {active_tag!r} "
            f"in task state"
        ),
        informational=True,
    )


def check_workspace_dirty(project_root: Path) -> DiagnosticCheck | None:
    """Report uncommitted git changes as an informational brownfield warning."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return DiagnosticCheck(
            name="Workspace dirty",
            passed=True,
            detail="Worktree clean",
        )

    count = len(lines)
    noun = "change" if count == 1 else "changes"
    return DiagnosticCheck(
        name="Workspace dirty",
        passed=True,
        detail=f"{count} uncommitted {noun} in worktree",
        informational=True,
    )


def check_handover_in_queue(
    handover_path: Path,
    project_root: Path,
    backend: TaskBackend,
    *,
    tag: str | None = None,
) -> DiagnosticCheck | None:
    """Warn when the handover parent task id is absent from the queue."""
    if not handover_path.is_file():
        return None
    try:
        raw = read_prompt_text(handover_path)
        handover_task_id = parse_task_id(raw, required=False)
    except PromptError:
        return None
    if handover_task_id is None or handover_task_id == 0:
        return None

    try:
        backend.show(project_root, str(handover_task_id), tag=tag)
    except _BROWNFIELD_BACKEND_ERRORS as exc:
        message = str(exc).lower()
        if "not found" in message:
            return DiagnosticCheck(
                name="Handover in queue",
                passed=True,
                detail=f"Handover task {handover_task_id} not found in queue",
                informational=True,
            )
        return DiagnosticCheck(
            name="Handover in queue",
            passed=False,
            detail=str(exc),
        )

    return DiagnosticCheck(
        name="Handover in queue",
        passed=True,
        detail=f"Handover task {handover_task_id} exists in queue",
    )


def check_handover_task_status(
    handover_path: Path,
    project_root: Path,
    backend: TaskBackend,
    *,
    tag: str | None = None,
) -> DiagnosticCheck | None:
    """Warn when handover points at a done task while pending work remains."""
    if not handover_path.is_file():
        return None
    try:
        raw = read_prompt_text(handover_path)
        handover_task_id = parse_task_id(raw, required=False)
    except PromptError:
        return None
    if handover_task_id is None or handover_task_id == 0:
        return None

    try:
        pending = backend.list_pending(project_root, tag=tag)
    except _BROWNFIELD_BACKEND_ERRORS as exc:
        return DiagnosticCheck(
            name="Handover task status",
            passed=False,
            detail=str(exc),
        )

    if not pending:
        return DiagnosticCheck(
            name="Handover task status",
            passed=True,
            detail="No pending tasks in queue",
            informational=True,
        )

    try:
        detail = backend.show(project_root, str(handover_task_id), tag=tag)
    except _BROWNFIELD_BACKEND_ERRORS as exc:
        message = str(exc).lower()
        if "not found" in message:
            return None
        return DiagnosticCheck(
            name="Handover task status",
            passed=False,
            detail=str(exc),
        )

    status = (detail.status or "").strip().lower()
    if status == "done":
        return DiagnosticCheck(
            name="Handover task status",
            passed=True,
            detail=(
                f"Handover task {handover_task_id} is done but "
                f"{len(pending)} pending task(s) remain"
            ),
            informational=True,
        )

    return DiagnosticCheck(
        name="Handover task status",
        passed=True,
        detail=f"Handover task {handover_task_id} status is {status or 'unknown'}",
    )


def check_handover_drift(
    handover_path: Path,
    project_root: Path,
    backend: TaskBackend,
    *,
    tag: str | None = None,
) -> DiagnosticCheck:
    """Warn when handover Task ID is missing or differs from ``backend.get_next()``."""
    if not handover_path.is_file():
        return DiagnosticCheck(
            name="Handover drift",
            passed=True,
            detail=f"Handover file not found: {handover_path}",
            informational=True,
        )

    try:
        raw = read_prompt_text(handover_path)
        handover_task_id = parse_task_id(raw, required=False)
    except PromptError as exc:
        return DiagnosticCheck(
            name="Handover drift",
            passed=True,
            detail=str(exc),
            informational=True,
        )

    if handover_task_id is None:
        return DiagnosticCheck(
            name="Handover drift",
            passed=True,
            detail=f"Handover missing `# Task ID:` marker in {handover_path.name}",
            informational=True,
        )
    if handover_task_id == 0:
        return DiagnosticCheck(
            name="Handover drift",
            passed=True,
            detail=(
                f"Handover has placeholder Task ID 0 in {handover_path.name}; "
                "sync from queue before starting a run"
            ),
            informational=True,
        )

    alignment = compare_handover_to_backend_next(
        handover_path,
        project_root,
        backend,
        tag=tag,
    )
    if not alignment.checked:
        reason = alignment.skipped_reason or "alignment check skipped"
        return DiagnosticCheck(
            name="Handover drift",
            passed=True,
            detail=reason,
            informational=True,
        )
    if alignment.aligned:
        return DiagnosticCheck(
            name="Handover drift",
            passed=True,
            detail=(
                f"Handover task {alignment.handover_task_id} matches "
                f"backend.get_next() task {alignment.next_task_id}"
            ),
        )

    return DiagnosticCheck(
        name="Handover drift",
        passed=True,
        detail=(
            f"Handover task {alignment.handover_task_id} differs from "
            f"backend.get_next() task {alignment.next_task_id}"
        ),
        informational=True,
    )


def run_brownfield_readiness_checks(
    project_root: Path,
    backend: TaskBackend,
    *,
    current_handover: Path,
    ai_context: Path | None = None,
    update_handover: Path | None = None,
    config_tag: str | None = None,
    include_workspace_dirty: bool = True,
) -> list[DiagnosticCheck]:
    """Run warn-by-default brownfield readiness checks via ``TaskBackend``."""
    checks: list[DiagnosticCheck] = [
        check_handover_drift(
            current_handover,
            project_root,
            backend,
            tag=config_tag,
        ),
    ]

    in_queue = check_handover_in_queue(
        current_handover,
        project_root,
        backend,
        tag=config_tag,
    )
    if in_queue is not None:
        checks.append(in_queue)

    task_status = check_handover_task_status(
        current_handover,
        project_root,
        backend,
        tag=config_tag,
    )
    if task_status is not None:
        checks.append(task_status)

    checks.append(
        check_stale_workflow_files(
            project_root,
            ai_context=ai_context,
            update_handover=update_handover,
            current_handover=current_handover,
        )
    )

    tag_check = check_active_tag_mismatch(
        project_root,
        backend,
        config_tag=config_tag,
    )
    if tag_check is not None:
        checks.append(tag_check)

    if include_workspace_dirty:
        dirty_check = check_workspace_dirty(project_root)
        if dirty_check is not None:
            checks.append(dirty_check)

    return checks


def _resolve_brownfield_backend(
    config: DoctorConfig | LaunchConfig,
    *,
    runner: Callable | None = None,
) -> TaskBackend:
    _ = (config, runner)
    return get_task_backend(TaskBackendConfig(task_backend="native"))


def run_diagnostics(
    config: DoctorConfig,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable | None = None,
    bridge_manager: Callable[[Path], Iterator[object | None]] | None = None,
    brownfield_ai_context: Path | None = None,
    brownfield_update_handover: Path | None = None,
    include_workspace_dirty: bool = False,
) -> list[DiagnosticCheck]:
    """Run all doctor checks and return ordered results."""
    _ = which
    checks = [check_api_key(env=env)]
    checks.append(check_sdk_bridge(config.project_root, bridge_manager=bridge_manager))
    checks.append(check_native_tasks_directory(config.project_root))
    if native_tasks_storage_exists(config.project_root):
        backend = _resolve_brownfield_backend(config, runner=runner)
        checks.append(
            check_complexity_report_readable(
                config.complexity_report,
                warn_missing=True,
            )
        )
        checks.extend(
            run_brownfield_readiness_checks(
                config.project_root,
                backend,
                current_handover=config.current_handover,
                ai_context=brownfield_ai_context,
                update_handover=brownfield_update_handover,
                config_tag=config.tag,
                include_workspace_dirty=include_workspace_dirty,
            )
        )

    return enrich_checks_with_remediation(checks, config.project_root)


def run_launch_diagnostics(
    config: LaunchConfig,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable | None = None,
    bridge_manager: Callable[[Path], Iterator[object | None]] | None = None,
) -> list[DiagnosticCheck]:
    """Run launcher preflight checks (doctor checks plus file and list summary)."""
    doctor_config = DoctorConfig(
        project_root=config.project_root,
        current_handover=config.current_handover,
        complexity_report=config.complexity_report,
        task_backend=config.task_backend,
        tag=config.tag,
        plain=config.plain,
        state_file=config.state_file,
    )
    checks = run_diagnostics(
        doctor_config,
        env=env,
        which=which,
        runner=runner,
        bridge_manager=bridge_manager,
        brownfield_ai_context=config.ai_context,
        brownfield_update_handover=config.update_handover,
        include_workspace_dirty=True,
    )
    native_backend = get_task_backend(TaskBackendConfig(task_backend="native"))
    launch_checks = [
        check_update_handover(config.update_handover),
        check_ai_context(config.ai_context),
        check_tasks_next(
            config.project_root,
            tag=config.tag,
            backend=native_backend,
        ),
        check_tasks_pending_list(
            config.project_root,
            tag=config.tag,
            backend=native_backend,
        ),
    ]
    checks.extend(launch_checks)
    return enrich_checks_with_remediation(checks, config.project_root)


def exit_code_for_checks(checks: list[DiagnosticCheck]) -> int:
    """Map failed checks to startup vs general exit codes."""
    if all(check.passed for check in checks):
        return 0
    if any(not check.passed and check.name in _STARTUP_CHECKS for check in checks):
        return STARTUP_EXIT_CODE
    return GENERAL_EXIT_CODE


def format_diagnostics_plain(checks: list[DiagnosticCheck]) -> str:
    """Render doctor results as plain text."""
    lines: list[str] = ["Cyclopsctl doctor diagnostics", ""]
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        if check.passed and check.informational:
            status = "NOTE"
        lines.append(f"[{status}] {check.name}: {format_check_detail(check)}")
    lines.append("")
    if all(check.passed for check in checks):
        lines.append("All checks passed.")
    else:
        failed = sum(1 for check in checks if not check.passed)
        lines.append(f"{failed} check(s) failed.")
    return "\n".join(lines)


def render_diagnostics_rich(checks: list[DiagnosticCheck]) -> Table:
    """Build a Rich table for TTY output."""
    table = Table(title="Cyclopsctl doctor diagnostics", show_header=True)
    table.add_column("Status", style="bold", no_wrap=True)
    table.add_column("Check")
    table.add_column("Detail")

    for check in checks:
        if not check.passed:
            status = "[red]FAIL[/red]"
        elif check.informational:
            status = "[cyan]NOTE[/cyan]"
        else:
            status = "[green]PASS[/green]"
        table.add_row(status, check.name, format_check_detail(check))

    return table


def print_diagnostics(
    checks: list[DiagnosticCheck],
    *,
    plain: bool,
    stderr_is_tty: bool | None = None,
) -> None:
    """Print doctor results to stderr using Rich when appropriate."""
    use_rich = not plain and (stderr_is_tty if stderr_is_tty is not None else sys.stderr.isatty())
    if use_rich:
        console = Console(stderr=True)
        console.print(render_diagnostics_rich(checks))
        if all(check.passed for check in checks):
            console.print("[green]All checks passed.[/green]")
        else:
            failed = sum(1 for check in checks if not check.passed)
            console.print(f"[red]{failed} check(s) failed.[/red]")
        return

    print(format_diagnostics_plain(checks), file=sys.stderr)


def apply_safe_fixes(
    checks: list[DiagnosticCheck],
    config: DoctorConfig,
) -> list[str]:
    """Apply limited safe filesystem fixes; never run parse-prd or agents."""
    actions: list[str] = []
    env_path = (config.project_root / ".env").resolve()

    failed_names = {check.name for check in checks if not check.passed}
    if "CURSOR_API_KEY" in failed_names and not env_path.is_file():
        env_path.write_text(_ENV_STUB_CONTENT, encoding="utf-8", newline="\n")
        actions.append(f"Created stub .env at {env_path}")

    return actions


def format_fix_actions(actions: list[str]) -> str:
    """Render auto-fix actions as plain text."""
    if not actions:
        return ""
    lines = ["Applied safe fixes:", *([f"  - {action}" for action in actions]), ""]
    return "\n".join(lines)


def print_fix_actions(
    actions: list[str],
    *,
    plain: bool,
    stderr_is_tty: bool | None = None,
) -> None:
    """Print auto-fix summary to stderr."""
    if not actions:
        return
    use_rich = not plain and (stderr_is_tty if stderr_is_tty is not None else sys.stderr.isatty())
    if use_rich:
        console = Console(stderr=True)
        console.print("[cyan]Applied safe fixes:[/cyan]")
        for action in actions:
            console.print(f"  - {action}")
        console.print("")
        return
    print(format_fix_actions(actions), file=sys.stderr)


@contextmanager
def _noop_bridge(_workspace: Path) -> Iterator[None]:
    yield None


def run_doctor(
    config: DoctorConfig,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable | None = None,
    bridge_manager: Callable[[Path], Iterator[object | None]] | None = None,
    stderr_is_tty: bool | None = None,
) -> int:
    """Execute doctor diagnostics and return a process exit code."""
    if bridge_manager is None and not needs_windows_bridge_bootstrap():
        bridge_manager = _noop_bridge

    checks = run_diagnostics(
        config,
        env=env,
        which=which,
        runner=runner,
        bridge_manager=bridge_manager,
    )
    if config.fix:
        fix_actions = apply_safe_fixes(checks, config)
        print_fix_actions(fix_actions, plain=config.plain, stderr_is_tty=stderr_is_tty)
        if fix_actions:
            checks = run_diagnostics(
                config,
                env=env,
                which=which,
                runner=runner,
                bridge_manager=bridge_manager,
            )
    print_diagnostics(checks, plain=config.plain, stderr_is_tty=stderr_is_tty)
    return exit_code_for_checks(checks)
