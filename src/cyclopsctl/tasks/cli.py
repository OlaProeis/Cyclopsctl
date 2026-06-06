"""Native task CRUD operations and ``cyclopsctl tasks`` CLI output formatting."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from cyclopsctl.config import resolve_project_root
from cyclopsctl.tasks.store import (
    TaskStore,
    TaskStoreCorruptError,
    TaskStoreError,
    TaskStoreNotFoundError,
    TaskStoreValidationError,
)
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult, TaskShowDetail

DONE_STATUS = "done"
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "in-progress",
        "done",
        "review",
        "cancelled",
        "blocked",
        "deferred",
    }
)


class TasksCliError(RuntimeError):
    """Native tasks CLI failure with a stable exit code."""

    exit_code: int = 1


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _numeric_id(task: dict[str, Any]) -> int:
    task_id = task.get("id")
    if task_id is None:
        raise TasksCliError("task record is missing id")
    try:
        return int(task_id)
    except (TypeError, ValueError) as exc:
        raise TasksCliError(f"invalid task id: {task_id!r}") from exc


def _task_status(task: dict[str, Any]) -> str:
    status = task.get("status")
    if not isinstance(status, str) or not status.strip():
        raise TasksCliError("task record is missing status")
    return status.strip()


def _dependencies(task: dict[str, Any]) -> list[str]:
    deps = task.get("dependencies", [])
    if deps is None:
        return []
    if not isinstance(deps, list):
        raise TasksCliError("task dependencies must be a list")
    return [str(dep) for dep in deps]


def _tasks_by_id(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise TasksCliError("each task must be a JSON object")
        indexed[str(_numeric_id(task))] = task
    return indexed


def _resolve_tag(store: TaskStore, tag: str | None) -> str:
    return tag if tag is not None else store.current_tag()


def _load_tag_tasks(store: TaskStore, tag: str | None) -> tuple[str, list[dict[str, Any]]]:
    active_tag = _resolve_tag(store, tag)
    try:
        tasks = store.load_tag_tasks(active_tag)
    except TaskStoreNotFoundError as exc:
        raise TasksCliError(str(exc)) from exc
    except TaskStoreCorruptError as exc:
        raise TasksCliError(str(exc)) from exc
    return active_tag, tasks


def _find_task(
    tasks: list[dict[str, Any]],
    task_id: str,
) -> dict[str, Any] | None:
    normalized = str(task_id).strip()
    if not normalized:
        raise TasksCliError("task id must be non-empty")
    for task in tasks:
        if str(_numeric_id(task)) == normalized:
            return task
    return None


def _dependencies_satisfied(
    task: dict[str, Any],
    *,
    tasks_by_id: dict[str, dict[str, Any]],
) -> bool:
    for dep in _dependencies(task):
        dep_task = tasks_by_id.get(dep)
        if dep_task is None:
            return False
        if _task_status(dep_task) != DONE_STATUS:
            return False
    return True


def _summary_task_dict(task: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(_numeric_id(task)),
        "title": str(task.get("title", "")),
    }
    status = task.get("status")
    if status is not None:
        payload["status"] = str(status)
    priority = task.get("priority")
    if priority is not None:
        payload["priority"] = str(priority)
    complexity = task.get("complexity")
    if complexity is not None:
        payload["complexity"] = complexity
    deps = _dependencies(task)
    if deps:
        payload["dependencies"] = deps
    description = task.get("description")
    if description is not None:
        payload["description"] = str(description)
    return payload


LIST_STATUS_FILTERS: frozenset[str] = frozenset(
    {"all", "pending", "done", *ALLOWED_STATUSES}
)


def _filter_tasks_by_status(
    tasks: list[dict[str, Any]],
    status_filter: str | None,
) -> list[dict[str, Any]]:
    """Filter tasks by status keyword (``all`` / omitted = no filter)."""
    if status_filter is None or status_filter == "all":
        return list(tasks)
    if status_filter == "pending":
        return [
            task
            for task in tasks
            if isinstance(task, dict) and _task_status(task) != DONE_STATUS
        ]
    if status_filter == "done":
        return [
            task
            for task in tasks
            if isinstance(task, dict) and _task_status(task) == DONE_STATUS
        ]
    return [
        task
        for task in tasks
        if isinstance(task, dict) and _task_status(task) == status_filter
    ]


def _sorted_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [task for task in tasks if isinstance(task, dict)]
    filtered.sort(key=_numeric_id)
    return filtered


def task_to_next_result(task: dict[str, Any], *, tag: str) -> NextTaskResult:
    """Convert a stored task record into ``NextTaskResult``."""
    summary = _summary_task_dict(task)
    complexity_raw = summary.get("complexity")
    complexity: int | None
    if complexity_raw is None:
        complexity = None
    else:
        try:
            complexity = int(complexity_raw)
        except (TypeError, ValueError) as exc:
            raise TasksCliError(
                f"invalid complexity value in task record: {complexity_raw!r}"
            ) from exc
    return NextTaskResult(
        task_id=str(summary["id"]),
        title=str(summary["title"]),
        status=str(summary["status"]) if summary.get("status") is not None else None,
        priority=str(summary["priority"]) if summary.get("priority") is not None else None,
        complexity=complexity,
        tag=tag,
        description=(
            str(summary["description"])
            if summary.get("description") is not None
            else None
        ),
    )


def _task_show_detail(task: dict[str, Any]) -> TaskShowDetail:
    summary = _summary_task_dict(task)
    complexity_raw = task.get("complexity")
    complexity: int | None
    if complexity_raw is None:
        complexity = None
    else:
        try:
            complexity = int(complexity_raw)
        except (TypeError, ValueError) as exc:
            raise TasksCliError(
                f"invalid complexity value in task record: {complexity_raw!r}"
            ) from exc

    description = task.get("description")
    details = task.get("details")
    test_strategy = task.get("testStrategy")
    status = task.get("status")
    priority = task.get("priority")

    return TaskShowDetail(
        task_id=str(summary["id"]),
        title=str(summary["title"]),
        description=str(description) if description is not None else None,
        details=str(details) if details is not None else None,
        test_strategy=str(test_strategy) if test_strategy is not None else None,
        priority=str(priority) if priority is not None else None,
        dependencies=tuple(_dependencies(task)),
        status=str(status) if status is not None else None,
        complexity=complexity,
    )


def _show_task_record(task: dict[str, Any]) -> dict[str, Any]:
    """Return the stored task dict with string id for JSON consumers."""
    payload = dict(task)
    payload["id"] = str(_numeric_id(task))
    deps = payload.get("dependencies")
    if isinstance(deps, list):
        payload["dependencies"] = [str(dep) for dep in deps]
    return payload


def list_tasks(
    project_root: Path,
    *,
    tag: str | None = None,
    status_filter: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return tasks for a tag, optionally filtered by status, sorted by numeric id."""
    store = TaskStore(project_root.resolve())
    active_tag, tasks = _load_tag_tasks(store, tag)
    filtered = _filter_tasks_by_status(tasks, status_filter)
    return active_tag, _sorted_tasks(filtered)


