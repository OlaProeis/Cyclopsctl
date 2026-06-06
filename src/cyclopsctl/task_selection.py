"""Configurable cycle task selection (task 16)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cyclopsctl.config import (
    DEFAULT_TASK_SOURCE,
    CyclopsctlConfig,
    TaskSource,
    normalize_task_source,
)
from cyclopsctl.prompt import snapshot_handover
from cyclopsctl.tasks.cli import (
    get_next_task,
    get_task_by_id,
    list_pending_task_results,
)
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult

ListPendingTasksFn = Callable[..., list[NextTaskResult]]
GetTaskByIdFn = Callable[..., NextTaskLookup]
GetNextTaskFn = Callable[..., NextTaskLookup]

# Sentinel written by the update agent when the task queue is empty.
QUEUE_COMPLETE_TASK_ID = 0


class TaskSelectionError(RuntimeError):
    """Task selection or validation failed."""


def format_selection_mismatch_message(
    *,
    selected_task_id: int,
    backend_next_id: int,
    task_source: TaskSource,
    project_root: Path,
) -> str:
    """Build a warning when the selected task differs from backend next."""
    return (
        f"Selected task ID {selected_task_id} does not match "
        f"backend next task ID {backend_next_id} "
        f"(task-source: {task_source}; project: {project_root})"
    )


def _task_to_lookup(task: NextTaskResult, *, tag: str | None) -> NextTaskLookup:
    return NextTaskLookup.from_task(task, tag=tag)


def _lowest_pending_task(
    tasks: list[NextTaskResult],
    *,
    tag: str | None,
    exclude_task_ids: set[int] | None = None,
) -> NextTaskLookup:
    exclude = exclude_task_ids or set()
    eligible = [task for task in tasks if task.numeric_id not in exclude]
    if not eligible:
        return NextTaskLookup.empty(tag=tag)
    selected = min(eligible, key=lambda task: task.numeric_id)
    return _task_to_lookup(selected, tag=tag)


def handover_selection_fallback_reason(
    config: CyclopsctlConfig,
    *,
    tag: str | None,
    allow_missing_handover: bool,
    get_task_by_id_fn: GetTaskByIdFn,
) -> str | None:
    """
    Return why handover mode should defer to backend next.

    Covers missing handover, queue-complete sentinel (Task ID 0), unknown ids,
    and stale handovers pointing at non-pending tasks.
    """
    snap = snapshot_handover(
        config.current_handover,
        allow_missing=allow_missing_handover,
    )
    if snap.missing:
        return "handover file missing"
    if snap.task_id is None:
        return "handover missing Task ID marker"
    if snap.task_id <= QUEUE_COMPLETE_TASK_ID:
        return (
            f"handover Task ID {snap.task_id} is not an active task "
            "(queue-complete sentinel)"
        )

    lookup = get_task_by_id_fn(
        config.project_root,
        snap.task_id,
        tag=tag,
    )
    if not lookup.found or lookup.task is None:
        return f"handover Task ID {snap.task_id} was not found in the task queue"
    if lookup.task.status != "pending":
        return (
            f"handover Task ID {snap.task_id} is not pending "
            f"(status: {lookup.task.status!r})"
        )
    return None


def _resolve_handover_task(
    config: CyclopsctlConfig,
    *,
    tag: str | None,
    allow_missing_handover: bool,
    get_task_by_id_fn: GetTaskByIdFn,
    get_next_task_fn: GetNextTaskFn,
    list_pending_tasks_fn: ListPendingTasksFn,
    exclude_task_ids: set[int] | None = None,
) -> NextTaskLookup:
    exclude = exclude_task_ids or set()
    fallback_reason = handover_selection_fallback_reason(
        config,
        tag=tag,
        allow_missing_handover=allow_missing_handover,
        get_task_by_id_fn=get_task_by_id_fn,
    )
    if fallback_reason is None:
        snap = snapshot_handover(
            config.current_handover,
            allow_missing=allow_missing_handover,
        )
        assert snap.task_id is not None
        if snap.task_id not in exclude:
            lookup = get_task_by_id_fn(
                config.project_root,
                snap.task_id,
                tag=tag,
            )
            if lookup.found and lookup.task is not None:
                return lookup

    pending_lookup = _resolve_sequential_task(
        config.project_root,
        tag=tag,
        list_pending_tasks_fn=list_pending_tasks_fn,
        exclude_task_ids=exclude,
    )
    if pending_lookup.found:
        return pending_lookup
    return get_next_task_fn(config.project_root, tag=tag)


def _resolve_sequential_task(
    project_root: Path,
    *,
    tag: str | None,
    list_pending_tasks_fn: ListPendingTasksFn,
    exclude_task_ids: set[int] | None = None,
) -> NextTaskLookup:
    pending = list_pending_tasks_fn(project_root, tag=tag)
    return _lowest_pending_task(
        pending,
        tag=tag,
        exclude_task_ids=exclude_task_ids,
    )


def _resolve_pinned_task(
    config: CyclopsctlConfig,
    *,
    tag: str | None,
    get_task_by_id_fn: GetTaskByIdFn,
) -> NextTaskLookup:
    task_id = config.pinned_task_id
    assert task_id is not None

    lookup = get_task_by_id_fn(config.project_root, task_id, tag=tag)
    if not lookup.found or lookup.task is None:
        raise TaskSelectionError(f"Pinned task ID {task_id} was not found in the task queue")

    task = lookup.task
    if task.status != "pending":
        raise TaskSelectionError(
            f"Pinned task ID {task_id} is not pending (status: {task.status!r})"
        )
    return lookup


@dataclass
class TaskSelectionState:
    """Tracks one-shot consumption of ``--task-id``."""

    pinned_task_id: int | None
    pin_consumed: bool = False


def resolve_cycle_task(
    config: CyclopsctlConfig,
    *,
    allow_missing_handover: bool = False,
    selection_state: TaskSelectionState | None = None,
    get_next_task_fn: GetNextTaskFn | None = None,
    list_pending_tasks_fn: ListPendingTasksFn | None = None,
    get_task_by_id_fn: GetTaskByIdFn | None = None,
    exclude_task_ids: set[int] | None = None,
) -> NextTaskLookup:
    """
    Select the task that drives one cyclopsctl cycle.

    Honors ``--task-id`` for the first unresolved cycle, then ``task_source``.
    """
    resolve_next = get_next_task_fn or get_next_task
    resolve_pending = list_pending_tasks_fn or list_pending_task_results
    resolve_show = get_task_by_id_fn or get_task_by_id
    tag = config.tag
    state = selection_state or TaskSelectionState(pinned_task_id=config.pinned_task_id)

    if state.pinned_task_id is not None and not state.pin_consumed:
        state.pin_consumed = True
        return _resolve_pinned_task(config, tag=tag, get_task_by_id_fn=resolve_show)

    if config.task_source == "sequential":
        return _resolve_sequential_task(
            config.project_root,
            tag=tag,
            list_pending_tasks_fn=resolve_pending,
            exclude_task_ids=exclude_task_ids,
        )

    return _resolve_handover_task(
        config,
        tag=tag,
        allow_missing_handover=allow_missing_handover,
        get_task_by_id_fn=resolve_show,
        get_next_task_fn=resolve_next,
        list_pending_tasks_fn=resolve_pending,
        exclude_task_ids=exclude_task_ids,
    )


def build_cycle_task_resolver(
    config: CyclopsctlConfig,
    *,
    get_next_task_fn: GetNextTaskFn | None = None,
    list_pending_tasks_fn: ListPendingTasksFn | None = None,
    get_task_by_id_fn: GetTaskByIdFn | None = None,
    selection_state: TaskSelectionState | None = None,
) -> tuple[GetNextTaskFn, TaskSelectionState]:
    """
    Build a callable compatible with the loop's ``get_next_task_fn`` hook.

    The returned resolver accepts ``(project_root, tag=...)`` for test compatibility
    but reads task-source settings from ``config``. ``allow_missing_handover`` is
    enabled only on the first invocation (cycle 1 bootstrap).
    """
    state = selection_state or TaskSelectionState(pinned_task_id=config.pinned_task_id)
    call_count = {"n": 0}

    def resolve(
        _project_root: Path,
        *,
        tag: str | None = None,
        exclude_task_ids: set[int] | None = None,
    ) -> NextTaskLookup:
        call_count["n"] += 1
        effective_tag = tag if tag is not None else config.tag
        return resolve_cycle_task(
            config,
            allow_missing_handover=call_count["n"] == 1,
            selection_state=state,
            get_next_task_fn=get_next_task_fn,
            list_pending_tasks_fn=list_pending_tasks_fn,
            get_task_by_id_fn=get_task_by_id_fn,
            exclude_task_ids=exclude_task_ids,
        )

    return resolve, state


__all__ = [
    "GetNextTaskFn",
    "GetTaskByIdFn",
    "ListPendingTasksFn",
    "QUEUE_COMPLETE_TASK_ID",
    "TaskSelectionError",
    "TaskSelectionState",
    "build_cycle_task_resolver",
    "format_selection_mismatch_message",
    "handover_selection_fallback_reason",
    "resolve_cycle_task",
]
