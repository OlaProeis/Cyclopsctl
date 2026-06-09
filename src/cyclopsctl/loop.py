"""Main implement → update cycle orchestration (task 8)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pathlib import Path

from cyclopsctl.config import ConfigError, CyclopsctlConfig
from cyclopsctl.alignment import format_alignment_mismatch_message, verify_handover_alignment
from cyclopsctl.history import (
    StartupResolution,
    build_history_from_run,
    format_history_mismatch_message,
    load_completed_task_ids,
    merge_completed_task_ids,
    resolve_startup,
    write_history,
)
from cyclopsctl.prompt import (
    build_fallback_implementation_prompt,
    compose_agent_prompt,
    snapshot_handover,
)
from cyclopsctl.interrupt import RunInterruptController, RunInterruptedError
from cyclopsctl.logging import CycleLogRecord, CycleLogger, DEFAULT_CYCLE_LOGGER
from cyclopsctl.routing import ModelRouter, RoutingDecision
from cyclopsctl.session import CycleSession
from cyclopsctl.tui import (
    TaskQueueSnapshot,
    activity_callback_for_logger,
    build_task_queue_snapshot,
    plan_callback_for_logger,
)
from cyclopsctl.tasks.cli import (
    get_next_task,
    get_task_by_id,
    list_pending_task_results,
)
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.task_selection import (
    GetTaskByIdFn,
    ListPendingTasksFn,
    build_cycle_task_resolver,
    format_selection_mismatch_message,
    handover_selection_fallback_reason,
)
from cyclopsctl.runner import (
    AgentRunError,
    SendRunResult,
    retry_delay_seconds,
    should_retry_transient_failure,
    should_retry_with_composer_after_opus_failure,
)
from cyclopsctl.sdk_bridge import active_managed_bridge, recover_managed_bridge
from cyclopsctl.state import RunStateStatus, RunStateTracker
from cyclopsctl.git_summary import capture_cycle_git_diff_summary, capture_git_head
from cyclopsctl.transcript_export import write_transcript_sidecar
from cyclopsctl.verify import (
    capture_pre_update_snapshot,
    guard_handover_files_after_implementation,
    read_post_update_snapshot,
    verify_handover_advanced,
)

GetNextTaskFn = Callable[..., NextTaskLookup]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class SkippedTask:
    """Parent task skipped during resume because it was already verified."""

    task_id: int
    task_title: str


@dataclass(frozen=True)
class CycleOutcome:
    """Summary of one verified cyclopsctl cycle."""

    cycle_number: int
    task_id: int
    task_title: str
    model_id: str
    agent_id: str
    impl_run_id: str
    update_run_id: str
    verification_result: str
    duration_seconds: float
    git_diff_summary: str | None = None


@dataclass
class RunLoopResult:
    """Result after completing the configured number of verified cycles."""

    completed_cycles: int
    outcomes: list[CycleOutcome] = field(default_factory=list)
    skipped_tasks: list[SkippedTask] = field(default_factory=list)
    empty_queue: bool = False
    empty_queue_tag: str | None = None
    dry_run: bool = False


def _resolve_router(
    config: CyclopsctlConfig,
    *,
    router: ModelRouter | None,
    api_key: str | None,
) -> ModelRouter:
    if router is not None:
        return router
    return ModelRouter.from_paths(
        complexity_report_path=config.complexity_report,
        default_model=config.default_model,
        routing_config=config.routing,
        api_key=api_key,
    )


def _implementation_prompt(
    config: CyclopsctlConfig,
    *,
    cycle_index: int,
    use_first_prompt_for_cycle_one: bool,
    selected_task: NextTaskLookup,
    handover_fallback_reason: str | None,
) -> str:
    if cycle_index == 0 and use_first_prompt_for_cycle_one:
        return config.first_prompt_text()
    if handover_fallback_reason is not None:
        assert selected_task.task is not None
        task = selected_task.task
        return build_fallback_implementation_prompt(
            task.numeric_id,
            title=task.title,
            description=task.description,
        )
    return config.current_handover_text()


def _is_bootstrap_cycle_one(
    cycle_index: int,
    *,
    use_first_prompt_for_cycle_one: bool,
) -> bool:
    return cycle_index == 0 and use_first_prompt_for_cycle_one


def _agent_prompt(
    config: CyclopsctlConfig,
    body: str,
    *,
    attach_ai_context: bool = True,
) -> str:
    composed, _attachment = compose_agent_prompt(
        body,
        config.ai_context,
        require_ai_context=config.require_ai_context,
        max_chars=config.ai_context_max_chars,
        attach_ai_context=attach_ai_context,
    )
    return composed


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _capture_queue_snapshot(
    config: CyclopsctlConfig,
    *,
    list_pending_fn: ListPendingTasksFn,
    current_task_id: int,
    session_completed_ids: list[int],
) -> TaskQueueSnapshot:
    """Load pending tasks from the backend and build a queue strip snapshot."""
    try:
        pending_tasks = list_pending_fn(config.project_root, tag=config.tag)
    except Exception:
        pending_tasks = []
    return build_task_queue_snapshot(
        pending_tasks,
        current_task_id=current_task_id,
        completed_ids=session_completed_ids,
    )


def _raise_if_interrupted(
    interrupt: RunInterruptController | None,
    *,
    cycle_number: int,
    phase: str,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> None:
    if interrupt is None:
        return
    interrupt.raise_if_requested(
        cycle_number=cycle_number,
        phase=phase,
        agent_id=agent_id,
        run_id=run_id,
    )


_IMPLEMENTATION_OPUS_FALLBACK_ATTEMPTS = 2


def _persist_phase_failure(
    state_tracker: RunStateTracker | None,
    *,
    config: CyclopsctlConfig,
    cycle_number: int,
    task: NextTaskResult,
    exc: AgentRunError,
    phase: str,
) -> None:
    """Record agent/run ids and a ``failed`` status when a phase errors."""
    if state_tracker is None:
        return
    state_tracker.persist(
        cycle_number=cycle_number,
        total_cycles=config.cycles,
        phase=phase,
        last_event=f"{phase} failed",
        task_id=task.numeric_id,
        task_title=task.title,
        agent_id=exc.agent_id,
        run_id=exc.run_id,
        status=RunStateStatus.FAILED,
    )


def _run_implementation_phase(
    config: CyclopsctlConfig,
    *,
    cycle_index: int,
    cycle_number: int,
    use_first_prompt_for_cycle_one: bool,
    lookup: NextTaskLookup,
    handover_fallback_reason: str | None,
    routing: RoutingDecision,
    model_router: ModelRouter,
    make_session: Callable[..., CycleSession],
    log: CycleLogger,
    interrupt: RunInterruptController | None,
) -> tuple[CycleSession, SendRunResult, RoutingDecision]:
    """
    Run the implementation prompt; retry once with Composer after Opus billing errors.
    """
    current_routing = routing
    task_id = lookup.task.numeric_id if lookup.task is not None else 0

    for attempt in range(1, _IMPLEMENTATION_OPUS_FALLBACK_ATTEMPTS + 1):
        session = make_session(model=current_routing.model)
        session.__enter__()
        impl_exc: AgentRunError | None = None
        impl: SendRunResult | None = None
        try:
            if interrupt is not None:
                interrupt.set_active_session(session)
            try:
                impl_body = _implementation_prompt(
                    config,
                    cycle_index=cycle_index,
                    use_first_prompt_for_cycle_one=use_first_prompt_for_cycle_one,
                    selected_task=lookup,
                    handover_fallback_reason=handover_fallback_reason,
                )
                impl = session.start_implementation(
                    _agent_prompt(config, impl_body, attach_ai_context=True)
                )
                _raise_if_interrupted(
                    interrupt,
                    cycle_number=cycle_number,
                    phase="implementation",
                    agent_id=impl.agent_id,
                    run_id=impl.run_id,
                )
            except AgentRunError as exc:
                impl_exc = exc
            finally:
                if interrupt is not None:
                    interrupt.set_active_session(None)
        except BaseException:
            session.__exit__(*sys.exc_info())
            raise

        if impl_exc is not None:
            session.__exit__(type(impl_exc), impl_exc, impl_exc.__traceback__)
            if (
                attempt < _IMPLEMENTATION_OPUS_FALLBACK_ATTEMPTS
                and current_routing.used_opus
                and should_retry_with_composer_after_opus_failure(impl_exc)
            ):
                log.log_warning(
                    "Opus agent run failed (billing or credits); "
                    "retrying implementation with Composer",
                    cycle_number=cycle_number,
                    attempt=attempt,
                    task_id=task_id,
                    failed_model_id=current_routing.model.id,
                    error=str(impl_exc),
                    result_detail=impl_exc.result_detail,
                    agent_id=impl_exc.agent_id,
                    run_id=impl_exc.run_id,
                )
                model_router.disable_opus_runtime()
                current_routing = model_router.route(task_id)
                continue
            raise impl_exc

        assert impl is not None
        return session, impl, current_routing

    raise RuntimeError("implementation retry loop exited without result or re-raise")


def _run_single_cycle(
    config: CyclopsctlConfig,
    *,
    cycle_index: int,
    cycle_number: int,
    use_first_prompt_for_cycle_one: bool,
    lookup: NextTaskLookup,
    raw_get_next_task: GetNextTaskFn,
    get_task_by_id_fn: GetTaskByIdFn,
    model_router: ModelRouter,
    make_session: Callable[..., CycleSession],
    log: CycleLogger,
    interrupt: RunInterruptController | None,
    state_tracker: RunStateTracker | None,
    queue_snapshot: TaskQueueSnapshot,
) -> CycleOutcome:
    """Execute one implement → update → verify cycle."""
    started_at = datetime.now(timezone.utc)

    if not lookup.found or lookup.task is None:
        raise RuntimeError("empty queue should be handled before _run_single_cycle")

    selected_task = lookup.task
    assert selected_task is not None
    routing: RoutingDecision = model_router.route(selected_task.numeric_id)

    bootstrap_cycle_one = _is_bootstrap_cycle_one(
        cycle_index,
        use_first_prompt_for_cycle_one=use_first_prompt_for_cycle_one,
    )
    handover_fallback_reason: str | None = None
    if config.task_source == "handover":
        handover_fallback_reason = handover_selection_fallback_reason(
            config,
            tag=config.tag,
            allow_missing_handover=bootstrap_cycle_one,
            get_task_by_id_fn=get_task_by_id_fn,
        )
        if handover_fallback_reason is not None:
            log.log_warning(
                "Handover unusable for task selection; using backend next",
                cycle_number=cycle_number,
                reason=handover_fallback_reason,
                selected_task_id=selected_task.numeric_id,
                handover_path=config.current_handover,
            )

    backend_next_lookup = raw_get_next_task(
        config.project_root,
        tag=config.tag,
    )
    backend_next_id = None
    if backend_next_lookup.found and backend_next_lookup.task is not None:
        backend_next_id = backend_next_lookup.task.numeric_id

    if (
        backend_next_id is not None
        and selected_task.numeric_id != backend_next_id
        and config.task_source in ("handover", "sequential")
    ):
        log.log_warning(
            format_selection_mismatch_message(
                selected_task_id=selected_task.numeric_id,
                backend_next_id=backend_next_id,
                task_source=config.task_source,
                project_root=config.project_root,
            ),
            cycle_number=cycle_number,
            selected_task_id=selected_task.numeric_id,
            backend_next_id=backend_next_id,
            task_source=config.task_source,
            project_root=config.project_root,
        )

    alignment_expected_id = selected_task.numeric_id
    if config.task_source == "sequential":
        alignment_expected_source = "lowest pending"
    else:
        alignment_expected_source = "selected handover"

    handover_relaxed = bootstrap_cycle_one or handover_fallback_reason is not None
    alignment = verify_handover_alignment(
        config.current_handover,
        alignment_expected_id,
        project_root=config.project_root,
        strict=config.strict_handover,
        skip_when_handover_missing=handover_relaxed,
        expected_source=alignment_expected_source,
        force_skip_reason=(
            f"handover unusable; deferred to backend next ({handover_fallback_reason})"
            if handover_fallback_reason
            else None
        ),
    )
    if alignment.skipped_reason:
        log.log_warning(
            "Skipping handover alignment check",
            cycle_number=cycle_number,
            reason=alignment.skipped_reason,
            handover_path=alignment.handover_path,
            next_task_id=alignment.next_task_id,
        )
    elif alignment.checked and alignment.aligned is False:
        log.log_warning(
            format_alignment_mismatch_message(alignment)
            + " (use --strict-handover to fail fast)",
            cycle_number=cycle_number,
            handover_task_id=alignment.handover_task_id,
            next_task_id=alignment.next_task_id,
            handover_path=alignment.handover_path,
            project_root=alignment.project_root,
        )

    handover_at_start = capture_pre_update_snapshot(
        config.current_handover,
        allow_missing=handover_relaxed,
    )
    update_handover_at_start = capture_pre_update_snapshot(
        config.update_handover,
        allow_missing=False,
    )
    log.log_cycle_start(
        cycle_number=cycle_number,
        total_cycles=config.cycles,
        next_task_id=selected_task.numeric_id,
        next_task_title=selected_task.title,
        model_id=routing.model.id,
        handover_task_id=handover_at_start.snapshot.task_id,
        timestamp=started_at,
        queue_snapshot=queue_snapshot,
    )
    git_start_head: str | None = None
    if config.git_summary:
        git_start_head = capture_git_head(config.project_root)

    if config.dry_run:
        aligned = None if alignment.skipped_reason else alignment.aligned
        log.log_dry_run_plan(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            next_task_id=selected_task.numeric_id,
            next_task_title=selected_task.title,
            model_id=routing.model.id,
            handover_task_id=handover_at_start.snapshot.task_id,
            aligned=aligned,
            timestamp=started_at,
        )
        completed_at = datetime.now(timezone.utc)
        duration_seconds = max(0.0, (completed_at - started_at).total_seconds())
        git_diff_summary = None
        if config.git_summary:
            git_diff_summary = capture_cycle_git_diff_summary(
                config.project_root,
                git_start_head,
            )
        record = CycleLogRecord(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            next_task_id=selected_task.numeric_id,
            next_task_title=selected_task.title,
            model_id=routing.model.id,
            agent_id="",
            impl_run_id="",
            impl_status="dry-run",
            update_run_id="",
            update_status="dry-run",
            handover_before_task_id=handover_at_start.snapshot.task_id,
            handover_after_task_id=handover_at_start.snapshot.task_id,
            handover_before_hash=handover_at_start.snapshot.content_hash,
            handover_after_hash=handover_at_start.snapshot.content_hash,
            verification_result="dry-run",
            started_at=started_at,
            completed_at=completed_at,
            git_diff_summary=git_diff_summary,
        )
        log.log_cycle(record)
        return CycleOutcome(
            cycle_number=cycle_number,
            task_id=selected_task.numeric_id,
            task_title=selected_task.title,
            model_id=routing.model.id,
            agent_id="",
            impl_run_id="",
            update_run_id="",
            verification_result="dry-run",
            duration_seconds=duration_seconds,
            git_diff_summary=git_diff_summary,
        )

    if state_tracker is not None:
        state_tracker.persist(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            phase="implementation",
            last_event="cycle started",
            task_id=selected_task.numeric_id,
            task_title=selected_task.title,
        )

    try:
        session, impl, routing = _run_implementation_phase(
            config,
            cycle_index=cycle_index,
            cycle_number=cycle_number,
            use_first_prompt_for_cycle_one=use_first_prompt_for_cycle_one,
            lookup=lookup,
            handover_fallback_reason=handover_fallback_reason,
            routing=routing,
            model_router=model_router,
            make_session=make_session,
            log=log,
            interrupt=interrupt,
        )
    except AgentRunError as exc:
        _persist_phase_failure(
            state_tracker,
            config=config,
            cycle_number=cycle_number,
            task=selected_task,
            exc=exc,
            phase="implementation",
        )
        raise
    try:
        guard_result = guard_handover_files_after_implementation(
            current_handover_path=config.current_handover,
            update_handover_path=config.update_handover,
            current_handover_at_start=handover_at_start,
            update_handover_at_start=update_handover_at_start,
            strict=config.strict_handover,
        )
        if guard_result.current_handover_modified or guard_result.update_handover_modified:
            log.log_warning(
                "Restored handover file(s) modified during implementation",
                cycle_number=cycle_number,
                current_handover_modified=guard_result.current_handover_modified,
                update_handover_modified=guard_result.update_handover_modified,
                current_handover_restored=guard_result.current_handover_restored,
                update_handover_restored=guard_result.update_handover_restored,
                current_handover_path=config.current_handover,
                update_handover_path=config.update_handover,
            )

        log.log_run_complete(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            phase="implementation",
            agent_id=impl.agent_id,
            run_id=impl.run_id,
            status=impl.status,
        )
        if state_tracker is not None:
            state_tracker.persist(
                cycle_number=cycle_number,
                total_cycles=config.cycles,
                phase="snapshot",
                last_event="implementation finished",
                task_id=selected_task.numeric_id,
                task_title=selected_task.title,
                agent_id=impl.agent_id,
                run_id=impl.run_id,
            )

        pre_handover = capture_pre_update_snapshot(
            config.current_handover,
            allow_missing=handover_relaxed,
        )
        log.log_handover_snapshot(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            label="before",
            path=pre_handover.snapshot.path,
            task_id=pre_handover.snapshot.task_id,
            content_hash=pre_handover.snapshot.content_hash,
            missing=pre_handover.snapshot.missing,
            captured_at=pre_handover.captured_at,
        )

        _raise_if_interrupted(
            interrupt,
            cycle_number=cycle_number,
            phase="snapshot",
            agent_id=impl.agent_id,
            run_id=impl.run_id,
        )

        if interrupt is not None:
            interrupt.set_active_session(session)
        try:
            update_body = config.update_handover_text()
            log.log_warning(
                "Sending update phase prompt",
                cycle_number=cycle_number,
                update_handover_path=config.update_handover,
                update_handover_hash_prefix=snapshot_handover(
                    config.update_handover,
                    allow_missing=False,
                ).content_hash[:12],
                update_prompt_chars=len(update_body),
            )
            update = session.run_update(
                _agent_prompt(config, update_body, attach_ai_context=False)
            )
            _raise_if_interrupted(
                interrupt,
                cycle_number=cycle_number,
                phase="update",
                agent_id=update.agent_id,
                run_id=update.run_id,
            )
        finally:
            if interrupt is not None:
                interrupt.set_active_session(None)

        log.log_run_complete(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            phase="update",
            agent_id=update.agent_id,
            run_id=update.run_id,
            status=update.status,
        )
        if state_tracker is not None:
            state_tracker.persist(
                cycle_number=cycle_number,
                total_cycles=config.cycles,
                phase="verify",
                last_event="update finished",
                task_id=selected_task.numeric_id,
                task_title=selected_task.title,
                agent_id=update.agent_id,
                run_id=update.run_id,
            )

        after = read_post_update_snapshot(config.current_handover)
        log.log_handover_snapshot(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            label="after",
            path=after.path,
            task_id=after.task_id,
            content_hash=after.content_hash,
            missing=after.missing,
        )

        verify_handover_advanced(
            pre_handover,
            after,
            project_root=config.project_root,
            tag=config.tag,
            get_next_task_fn=raw_get_next_task,
        )
        log.log_verification_result(
            cycle_number=cycle_number,
            total_cycles=config.cycles,
            result="passed",
            before_task_id=pre_handover.snapshot.task_id,
            after_task_id=after.task_id,
            before_hash=pre_handover.snapshot.content_hash,
            after_hash=after.content_hash,
        )
        if state_tracker is not None:
            state_tracker.persist(
                cycle_number=cycle_number,
                total_cycles=config.cycles,
                phase="complete",
                last_event="verification passed",
                task_id=selected_task.numeric_id,
                task_title=selected_task.title,
                agent_id=update.agent_id,
                run_id=update.run_id,
            )
    except AgentRunError as exc:
        _persist_phase_failure(
            state_tracker,
            config=config,
            cycle_number=cycle_number,
            task=selected_task,
            exc=exc,
            phase="update",
        )
        raise
    finally:
        session.__exit__(None, None, None)

    completed_at = datetime.now(timezone.utc)
    git_diff_summary = None
    if config.git_summary:
        git_diff_summary = capture_cycle_git_diff_summary(
            config.project_root,
            git_start_head,
        )
    record = CycleLogRecord(
        cycle_number=cycle_number,
        total_cycles=config.cycles,
        next_task_id=selected_task.numeric_id,
        next_task_title=selected_task.title,
        model_id=routing.model.id,
        agent_id=impl.agent_id,
        impl_run_id=impl.run_id,
        impl_status=impl.status,
        update_run_id=update.run_id,
        update_status=update.status,
        handover_before_task_id=pre_handover.snapshot.task_id,
        handover_after_task_id=after.task_id,
        handover_before_hash=pre_handover.snapshot.content_hash,
        handover_after_hash=after.content_hash,
        verification_result="passed",
        started_at=started_at,
        completed_at=completed_at,
        git_diff_summary=git_diff_summary,
    )
    log.log_cycle(record)
    if config.export_transcript_dir is not None:
        write_transcript_sidecar(
            config.export_transcript_dir,
            cycle_number=cycle_number,
            task_id=selected_task.numeric_id,
            agent_id=impl.agent_id,
            run_id=impl.run_id,
            model=routing.model.id,
            status="passed",
        )

    duration_seconds = max(0.0, (completed_at - started_at).total_seconds())

    return CycleOutcome(
        cycle_number=cycle_number,
        task_id=selected_task.numeric_id,
        task_title=selected_task.title,
        model_id=routing.model.id,
        agent_id=impl.agent_id,
        impl_run_id=impl.run_id,
        update_run_id=update.run_id,
        verification_result="passed",
        duration_seconds=duration_seconds,
        git_diff_summary=git_diff_summary,
    )


_BRIDGE_CONNECT_FAILURE_HINTS = (
    "connecterror",
    "connection refused",
    "actively refused",
    "10061",
)


def _is_bridge_connect_failure(exc: AgentRunError) -> bool:
    """Whether the failure is the local bridge refusing/dropping connections."""
    message = str(exc).lower()
    if "bridge request failed" not in message:
        return False
    return any(hint in message for hint in _BRIDGE_CONNECT_FAILURE_HINTS)


def _recover_bridge_before_retry(
    config: CyclopsctlConfig,
    *,
    exc: AgentRunError,
    log: CycleLogger,
    cycle_number: int,
) -> None:
    """Relaunch the managed bridge before a retry when it died mid-run.

    The bridge is a plain node.exe process; an orchestrated agent that kills
    node processes (port cleanup, test teardown) takes the bridge down, and
    retrying against the dead endpoint can never succeed. Relaunching makes
    the retry meaningful. No-op when there is no managed bridge (non-Windows
    or externally supplied endpoint) or when it is still healthy.
    """
    bridge = active_managed_bridge()
    if bridge is None:
        return
    bridge_alive = bridge.is_alive()
    if bridge_alive and not _is_bridge_connect_failure(exc):
        return
    log.log_warning(
        "cursor-sdk-bridge is down; relaunching before retry",
        cycle_number=cycle_number,
        bridge_alive=bridge_alive,
        error=str(exc),
    )
    try:
        recover_managed_bridge(config.project_root)
    except Exception as bridge_exc:
        log.log_warning(
            "Bridge relaunch failed; retrying cycle anyway",
            cycle_number=cycle_number,
            error=str(bridge_exc),
        )


def _run_cycle_with_retry(
    config: CyclopsctlConfig,
    *,
    cycle_index: int,
    cycle_number: int,
    use_first_prompt_for_cycle_one: bool,
    lookup: NextTaskLookup,
    raw_get_next_task: GetNextTaskFn,
    get_task_by_id_fn: GetTaskByIdFn,
    model_router: ModelRouter,
    make_session: Callable[..., CycleSession],
    log: CycleLogger,
    interrupt: RunInterruptController | None,
    state_tracker: RunStateTracker | None,
    sleep_fn: SleepFn,
    queue_snapshot: TaskQueueSnapshot,
) -> CycleOutcome:
    max_attempts = config.retry_max_attempts if config.retry_transient_enabled else 1
    for attempt in range(1, max_attempts + 1):
        try:
            return _run_single_cycle(
                config,
                cycle_index=cycle_index,
                cycle_number=cycle_number,
                use_first_prompt_for_cycle_one=use_first_prompt_for_cycle_one,
                lookup=lookup,
                raw_get_next_task=raw_get_next_task,
                get_task_by_id_fn=get_task_by_id_fn,
                model_router=model_router,
                make_session=make_session,
                log=log,
                interrupt=interrupt,
                state_tracker=state_tracker,
                queue_snapshot=queue_snapshot,
            )
        except AgentRunError as exc:
            if not should_retry_transient_failure(
                exc,
                retry_on_transient=config.retry_transient_enabled,
                failed_attempt=attempt,
                max_attempts=max_attempts,
            ):
                raise
            _recover_bridge_before_retry(
                config,
                exc=exc,
                log=log,
                cycle_number=cycle_number,
            )
            delay = retry_delay_seconds(
                config.retry_backoff_seconds,
                failed_attempt=attempt,
            )
            log.log_warning(
                "Transient agent failure; retrying cycle",
                cycle_number=cycle_number,
                attempt=attempt,
                max_attempts=max_attempts,
                retry_delay_seconds=delay,
                error=str(exc),
                agent_id=exc.agent_id,
                run_id=exc.run_id,
            )
            sleep_fn(delay)
    raise RuntimeError("retry loop exited without result or re-raise")


def _validate_first_prompt_when_required(
    config: CyclopsctlConfig,
    startup: StartupResolution,
) -> None:
    """Require first-prompt on disk only when cycle 1 will use it."""
    if not startup.use_first_prompt_for_cycle_one:
        return
    if config.first_prompt.is_file():
        return
    raise ConfigError(
        f"first-prompt not found: {config.first_prompt}. "
        "The first-prompt file is only required for greenfield bootstrap "
        "(--fresh or when current-handover-prompt.md is missing or has no "
        "# Task ID:). When the handover is ready, first-prompt is not used."
    )


def _log_startup_resolution(
    log: CycleLogger,
    resolution: StartupResolution,
    *,
    handover_path: Path,
) -> None:
    if resolution.use_first_prompt_for_cycle_one:
        log.log_warning(
            "Starting fresh bootstrap (first-prompt for cycle 1)",
            resumed=False,
            prior_bootstrap_complete=(
                resolution.history.bootstrap_complete
                if resolution.history is not None
                else False
            ),
        )
    elif resolution.resumed:
        log.log_warning(
            "Resuming cyclopsctl run from current handover (bootstrap complete)",
            resumed=True,
            history_path=str(resolution.history_path) if resolution.history_path else None,
            last_handover_task_id=(
                resolution.history.last_handover_task_id
                if resolution.history is not None
                else None
            ),
            last_run_at=(
                resolution.history.last_run_at
                if resolution.history is not None
                else None
            ),
        )
    else:
        log.log_warning(
            "Using current-handover-prompt.md for cycle 1",
            handover_path=str(handover_path),
        )

    validation = resolution.validation
    if validation is not None and validation.checked and validation.aligned is False:
        log.log_warning(
            format_history_mismatch_message(validation),
            handover_task_id=validation.handover_task_id,
            expected_handover_task_id=validation.expected_handover_task_id,
            handover_path=validation.handover_path,
            history_path=validation.history_path,
        )


def _persist_run_history_after_failure(
    config: CyclopsctlConfig,
    *,
    result: RunLoopResult,
    prior_completed_task_ids: list[int] | None,
    log: CycleLogger,
) -> None:
    """Best-effort history write so ``--resume`` knows about verified cycles.

    Called when an exception aborts the run loop after one or more cycles
    completed. Persistence failures are logged, never raised, so the original
    error keeps propagating.
    """
    if result.completed_cycles == 0 or config.dry_run:
        return
    try:
        _persist_run_history(
            config,
            completed_task_ids=[outcome.task_id for outcome in result.outcomes],
            prior_completed_task_ids=prior_completed_task_ids,
        )
    except Exception as persist_exc:
        log.log_warning(
            "Failed to persist run history after run failure",
            error=str(persist_exc),
            completed_cycles=result.completed_cycles,
        )


def _persist_run_history(
    config: CyclopsctlConfig,
    *,
    completed_task_ids: list[int],
    prior_completed_task_ids: list[int] | None = None,
) -> None:
    if config.history_file is None or not completed_task_ids:
        return
    if config.resume and prior_completed_task_ids is not None:
        persisted_ids = merge_completed_task_ids(prior_completed_task_ids, completed_task_ids)
    else:
        persisted_ids = completed_task_ids
    final_handover = snapshot_handover(config.current_handover, allow_missing=False)
    history = build_history_from_run(
        project_root=config.project_root,
        completed_task_ids=persisted_ids,
        final_handover=final_handover,
    )
    write_history(config.history_file, history)


def _resolve_runnable_task(
    config: CyclopsctlConfig,
    *,
    resolve_selected: GetNextTaskFn,
    completed_task_ids: frozenset[int],
    resume_exclude: set[int],
    log: CycleLogger,
    cycle_number: int,
) -> tuple[NextTaskLookup, list[SkippedTask]]:
    """Resolve the next task to run, skipping resume-completed ids when enabled."""
    skipped: list[SkippedTask] = []
    exclude = set(resume_exclude)

    while True:
        lookup = resolve_selected(
            config.project_root,
            tag=config.tag,
            exclude_task_ids=exclude or None,
        )
        if not lookup.found or lookup.task is None:
            return lookup, skipped

        task = lookup.task
        if not config.resume or task.numeric_id not in completed_task_ids:
            return lookup, skipped

        skipped.append(SkippedTask(task_id=task.numeric_id, task_title=task.title))
        log.log_warning(
            "Skipping already completed task (--resume)",
            cycle_number=cycle_number,
            task_id=task.numeric_id,
            task_title=task.title,
        )
        exclude.add(task.numeric_id)


def run_cycles(
    config: CyclopsctlConfig,
    *,
    api_key: str | None = None,
    get_next_task_fn: GetNextTaskFn | None = None,
    list_pending_tasks_fn: ListPendingTasksFn | None = None,
    get_task_by_id_fn: GetTaskByIdFn | None = None,
    router: ModelRouter | None = None,
    session_factory: Callable[..., CycleSession] | None = None,
    cycle_logger: CycleLogger | None = None,
    interrupt: RunInterruptController | None = None,
    sleep_fn: SleepFn | None = None,
    state_tracker: RunStateTracker | None = None,
) -> RunLoopResult:
    """
    Execute ``config.cycles`` verified implement → update cycles.

    Stops on the first ``AgentRunError``, task queue, prompt, session, or
    handover verification failure without starting the next cycle.
    """
    raw_get_next_task = get_next_task_fn or get_next_task
    raw_list_pending = list_pending_tasks_fn or list_pending_task_results
    raw_get_task_by_id = get_task_by_id_fn or get_task_by_id
    resolve_selected, _selection_state = build_cycle_task_resolver(
        config,
        get_next_task_fn=raw_get_next_task,
        list_pending_tasks_fn=raw_list_pending,
        get_task_by_id_fn=raw_get_task_by_id,
    )
    model_router = _resolve_router(config, router=router, api_key=api_key)
    log = cycle_logger or DEFAULT_CYCLE_LOGGER
    activity_callback = activity_callback_for_logger(log)
    plan_callback = plan_callback_for_logger(log)
    make_session = session_factory or (
        lambda **kwargs: CycleSession(
            project_root=config.project_root,
            api_key=api_key,
            on_activity=activity_callback,
            on_plan_update=plan_callback,
            **kwargs,
        )
    )
    sleep = sleep_fn or _default_sleep
    tracker = (
        state_tracker
        if state_tracker is not None
        else RunStateTracker(config.state_file, project_root=config.project_root)
    )

    startup = resolve_startup(
        project_root=config.project_root,
        handover_path=config.current_handover,
        history_path=config.history_file,
        fresh=config.fresh,
        strict=config.strict_handover,
    )
    _validate_first_prompt_when_required(config, startup)
    _log_startup_resolution(log, startup, handover_path=config.current_handover)

    completed_task_ids = (
        load_completed_task_ids(config.history_file) if config.resume else frozenset()
    )
    prior_completed_task_ids = (
        list(startup.history.completed_cycle_task_ids)
        if config.resume and startup.history is not None
        else None
    )
    if config.resume and completed_task_ids:
        ids = ", ".join(str(task_id) for task_id in sorted(completed_task_ids))
        log.log_warning(
            f"Task-level resume enabled; will skip completed task IDs: {ids}",
            resume=True,
            completed_task_ids=sorted(completed_task_ids),
        )

    result = RunLoopResult(completed_cycles=0, dry_run=config.dry_run)
    try:
        return _run_cycle_loop(
            config,
            result=result,
            startup=startup,
            resolve_selected=resolve_selected,
            raw_get_next_task=raw_get_next_task,
            raw_list_pending=raw_list_pending,
            get_task_by_id_fn=raw_get_task_by_id,
            model_router=model_router,
            make_session=make_session,
            log=log,
            interrupt=interrupt,
            tracker=tracker,
            sleep=sleep,
            completed_task_ids=completed_task_ids,
            prior_completed_task_ids=prior_completed_task_ids,
        )
    except RunInterruptedError as exc:
        _persist_run_history_after_failure(
            config,
            result=result,
            prior_completed_task_ids=prior_completed_task_ids,
            log=log,
        )
        exc.partial_result = result
        raise
    except BaseException:
        # A cycle failed (agent error, verification, queue, ...) after earlier
        # cycles were verified: persist those task ids so --resume can skip
        # them on the next invocation. Task status and the handover were
        # already advanced by the update phase, so this only backfills history.
        _persist_run_history_after_failure(
            config,
            result=result,
            prior_completed_task_ids=prior_completed_task_ids,
            log=log,
        )
        raise


def _run_cycle_loop(
    config: CyclopsctlConfig,
    *,
    result: RunLoopResult,
    startup: StartupResolution,
    resolve_selected: GetNextTaskFn,
    raw_get_next_task: GetNextTaskFn,
    raw_list_pending: ListPendingTasksFn,
    get_task_by_id_fn: GetTaskByIdFn,
    model_router: ModelRouter,
    make_session: Callable[..., CycleSession],
    log: CycleLogger,
    interrupt: RunInterruptController | None,
    tracker: RunStateTracker,
    sleep: SleepFn,
    completed_task_ids: frozenset[int],
    prior_completed_task_ids: list[int] | None,
) -> RunLoopResult:
    resume_exclude: set[int] = set()
    session_completed_ids: list[int] = []
    cycles_remaining = config.cycles
    cycle_index = 0
    attempt_number = 0

    while cycles_remaining > 0:
        attempt_number += 1
        cycle_number = attempt_number
        _raise_if_interrupted(interrupt, cycle_number=cycle_number, phase="idle")
        if tracker.enabled:
            tracker.persist(
                cycle_number=cycle_number,
                total_cycles=config.cycles,
                phase="idle",
                last_event="waiting to start cycle",
            )

        lookup, skipped = _resolve_runnable_task(
            config,
            resolve_selected=resolve_selected,
            completed_task_ids=completed_task_ids,
            resume_exclude=resume_exclude,
            log=log,
            cycle_number=cycle_number,
        )
        if skipped:
            result.skipped_tasks.extend(skipped)
            resume_exclude.update(task.task_id for task in skipped)

        _raise_if_interrupted(
            interrupt,
            cycle_number=cycle_number,
            phase="resolve",
        )
        if tracker.enabled:
            tracker.persist(
                cycle_number=cycle_number,
                total_cycles=config.cycles,
                phase="resolve",
                last_event="resolving next task",
            )
        if not lookup.found:
            log.log_warning(
                "Task queue empty; stopping run",
                completed_cycles=result.completed_cycles,
                requested_cycles=config.cycles,
                skipped_tasks=len(result.skipped_tasks),
                tag=lookup.tag,
            )
            result.empty_queue = True
            result.empty_queue_tag = lookup.tag
            tracker.mark_completed()
            if result.completed_cycles > 0 and not config.dry_run:
                completed_task_ids_run = [outcome.task_id for outcome in result.outcomes]
                _persist_run_history(
                    config,
                    completed_task_ids=completed_task_ids_run,
                    prior_completed_task_ids=prior_completed_task_ids,
                )
            return result

        if lookup.found and lookup.task is not None and tracker.enabled:
            tracker.persist(
                cycle_number=cycle_number,
                total_cycles=config.cycles,
                phase="resolve",
                last_event="task selected",
                task_id=lookup.task.numeric_id,
                task_title=lookup.task.title,
            )

        assert lookup.task is not None
        queue_snapshot = _capture_queue_snapshot(
            config,
            list_pending_fn=raw_list_pending,
            current_task_id=lookup.task.numeric_id,
            session_completed_ids=session_completed_ids,
        )

        outcome = _run_cycle_with_retry(
            config,
            cycle_index=cycle_index,
            cycle_number=cycle_number,
            use_first_prompt_for_cycle_one=startup.use_first_prompt_for_cycle_one
            and cycle_index == 0,
            lookup=lookup,
            raw_get_next_task=raw_get_next_task,
            get_task_by_id_fn=get_task_by_id_fn,
            model_router=model_router,
            make_session=make_session,
            log=log,
            interrupt=interrupt,
            state_tracker=tracker if tracker.enabled else None,
            sleep_fn=sleep,
            queue_snapshot=queue_snapshot,
        )
        result.outcomes.append(outcome)
        session_completed_ids.append(outcome.task_id)
        result.completed_cycles += 1
        cycles_remaining -= 1
        cycle_index += 1

    tracker.mark_completed()
    if result.completed_cycles > 0 and not config.dry_run:
        completed_task_ids_run = [outcome.task_id for outcome in result.outcomes]
        _persist_run_history(
            config,
            completed_task_ids=completed_task_ids_run,
            prior_completed_task_ids=prior_completed_task_ids,
        )
    return result