def list_pending_tasks(
    project_root: Path,
    *,
    tag: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return non-done tasks for a tag, sorted by numeric id."""
    return list_tasks(project_root, tag=tag, status_filter="pending")


def list_pending_task_results(
    project_root: Path,
    *,
    tag: str | None = None,
) -> list[NextTaskResult]:
    """Return pending tasks as ``NextTaskResult`` records for cyclopsctl hooks."""
    active_tag, tasks = list_pending_tasks(project_root, tag=tag)
    return [task_to_next_result(task, tag=active_tag) for task in tasks]


def get_next_task(
    project_root: Path,
    *,
    tag: str | None = None,
) -> NextTaskLookup:
    """Return the lowest pending task whose dependencies are all done."""
    store = TaskStore(project_root.resolve())
    active_tag, tasks = _load_tag_tasks(store, tag)
    indexed = _tasks_by_id(tasks)
    eligible = [
        task
        for task in tasks
        if isinstance(task, dict)
        and _task_status(task) == "pending"
        and _dependencies_satisfied(task, tasks_by_id=indexed)
    ]
    if not eligible:
        return NextTaskLookup.empty(tag=active_tag)
    selected = min(eligible, key=_numeric_id)
    return NextTaskLookup.from_task(
        task_to_next_result(selected, tag=active_tag),
        tag=active_tag,
    )


def get_task_show_detail(
    project_root: Path,
    task_id: str,
    *,
    tag: str | None = None,
) -> TaskShowDetail:
    """Return full task detail for handover sync."""
    store = TaskStore(project_root.resolve())
    active_tag, tasks = _load_tag_tasks(store, tag)
    task = _find_task(tasks, task_id)
    if task is None:
        raise TasksCliError(f"task not found: {task_id}")
    return _task_show_detail(task)


def get_task_by_id(
    project_root: Path,
    task_id: str,
    *,
    tag: str | None = None,
) -> NextTaskLookup:
    """Return a summary lookup for one task id."""
    store = TaskStore(project_root.resolve())
    active_tag, tasks = _load_tag_tasks(store, tag)
    task = _find_task(tasks, task_id)
    if task is None:
        return NextTaskLookup.empty(tag=active_tag)
    return NextTaskLookup.from_task(
        task_to_next_result(task, tag=active_tag),
        tag=active_tag,
    )


def set_task_status(
    project_root: Path,
    task_id: str,
    status: str,
    *,
    tag: str | None = None,
) -> None:
    """Update a task status and ``updatedAt`` timestamp."""
    normalized_status = status.strip()
    if normalized_status not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        raise TasksCliError(
            f"invalid status {status!r}; allowed values: {allowed}"
        )

    store = TaskStore(project_root.resolve())
    active_tag, tasks = _load_tag_tasks(store, tag)
    task = _find_task(tasks, task_id)
    if task is None:
        raise TasksCliError(f"task not found: {task_id}")

    updated = dict(task)
    updated["status"] = normalized_status
    updated["updatedAt"] = _utc_timestamp()

    replaced: list[dict[str, Any]] = []
    normalized_id = str(task_id).strip()
    for item in tasks:
        if str(_numeric_id(item)) == normalized_id:
            replaced.append(updated)
        else:
            replaced.append(item)

    try:
        store.save_tag_tasks(replaced, tag=active_tag, merge=True)
    except (TaskStoreCorruptError, TaskStoreValidationError) as exc:
        raise TasksCliError(str(exc)) from exc


def format_list_json(
    tasks: list[dict[str, Any]],
    *,
    tag: str,
    status_filter: str | None = None,
) -> str:
    payload = {
        "tasks": [_summary_task_dict(task) for task in tasks],
        "tag": tag,
        "filter": _list_filter_label(status_filter),
    }
    return json.dumps(payload, indent=2)


def format_next_json(lookup: NextTaskLookup) -> str:
    payload: dict[str, Any] = {
        "found": lookup.found,
        "tag": lookup.tag,
    }
    if lookup.found and lookup.task is not None:
        task = lookup.task
        payload["task"] = {
            "id": task.task_id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "complexity": task.complexity,
            "description": task.description,
        }
    else:
        payload["task"] = None
    return json.dumps(payload, indent=2)


def format_show_json(detail: TaskShowDetail, *, task_record: dict[str, Any]) -> str:
    payload = {
        "found": True,
        "task": _show_task_record(task_record),
    }
    return json.dumps(payload, indent=2)


def _list_filter_label(status_filter: str | None) -> str:
    if status_filter is None or status_filter == "all":
        return "all"
    return status_filter


def format_list_table(
    tasks: list[dict[str, Any]],
    *,
    tag: str,
    status_filter: str | None = None,
) -> Table:
    """Build a Rich table of tasks for terminal output."""
    label = _list_filter_label(status_filter)
    table = Table(
        title=f"Tasks ({tag}) — {label}",
        show_header=True,
        show_lines=False,
    )
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Complexity", justify="right", no_wrap=True)
    table.add_column("Dependencies", no_wrap=True)

    if not tasks:
        table.add_row("—", "(none)", "—", "—", "—")
        return table

    for task in tasks:
        task_id = str(_numeric_id(task))
        title = str(task.get("title", ""))
        status = _task_status(task)
        complexity_raw = task.get("complexity")
        complexity = str(complexity_raw) if complexity_raw is not None else "—"
        deps = _dependencies(task)
        deps_display = ", ".join(deps) if deps else "—"
        table.add_row(task_id, title, status, complexity, deps_display)
    return table


def format_list_plain(
    tasks: list[dict[str, Any]],
    *,
    tag: str,
    status_filter: str | None = None,
) -> str:
    """Render task list as plain text (fixed-width table without Rich)."""
    label = _list_filter_label(status_filter)
    header = f"Tasks ({tag}) — {label}"
    if not tasks:
        return f"{header}\n  (none)"

    id_w = max(len("ID"), max(len(str(_numeric_id(task))) for task in tasks))
    status_w = max(
        len("Status"),
        max(len(_task_status(task)) for task in tasks),
    )
    lines = [
        header,
        f"{'ID':>{id_w}}  {'Title':<24}  {'Status':<{status_w}}  {'Cx':>2}  Dependencies",
        f"{'-' * id_w}  {'-' * 24}  {'-' * status_w}  {'--':>2}  {'-' * 12}",
    ]
    for task in tasks:
        task_id = str(_numeric_id(task))
        title = str(task.get("title", ""))[:24]
        status = _task_status(task)
        complexity_raw = task.get("complexity")
        complexity = str(complexity_raw) if complexity_raw is not None else "—"
        deps = _dependencies(task)
        deps_display = ", ".join(deps) if deps else "—"
        lines.append(
            f"{task_id:>{id_w}}  {title:<24}  {status:<{status_w}}  "
            f"{complexity:>2}  {deps_display}"
        )
    return "\n".join(lines)


def format_next_plain(lookup: NextTaskLookup) -> str:
    if not lookup.found or lookup.task is None:
        tag = lookup.tag or "master"
        return f"No eligible next task for tag {tag!r}."
    task = lookup.task
    return f"Next task: {task.task_id} — {task.title} [{task.status}]"


def format_show_plain(detail: TaskShowDetail) -> str:
    lines = [
        f"Task {detail.task_id}: {detail.title}",
        f"Status: {detail.status}",
        f"Priority: {detail.priority}",
        f"Complexity: {detail.complexity}",
        f"Dependencies: {', '.join(detail.dependencies) if detail.dependencies else '(none)'}",
    ]
    if detail.description:
        lines.append(f"Description: {detail.description}")
    if detail.details:
        lines.append(f"Details: {detail.details}")
    if detail.test_strategy:
        lines.append(f"Test strategy: {detail.test_strategy}")
    return "\n".join(lines)


def _emit_output(text: str) -> None:
    print(text)


def _emit_error(message: str) -> None:
    print(message, file=sys.stderr)


def run_list(args: argparse.Namespace) -> int:
    status_filter: str | None = getattr(args, "status_filter", None)
    if status_filter is not None and status_filter not in LIST_STATUS_FILTERS:
        allowed = ", ".join(sorted(LIST_STATUS_FILTERS))
        _emit_error(
            f"invalid status filter {status_filter!r}; allowed values: {allowed}"
        )
        return 1

    try:
        project_root = resolve_project_root(args.project_root)
        tag, tasks = list_tasks(
            project_root,
            tag=args.tag,
            status_filter=status_filter,
        )
    except TasksCliError as exc:
        _emit_error(str(exc))
        return exc.exit_code
    except TaskStoreError as exc:
        _emit_error(str(exc))
        return 1

    if args.format == "json":
        _emit_output(format_list_json(tasks, tag=tag, status_filter=status_filter))
    elif getattr(args, "plain_table", False):
        _emit_output(format_list_plain(tasks, tag=tag, status_filter=status_filter))
    else:
        Console().print(format_list_table(tasks, tag=tag, status_filter=status_filter))
    return 0


def run_show(args: argparse.Namespace) -> int:
    try:
        project_root = resolve_project_root(args.project_root)
        store = TaskStore(project_root)
        active_tag, tasks = _load_tag_tasks(store, args.tag)
        task = _find_task(tasks, args.task_id)
        if task is None:
            raise TasksCliError(f"task not found: {args.task_id}")
        detail = _task_show_detail(task)
    except TasksCliError as exc:
        _emit_error(str(exc))
        return exc.exit_code
    except TaskStoreError as exc:
        _emit_error(str(exc))
        return 1

    if args.format == "json":
        _emit_output(format_show_json(detail, task_record=task))
    else:
        _emit_output(format_show_plain(detail))
    return 0


def run_next(args: argparse.Namespace) -> int:
    try:
        project_root = resolve_project_root(args.project_root)
        lookup = get_next_task(project_root, tag=args.tag)
    except TasksCliError as exc:
        _emit_error(str(exc))
        return exc.exit_code
    except TaskStoreError as exc:
        _emit_error(str(exc))
        return 1

    if args.format == "json":
        _emit_output(format_next_json(lookup))
    else:
        _emit_output(format_next_plain(lookup))
    return 0


def run_set_status(args: argparse.Namespace) -> int:
    try:
        project_root = resolve_project_root(args.project_root)
        set_task_status(
            project_root,
            args.task_id,
            args.status,
            tag=args.tag,
        )
    except TasksCliError as exc:
        _emit_error(str(exc))
        return exc.exit_code
    except TaskStoreError as exc:
        _emit_error(str(exc))
        return 1

    if args.format == "json":
        payload = {
            "updated": True,
            "id": str(args.task_id),
            "status": args.status.strip(),
        }
        _emit_output(json.dumps(payload, indent=2))
    else:
        _emit_output(
            f"Updated task {args.task_id} status to {args.status.strip()!r}."
        )
    return 0


def add_tasks_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``cyclopsctl tasks`` command group."""
    tasks_parser = subparsers.add_parser(
        "tasks",
        help="Native task queue CRUD (list [filter], show, next, set-status)",
    )
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        help="Project repository root (default: current working directory)",
    )
    shared.add_argument(
        "--tag",
        help="Task tag (default: current tag from native state.json)",
    )
    shared.add_argument(
        "--format",
        choices=["json", "plain"],
        default="plain",
        help="Output format (default: plain)",
    )

    list_parser = tasks_subparsers.add_parser(
        "list",
        parents=[shared],
        help="List tasks (table by default; optional status filter)",
    )
    list_parser.add_argument(
        "status_filter",
        nargs="?",
        default=None,
        metavar="FILTER",
        help=(
            "Optional filter: all (default), pending (non-done), done, or an exact "
            "status (in-progress, review, cancelled, blocked, deferred)"
        ),
    )
    list_parser.add_argument(
        "--plain-table",
        action="store_true",
        help="Use fixed-width text table instead of Rich (plain format only)",
    )
    list_parser.set_defaults(tasks_handler=run_list)

    show_parser = tasks_subparsers.add_parser(
        "show",
        parents=[shared],
        help="Show one task with full fields for handover sync",
    )
    show_parser.add_argument(
        "task_id",
        help="Parent task id",
    )
    show_parser.set_defaults(tasks_handler=run_show)

    next_parser = tasks_subparsers.add_parser(
        "next",
        parents=[shared],
        help="Return the lowest pending task with satisfied dependencies",
    )
    next_parser.set_defaults(tasks_handler=run_next)

    set_status_parser = tasks_subparsers.add_parser(
        "set-status",
        parents=[shared],
        help="Update a task status",
    )
    set_status_parser.add_argument(
        "--id",
        dest="task_id",
        required=True,
        help="Parent task id",
    )
    set_status_parser.add_argument(
        "--status",
        required=True,
        help="New status (pending, in-progress, done, review, cancelled, blocked, deferred)",
    )
    set_status_parser.set_defaults(tasks_handler=run_set_status)


def run_tasks_command(args: argparse.Namespace) -> int:
    """Dispatch a parsed ``cyclopsctl tasks`` subcommand."""
    handler = getattr(args, "tasks_handler", None)
    if handler is None:
        _emit_error("missing tasks subcommand")
        return 2
    return int(handler(args))
