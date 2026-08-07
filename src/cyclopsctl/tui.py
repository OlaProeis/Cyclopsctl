"""Rich live terminal dashboard for cyclopsctl runs."""

from __future__ import annotations

import sys
import textwrap
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

from cyclopsctl.doctor import render_diagnostics_rich
from cyclopsctl.interrupt import RunInterruptController
from cyclopsctl.logging import CycleLogRecord, CycleLogger
from cyclopsctl.runner import (
    ActivityCallback,
    DEFAULT_ACTIVITY_PROSE_DISPLAY_MAX_LEN,
    PlanUpdateCallback,
    is_prose_activity_line,
    merge_prose_activity,
    truncate_activity_text,
)
from cyclopsctl.tasks.types import NextTaskResult

DEFAULT_ACTIVITY_BUFFER_SIZE = 8
DEFAULT_ACTIVITY_MAX_VISUAL_LINES = 10
DEFAULT_AGENT_PLAN_MAX_VISIBLE = 12
DEFAULT_QUEUE_STRIP_UPCOMING_CAP = 5
ACTIVITY_REFRESH_INTERVAL_SECONDS = 0.25
ACTIVITY_PANEL_HORIZONTAL_PADDING = 6

# Phases where an SDK agent run is actively in progress and may go silent
# for long stretches (e.g. a slow test suite). A heartbeat reassures the
# user that cyclopsctl has not hung during these windows.
ACTIVE_RUN_PHASES = frozenset({"implementation", "update"})
HEARTBEAT_IDLE_THRESHOLD_SECONDS = 15.0
HEARTBEAT_REFRESH_PER_SECOND = 2


def format_elapsed_short(seconds: float) -> str:
    """Render a duration as a compact human string (``45s``, ``3m 20s``, ``1h 5m``)."""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


class AgentPlanItemStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


STEP_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("resolve", "Resolve next task"),
    ("implementation", "Implementation"),
    ("snapshot", "Snapshot handover"),
    ("update", "Update phase"),
    ("verify", "Verify handover"),
)


def _step_icon(status: StepStatus) -> str:
    if status is StepStatus.DONE:
        return "[green]✓[/green]"
    if status is StepStatus.RUNNING:
        return "[yellow]●[/yellow]"
    return "[dim]○[/dim]"


def _parse_agent_plan_status(raw: str) -> AgentPlanItemStatus:
    normalized = raw.strip().lower().replace("-", "_")
    for status in AgentPlanItemStatus:
        if normalized == status.value:
            return status
    return AgentPlanItemStatus.PENDING


def _agent_plan_icon(status: AgentPlanItemStatus) -> str:
    if status is AgentPlanItemStatus.COMPLETED:
        return "[green]✓[/green]"
    if status is AgentPlanItemStatus.IN_PROGRESS:
        return "[yellow]●[/yellow]"
    if status is AgentPlanItemStatus.CANCELLED:
        return "[dim strike]✗[/dim strike]"
    return "[dim]○[/dim]"


@dataclass
class AgentPlanItem:
    id: str
    content: str
    status: AgentPlanItemStatus = AgentPlanItemStatus.PENDING


@dataclass
class AgentPlanState:
    """Composer TodoWrite plan items for the current cycle."""

    items: list[AgentPlanItem] = field(default_factory=list)

    def clear(self) -> None:
        self.items.clear()

    def apply_todo_write(
        self,
        todos: Sequence[Mapping[str, Any]],
        *,
        merge: bool,
    ) -> None:
        parsed: list[AgentPlanItem] = []
        for raw in todos:
            todo_id = str(raw.get("id", "")).strip()
            if not todo_id:
                continue
            content = str(raw.get("content", "")).strip()
            status = _parse_agent_plan_status(str(raw.get("status", "pending")))
            parsed.append(AgentPlanItem(id=todo_id, content=content, status=status))

        if not merge:
            self.items = parsed
            return

        by_id = {item.id: item for item in self.items}
        order = [item.id for item in self.items]
        for item in parsed:
            if item.id in by_id:
                by_id[item.id] = item
                order.remove(item.id)
                order.append(item.id)
            else:
                by_id[item.id] = item
                order.append(item.id)
        self.items = [by_id[item_id] for item_id in order]


@dataclass(frozen=True)
class TaskQueueSnapshot:
    """Queue strip inputs captured from ``TaskBackend.list_pending()`` at cycle start."""

    completed_ids: tuple[int, ...]
    current_task_id: int
    pending_ids: tuple[int, ...]
    total_pending: int
    upcoming_cap: int = DEFAULT_QUEUE_STRIP_UPCOMING_CAP


def build_task_queue_snapshot(
    pending_tasks: Sequence[NextTaskResult],
    *,
    current_task_id: int,
    completed_ids: Sequence[int] = (),
    upcoming_cap: int = DEFAULT_QUEUE_STRIP_UPCOMING_CAP,
) -> TaskQueueSnapshot:
    """Build a queue snapshot from backend pending tasks and session context."""
    pending_ids = tuple(task.numeric_id for task in pending_tasks)
    return TaskQueueSnapshot(
        completed_ids=tuple(sorted(completed_ids)),
        current_task_id=current_task_id,
        pending_ids=pending_ids,
        total_pending=len(pending_ids),
        upcoming_cap=upcoming_cap,
    )


def _upcoming_pending_ids(
    pending_ids: Sequence[int],
    current_task_id: int,
    cap: int,
) -> list[int]:
    if cap <= 0:
        return []
    try:
        start = pending_ids.index(current_task_id) + 1
    except ValueError:
        start = 0
    return list(pending_ids[start : start + cap])


@dataclass
class TaskQueueStripState:
    """Session-local queue strip state for Rich and plain rendering."""

    completed_ids: list[int] = field(default_factory=list)
    current_task_id: int | None = None
    pending_ids: list[int] = field(default_factory=list)
    total_pending: int = 0
    upcoming_cap: int = DEFAULT_QUEUE_STRIP_UPCOMING_CAP

    def apply_snapshot(self, snapshot: TaskQueueSnapshot) -> None:
        self.completed_ids = list(snapshot.completed_ids)
        self.current_task_id = snapshot.current_task_id
        self.pending_ids = list(snapshot.pending_ids)
        self.total_pending = snapshot.total_pending
        self.upcoming_cap = snapshot.upcoming_cap

    def mark_task_completed(self, task_id: int) -> None:
        if task_id not in self.completed_ids:
            self.completed_ids.append(task_id)
            self.completed_ids.sort()
        if self.current_task_id == task_id:
            self.current_task_id = None
        if task_id in self.pending_ids:
            self.pending_ids.remove(task_id)
            self.total_pending = len(self.pending_ids)


def format_queue_strip_line(state: TaskQueueStripState) -> str:
    """Render the queue strip as a plain one-line summary."""
    if state.current_task_id is None and not state.completed_ids and not state.pending_ids:
        return ""

    parts: list[str] = ["Queue:"]
    for task_id in state.completed_ids:
        parts.append(f"[✓{task_id}]")
    if state.current_task_id is not None:
        parts.append(f"[●{state.current_task_id}]")
        upcoming = _upcoming_pending_ids(
            state.pending_ids,
            state.current_task_id,
            state.upcoming_cap,
        )
    else:
        upcoming = list(state.pending_ids[: state.upcoming_cap])
    for task_id in upcoming:
        parts.append(f"[{task_id}]")

    shown_pending = (1 if state.current_task_id is not None else 0) + len(upcoming)
    if state.total_pending > shown_pending:
        parts.append("…")
    if state.total_pending > 0:
        parts.append(f"({state.total_pending} pending)")
    return " ".join(parts)


def render_queue_strip_section(state: TaskQueueStripState) -> RenderableType | None:
    """Build the Rich queue strip, or ``None`` when there is nothing to show."""
    line = format_queue_strip_line(state)
    if not line:
        return None

    text = Text("Queue:", style="bold")
    text.append(" ")
    for task_id in state.completed_ids:
        text.append("[", style="dim")
        text.append("✓", style="green")
        text.append(f"{task_id}", style="green")
        text.append("] ", style="dim")
    if state.current_task_id is not None:
        text.append("[", style="bold")
        text.append("●", style="yellow bold")
        text.append(f"{state.current_task_id}", style="yellow bold")
        text.append("] ", style="bold")
        upcoming = _upcoming_pending_ids(
            state.pending_ids,
            state.current_task_id,
            state.upcoming_cap,
        )
    else:
        upcoming = list(state.pending_ids[: state.upcoming_cap])
    for task_id in upcoming:
        text.append(f"[{task_id}] ", style="dim")

    shown_pending = (1 if state.current_task_id is not None else 0) + len(upcoming)
    if state.total_pending > shown_pending:
        text.append("… ", style="dim")
    if state.total_pending > 0:
        text.append(f"({state.total_pending} pending)", style="dim")
    return text


@dataclass
class RunDashboardState:
    """Mutable dashboard state updated by RichCycleLogger."""

    cycle_number: int = 0
    total_cycles: int = 0
    next_task_id: int | None = None
    next_task_title: str = ""
    model_id: str = ""
    phase: str = "idle"
    agent_id: str = ""
    run_id: str = ""
    impl_status: str = ""
    update_status: str = ""
    verification_result: str = ""
    activity_lines: list[str] = field(default_factory=list)
    agent_plan: AgentPlanState = field(default_factory=AgentPlanState)
    queue_strip: TaskQueueStripState = field(default_factory=TaskQueueStripState)
    step_status: dict[str, StepStatus] = field(
        default_factory=lambda: {key: StepStatus.PENDING for key, _ in STEP_DEFINITIONS}
    )
    last_activity_monotonic: float | None = None
    phase_started_monotonic: float | None = None

    def note_activity(self, now: float) -> None:
        """Record the monotonic time of the most recent agent activity."""
        self.last_activity_monotonic = now

    def begin_active_phase(self, now: float) -> None:
        """Mark the start of an active SDK run phase for heartbeat tracking."""
        self.phase_started_monotonic = now
        self.last_activity_monotonic = now

    def end_active_phase(self) -> None:
        """Clear heartbeat tracking once an active run phase finishes."""
        self.phase_started_monotonic = None
        self.last_activity_monotonic = None

    def clear_activity(self) -> None:
        self.activity_lines.clear()

    def clear_agent_plan(self) -> None:
        self.agent_plan.clear()

    def append_activity(
        self,
        line: str,
        *,
        max_lines: int = DEFAULT_ACTIVITY_BUFFER_SIZE,
    ) -> None:
        if not line:
            return
        if (
            self.activity_lines
            and is_prose_activity_line(line)
            and is_prose_activity_line(self.activity_lines[-1])
        ):
            self.activity_lines[-1] = merge_prose_activity(
                self.activity_lines[-1],
                line,
            )
        else:
            self.activity_lines.append(line)
        overflow = len(self.activity_lines) - max_lines
        if overflow > 0:
            del self.activity_lines[:overflow]

    def reset_steps(self) -> None:
        for key, _ in STEP_DEFINITIONS:
            self.step_status[key] = StepStatus.PENDING

    def set_step(self, key: str, status: StepStatus) -> None:
        if key in self.step_status:
            self.step_status[key] = status

    def mark_interrupted(self) -> None:
        self.phase = "interrupted"
        for key, _ in STEP_DEFINITIONS:
            if self.step_status[key] is StepStatus.RUNNING:
                self.step_status[key] = StepStatus.PENDING


@dataclass
class RichCycleLogger(CycleLogger):
    """CycleLogger that drives a Rich live dashboard instead of plain text logs."""

    state: RunDashboardState = field(default_factory=RunDashboardState)
    on_update: Callable[[], None] | None = field(default=None, repr=False)
    suppress_plain_logs: bool = True
    activity_refresh_interval: float = ACTIVITY_REFRESH_INTERVAL_SECONDS
    _last_activity_refresh: float = field(
        default=-ACTIVITY_REFRESH_INTERVAL_SECONDS,
        init=False,
        repr=False,
    )
    _monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)

    def _refresh(self) -> None:
        if self.on_update is not None:
            self.on_update()

    def append_activity(self, line: str) -> None:
        """Record one agent activity line and refresh the dashboard when due."""
        self.state.append_activity(line)
        now = self._monotonic()
        self.state.note_activity(now)
        self._maybe_refresh_activity(now=now)

    def apply_plan_update(
        self,
        todos: Sequence[Mapping[str, Any]],
        merge: bool,
    ) -> None:
        """Apply a TodoWrite update and refresh the dashboard when due."""
        self.state.agent_plan.apply_todo_write(todos, merge=merge)
        now = self._monotonic()
        self.state.note_activity(now)
        self._maybe_refresh_activity(now=now)

    def _maybe_refresh_activity(self, *, now: float | None = None) -> None:
        now = self._monotonic() if now is None else now
        if now - self._last_activity_refresh < self.activity_refresh_interval:
            return
        self._last_activity_refresh = now
        self._refresh()

    def _emit(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        if not self.suppress_plain_logs:
            self._logger.info(message)
        if self.jsonl_path is not None and payload is not None:
            self._append_jsonl(payload)

    def log_cycle_start(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        next_task_id: int,
        next_task_title: str,
        model_id: str,
        handover_task_id: int | None,
        timestamp: Any = None,
        queue_snapshot: TaskQueueSnapshot | None = None,
    ) -> Any:
        self.state.cycle_number = cycle_number
        self.state.total_cycles = total_cycles
        self.state.next_task_id = next_task_id
        self.state.next_task_title = next_task_title
        self.state.model_id = model_id
        self.state.phase = "implementation"
        self.state.agent_id = ""
        self.state.run_id = ""
        self.state.impl_status = ""
        self.state.update_status = ""
        self.state.verification_result = ""
        self.state.clear_activity()
        self.state.clear_agent_plan()
        if queue_snapshot is not None:
            self.state.queue_strip.apply_snapshot(queue_snapshot)
        self._last_activity_refresh = -self.activity_refresh_interval
        self.state.reset_steps()
        self.state.set_step("resolve", StepStatus.DONE)
        self.state.set_step("implementation", StepStatus.RUNNING)
        self.state.begin_active_phase(self._monotonic())
        self._refresh()
        return super().log_cycle_start(
            cycle_number=cycle_number,
            total_cycles=total_cycles,
            next_task_id=next_task_id,
            next_task_title=next_task_title,
            model_id=model_id,
            handover_task_id=handover_task_id,
            timestamp=timestamp,
            queue_snapshot=queue_snapshot,
        )

    def log_run_complete(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        phase: str,
        agent_id: str,
        run_id: str,
        status: str,
        timestamp: Any = None,
    ) -> None:
        self.state.agent_id = agent_id
        self.state.run_id = run_id
        if phase == "implementation":
            self.state.phase = "snapshot"
            self.state.impl_status = status
            self.state.set_step("implementation", StepStatus.DONE)
            self.state.set_step("snapshot", StepStatus.RUNNING)
        elif phase == "update":
            self.state.phase = "verify"
            self.state.update_status = status
            self.state.set_step("update", StepStatus.DONE)
        self.state.end_active_phase()
        self._refresh()
        super().log_run_complete(
            cycle_number=cycle_number,
            total_cycles=total_cycles,
            phase=phase,
            agent_id=agent_id,
            run_id=run_id,
            status=status,
            timestamp=timestamp,
        )

    def log_handover_snapshot(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        label: str,
        path: Path,
        task_id: int | None,
        content_hash: str,
        missing: bool,
        captured_at: Any = None,
        timestamp: Any = None,
    ) -> None:
        if label == "before":
            self.state.phase = "update"
            self.state.set_step("update", StepStatus.RUNNING)
            self.state.begin_active_phase(self._monotonic())
        elif label == "after":
            self.state.set_step("snapshot", StepStatus.DONE)
            self.state.set_step("verify", StepStatus.RUNNING)
            self.state.phase = "verify"
        self._refresh()
        super().log_handover_snapshot(
            cycle_number=cycle_number,
            total_cycles=total_cycles,
            label=label,
            path=path,
            task_id=task_id,
            content_hash=content_hash,
            missing=missing,
            captured_at=captured_at,
            timestamp=timestamp,
        )

    def log_verification_result(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        result: str,
        before_task_id: int | None,
        after_task_id: int | None,
        before_hash: str,
        after_hash: str,
        timestamp: Any = None,
    ) -> None:
        self.state.verification_result = result
        self.state.phase = "complete"
        self.state.set_step("verify", StepStatus.DONE)
        if before_task_id is not None and result.lower() in {"passed", "pass", "ok", "success"}:
            self.state.queue_strip.mark_task_completed(before_task_id)
        self._refresh()
        super().log_verification_result(
            cycle_number=cycle_number,
            total_cycles=total_cycles,
            result=result,
            before_task_id=before_task_id,
            after_task_id=after_task_id,
            before_hash=before_hash,
            after_hash=after_hash,
            timestamp=timestamp,
        )

    def log_cycle(self, record: CycleLogRecord) -> None:
        self._refresh()
        super().log_cycle(record)


def activity_panel_content_width(console_width: int) -> int:
    """Return usable text width inside the dashboard activity panel."""
    return max(20, console_width - ACTIVITY_PANEL_HORIZONTAL_PADDING)


def format_activity_for_display(line: str, *, width: int) -> str:
    """Render one logical activity entry with width-aware prose wrapping."""
    if is_prose_activity_line(line):
        truncated = truncate_activity_text(
            line,
            max_len=DEFAULT_ACTIVITY_PROSE_DISPLAY_MAX_LEN,
        )
        return textwrap.fill(
            truncated,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
    return line


def render_activity_visual_lines(
    activity_lines: Sequence[str],
    *,
    width: int,
    max_visual_lines: int = DEFAULT_ACTIVITY_MAX_VISUAL_LINES,
) -> list[str]:
    """Wrap activity entries and crop to a fixed visual height for stable Live layout."""
    if max_visual_lines <= 0:
        return []
    wrapped: list[str] = []
    for line in activity_lines:
        formatted = format_activity_for_display(line, width=width)
        wrapped.extend(formatted.splitlines() or [""])
    if len(wrapped) > max_visual_lines:
        wrapped = wrapped[-max_visual_lines:]
    while len(wrapped) < max_visual_lines:
        wrapped.append("")
    return wrapped


def render_agent_plan_section(
    state: AgentPlanState,
    *,
    max_visible: int = DEFAULT_AGENT_PLAN_MAX_VISIBLE,
) -> RenderableType | None:
    """Build the Agent plan section, or ``None`` when no todos are present."""
    if not state.items:
        return None

    visible = state.items[:max_visible]
    overflow = len(state.items) - len(visible)

    plan = Table(show_header=False, box=None, padding=(0, 1))
    plan.add_column(width=2)
    plan.add_column()
    for item in visible:
        plan.add_row(_agent_plan_icon(item.status), item.content or item.id)
    if overflow > 0:
        plan.add_row("", f"[dim]+{overflow} more[/dim]")

    return Group(Text("Agent plan", style="bold"), plan)


def render_heartbeat(
    state: RunDashboardState,
    *,
    now: float | None = None,
    idle_threshold: float = HEARTBEAT_IDLE_THRESHOLD_SECONDS,
) -> Text | None:
    """Build a 'still running' heartbeat line when an active run has gone quiet.

    Returns ``None`` unless the dashboard is in an active SDK run phase and no
    new activity has arrived for at least ``idle_threshold`` seconds. This keeps
    a slow-but-alive run (e.g. a long test suite) from looking hung.
    """
    if state.phase not in ACTIVE_RUN_PHASES:
        return None
    if state.last_activity_monotonic is None:
        return None
    current = time.monotonic() if now is None else now
    idle = current - state.last_activity_monotonic
    if idle < idle_threshold:
        return None
    message = f"still running - no new activity for {format_elapsed_short(idle)}"
    if state.phase_started_monotonic is not None:
        elapsed = current - state.phase_started_monotonic
        message += f" ({format_elapsed_short(elapsed)} elapsed)"
    return Text.assemble(("\u23f3 ", "yellow"), (message, "yellow"))


def render_dashboard(
    state: RunDashboardState,
    *,
    console_width: int | None = None,
    now: float | None = None,
) -> RenderableType:
    """Build the Rich renderable for the current dashboard state."""
    header = Text.assemble(
        ("Cursor Cyclopsctl", "bold"),
        ("  ·  ", "dim"),
        (f"Cycle {state.cycle_number}/{state.total_cycles}", "cyan"),
    )

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=32),
        TaskProgressColumn(),
        expand=True,
    )
    completed = max(0, state.cycle_number - 1)
    if state.phase == "complete":
        completed = state.cycle_number
    progress.add_task(
        "Overall progress",
        total=max(state.total_cycles, 1),
        completed=min(completed, state.total_cycles),
    )

    task_line = Text()
    if state.next_task_id is not None:
        task_line.append(f"Task {state.next_task_id}", style="bold")
        if state.next_task_title:
            task_line.append(f" — {state.next_task_title}", style="dim")

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim")
    meta.add_column()
    meta.add_row("Model", state.model_id or "—")
    meta.add_row("Phase", state.phase or "idle")
    if state.agent_id:
        meta.add_row("Agent", state.agent_id)
    if state.run_id:
        meta.add_row("Run", state.run_id)
    if state.impl_status:
        meta.add_row("Impl status", state.impl_status)
    if state.update_status:
        meta.add_row("Update status", state.update_status)
    if state.verification_result:
        meta.add_row("Verification", state.verification_result)

    steps = Table(show_header=False, box=None, padding=(0, 1))
    steps.add_column(width=2)
    steps.add_column()
    for key, label in STEP_DEFINITIONS:
        status = state.step_status.get(key, StepStatus.PENDING)
        steps.add_row(_step_icon(status), label)

    sections: list[RenderableType] = [
        progress,
        Text(""),
    ]
    heartbeat = render_heartbeat(state, now=now)
    if heartbeat is not None:
        sections.extend([heartbeat, Text("")])
    queue_strip = render_queue_strip_section(state.queue_strip)
    if queue_strip is not None:
        sections.append(queue_strip)
        sections.append(Text(""))
    sections.extend(
        [
        task_line if state.next_task_id is not None else Text("Waiting for next task…", style="dim"),
        Text(""),
        meta,
        Text(""),
        Text("Steps", style="bold"),
        steps,
        ]
    )
    agent_plan = render_agent_plan_section(state.agent_plan)
    if agent_plan is not None:
        sections.extend([Text(""), agent_plan])

    if state.activity_lines:
        activity_width = activity_panel_content_width(console_width or 80)
        activity = Text()
        for visual_line in render_activity_visual_lines(
            state.activity_lines,
            width=activity_width,
        ):
            activity.append(visual_line)
            activity.append("\n")
        sections.extend(
            [
                Text(""),
                Text("Recent activity", style="bold"),
                activity,
            ]
        )

    body = Group(*sections)
    return Panel(body, title=header, border_style="blue")


def activity_callback_for_logger(logger: CycleLogger | None) -> ActivityCallback | None:
    """Return an activity sink for Rich mode, or ``None`` for plain logging."""
    if isinstance(logger, RichCycleLogger):
        return logger.append_activity
    return None


def plan_callback_for_logger(logger: CycleLogger | None) -> PlanUpdateCallback | None:
    """Return a TodoWrite plan sink for Rich mode, or ``None`` for plain logging."""
    if isinstance(logger, RichCycleLogger):
        return logger.apply_plan_update
    return None


def use_rich_display(*, plain: bool = False) -> bool:
    """Return True when the Rich live dashboard should be used."""
    return not plain and sys.stderr.isatty()


@contextmanager
def managed_cycle_display(
    *,
    plain: bool = False,
    jsonl_path: Path | None = None,
    interrupt: RunInterruptController | None = None,
) -> Iterator[CycleLogger]:
    """Yield a cycle logger, optionally wrapped in a Rich Live display."""
    if plain or not sys.stderr.isatty():
        yield CycleLogger(jsonl_path=jsonl_path)
        return

    state = RunDashboardState()
    console = Console(stderr=True)
    rich_logger = RichCycleLogger(state=state, jsonl_path=jsonl_path)
    live: Live | None = None
    live_stopped = False

    def stop_live() -> None:
        nonlocal live_stopped
        if live_stopped:
            return
        live_stopped = True
        state.mark_interrupted()
        rich_logger._refresh()
        if live is not None:
            live.stop()

    if interrupt is not None:
        interrupt.add_callback(stop_live)

    def dashboard_renderable() -> RenderableType:
        return render_dashboard(
            state,
            console_width=console.size.width,
            now=time.monotonic(),
        )

    with Live(
        console=console,
        get_renderable=dashboard_renderable,
        auto_refresh=True,
        refresh_per_second=HEARTBEAT_REFRESH_PER_SECOND,
        transient=False,
    ) as live_ctx:
        live = live_ctx
        rich_logger.on_update = live_ctx.refresh
        try:
            yield rich_logger
        finally:
            if interrupt is not None and interrupt.stop_requested:
                stop_live()


SUMMARY_TITLE = "Run summary"
SUMMARY_COLUMNS = (
    "Cycle",
    "Task ID",
    "Title",
    "Model",
    "Duration",
    "Verification",
)
SUMMARY_TITLE_MAX_LEN = 40


SUMMARY_SKIPPED_TITLE = "Skipped tasks (--resume)"


class RunSummaryOutcome(Protocol):
    """Minimal outcome shape consumed by post-run summary rendering."""

    cycle_number: int
    task_id: int
    task_title: str
    model_id: str
    duration_seconds: float
    verification_result: str


def _outcome_agent_id(outcome: RunSummaryOutcome) -> str:
    agent_id = getattr(outcome, "agent_id", "") or ""
    return agent_id or "—"


def _outcome_impl_run_id(outcome: RunSummaryOutcome) -> str:
    run_id = getattr(outcome, "impl_run_id", "") or ""
    return run_id or "—"


def _outcome_git_summary(outcome: RunSummaryOutcome) -> str | None:
    return getattr(outcome, "git_diff_summary", None)


def _summary_columns_for_outcomes(outcomes: list[RunSummaryOutcome]) -> tuple[str, ...]:
    columns = list(SUMMARY_COLUMNS)
    if any(_outcome_agent_id(outcome) != "—" for outcome in outcomes):
        columns.extend(["Agent ID", "Run ID"])
    if any(_outcome_git_summary(outcome) for outcome in outcomes):
        columns.append("Git changes")
    return tuple(columns)


def _format_git_summary_cell(summary: str | None, *, max_len: int = 40) -> str:
    if not summary:
        return "—"
    first_line = summary.splitlines()[0]
    return _truncate_title(first_line, max_len=max_len)


def format_duration(seconds: float) -> str:
    """Format a cycle duration for human-readable summary output."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        if seconds >= 10:
            return f"{seconds:.1f}s"
        return f"{seconds:.2f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _truncate_title(title: str, *, max_len: int = SUMMARY_TITLE_MAX_LEN) -> str:
    if len(title) <= max_len:
        return title
    if max_len <= 1:
        return title[:max_len]
    return title[: max_len - 1] + "…"


def _verification_style(result: str) -> str:
    normalized = result.lower()
    if normalized in {"passed", "pass", "ok", "success"}:
        return "[green]passed[/green]"
    if normalized in {"failed", "fail", "error"}:
        return "[red]failed[/red]"
    if normalized in {"interrupted", "interrupt"}:
        return "[yellow]interrupted[/yellow]"
    if normalized == "dry-run":
        return "[cyan]dry-run[/cyan]"
    return result


def render_summary_table(outcomes: list[RunSummaryOutcome]) -> Table:
    """Build a Rich table summarizing completed cycle outcomes."""
    columns = _summary_columns_for_outcomes(outcomes)
    table = Table(title=SUMMARY_TITLE, show_header=True, show_lines=False)
    for column in columns:
        table.add_column(
            column,
            overflow="ellipsis",
            no_wrap=column in {"Cycle", "Task ID", "Duration", "Agent ID", "Run ID"},
        )
    for outcome in outcomes:
        row = [
            str(outcome.cycle_number),
            str(outcome.task_id),
            _truncate_title(outcome.task_title),
            outcome.model_id,
            format_duration(outcome.duration_seconds),
            _verification_style(outcome.verification_result),
        ]
        if "Agent ID" in columns:
            row.extend([_outcome_agent_id(outcome), _outcome_impl_run_id(outcome)])
        if "Git changes" in columns:
            row.append(_format_git_summary_cell(_outcome_git_summary(outcome)))
        table.add_row(*row)
    return table


def format_plain_summary(outcomes: list[RunSummaryOutcome]) -> str:
    """Render cycle outcomes as a plain ASCII table or structured log lines."""
    if not outcomes:
        return ""

    headers = list(_summary_columns_for_outcomes(outcomes))
    rows: list[list[str]] = []
    for outcome in outcomes:
        row = [
            str(outcome.cycle_number),
            str(outcome.task_id),
            _truncate_title(outcome.task_title),
            outcome.model_id,
            format_duration(outcome.duration_seconds),
            outcome.verification_result,
        ]
        if "Agent ID" in headers:
            row.extend([_outcome_agent_id(outcome), _outcome_impl_run_id(outcome)])
        if "Git changes" in headers:
            row.append(_format_git_summary_cell(_outcome_git_summary(outcome)))
        rows.append(row)
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def _format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells))

    lines = [SUMMARY_TITLE, _format_row(headers), _format_row(["-" * width for width in widths])]
    lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def render_launch_menu() -> RenderableType:
    """Build a Rich panel listing launcher entry actions."""
    menu = Table(show_header=False, box=None, padding=(0, 1))
    menu.add_column(width=2, style="cyan", no_wrap=True)
    menu.add_column()
    from cyclopsctl.launcher import LAUNCH_ACTION_LABELS, LaunchAction

    for index, action in enumerate(LaunchAction, start=1):
        menu.add_row(str(index), LAUNCH_ACTION_LABELS[action])
    return Panel(
        menu,
        title="[bold]Choose an action[/bold]",
        border_style="green",
    )


def format_launch_menu_plain() -> str:
    """Render launcher entry actions as plain text."""
    from cyclopsctl.launcher import LAUNCH_ACTION_LABELS, LaunchAction

    lines = ["Choose an action:", ""]
    for index, action in enumerate(LaunchAction, start=1):
        lines.append(f"  {index}. {LAUNCH_ACTION_LABELS[action]}")
    return "\n".join(lines)


def print_launch_menu(
    *,
    plain: bool = False,
    stderr_is_tty: bool | None = None,
) -> None:
    """Print the launcher action menu."""
    use_rich = not plain and (
        stderr_is_tty if stderr_is_tty is not None else sys.stderr.isatty()
    )
    if use_rich:
        Console(stderr=True).print(render_launch_menu())
        return
    print(format_launch_menu_plain(), file=sys.stderr)


def render_launch_overview(status: object) -> RenderableType:
    """Build a Rich panel summarizing pre-run launcher status."""
    from cyclopsctl.launcher import format_launch_summary_line

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="dim", no_wrap=True)
    overview.add_column()
    overview.add_row("Summary", format_launch_summary_line(status))
    overview.add_row("Project root", str(status.config.project_root))
    active_tag = getattr(status, "active_tag", None) or getattr(status.config, "tag", None)
    if active_tag:
        overview.add_row("Active tag", active_tag)
    if getattr(status, "profile_names", ()):
        overview.add_row("Profiles", ", ".join(status.profile_names))
    overview.add_row("Composer tier", getattr(status, "default_composer_tier", "standard"))
    overview.add_row("Grok tier", getattr(status, "default_grok_tier", "standard"))
    overview.add_row(
        "Fable enabled",
        "yes" if getattr(status, "default_fable_enabled", True) else "no",
    )
    if status.handover_task_id is not None:
        overview.add_row("Handover task", str(status.handover_task_id))
    else:
        overview.add_row("Handover task", "—")
    if status.next_task is not None and status.next_task.found and status.next_task.task is not None:
        task = status.next_task.task
        overview.add_row("Backend next", f"#{task.task_id} — {task.title}")
    elif status.next_task is not None and not status.next_task.found:
        overview.add_row("Backend next", "queue empty")
    elif status.next_task_error:
        overview.add_row("Backend next", status.next_task_error)
    else:
        overview.add_row("Backend next", "—")
    if status.pending_count is not None:
        overview.add_row("Pending tasks", str(status.pending_count))
    overview.add_row("Suggested cycles", str(status.suggested_cycles))
    overview.add_row(
        "Resume available",
        "yes" if status.resume_available else "no",
    )

    sections: list[RenderableType] = [overview, Text(""), render_diagnostics_rich(status.checks)]
    body = Group(*sections)
    return Panel(body, title="[bold]Cyclopsctl launch[/bold]", border_style="cyan")


def format_launch_overview_plain(status: object) -> str:
    """Render launcher status as plain text."""
    from cyclopsctl.doctor import format_diagnostics_plain
    from cyclopsctl.launcher import format_launch_summary_line

    lines = [
        "Cyclopsctl launch",
        "",
        format_launch_summary_line(status),
        "",
        f"Project root: {status.config.project_root}",
    ]
    active_tag = getattr(status, "active_tag", None) or getattr(status.config, "tag", None)
    if active_tag:
        lines.append(f"Active tag: {active_tag}")
    profile_names = getattr(status, "profile_names", ())
    if profile_names:
        lines.append(f"Profiles: {', '.join(profile_names)}")
    lines.append(f"Composer tier: {getattr(status, 'default_composer_tier', 'standard')}")
    lines.append(f"Grok tier: {getattr(status, 'default_grok_tier', 'standard')}")
    fable_enabled = getattr(status, "default_fable_enabled", True)
    lines.append(f"Fable enabled: {'yes' if fable_enabled else 'no'}")
    if status.handover_task_id is not None:
        lines.append(f"Handover task ID: {status.handover_task_id}")
    if status.next_task is not None and status.next_task.found and status.next_task.task is not None:
        task = status.next_task.task
        lines.append(f"Backend next: #{task.task_id} — {task.title}")
    elif status.next_task is not None and not status.next_task.found:
        lines.append("Backend next: queue empty")
    elif status.next_task_error:
        lines.append(f"Backend next: {status.next_task_error}")
    if status.pending_count is not None:
        lines.append(f"Pending tasks: {status.pending_count}")
    lines.extend(
        [
            f"Suggested cycles: {status.suggested_cycles}",
            f"Resume available: {'yes' if status.resume_available else 'no'}",
            "",
            format_diagnostics_plain(status.checks),
        ]
    )
    return "\n".join(lines)


def print_launch_status(
    status: object,
    *,
    plain: bool = False,
    stderr_is_tty: bool | None = None,
) -> None:
    """Print launcher diagnostics and project summary."""
    use_rich = not plain and (
        stderr_is_tty if stderr_is_tty is not None else sys.stderr.isatty()
    )
    if use_rich:
        console = Console(stderr=True)
        console.print(render_launch_overview(status))
        return
    print(format_launch_overview_plain(status), file=sys.stderr)


class RunSummarySkipped(Protocol):
    """Minimal skipped-task shape consumed by post-run summary rendering."""

    task_id: int
    task_title: str


def render_skipped_tasks_table(skipped_tasks: list[RunSummarySkipped]) -> Table:
    """Build a Rich table summarizing resume-skipped tasks."""
    table = Table(title=SUMMARY_SKIPPED_TITLE, show_header=True, show_lines=False)
    table.add_column("Task ID", no_wrap=True)
    table.add_column("Title", overflow="ellipsis")
    for skipped in skipped_tasks:
        table.add_row(str(skipped.task_id), _truncate_title(skipped.task_title))
    return table


def format_plain_skipped_summary(skipped_tasks: list[RunSummarySkipped]) -> str:
    """Render skipped tasks as plain text lines."""
    if not skipped_tasks:
        return ""
    lines = [SUMMARY_SKIPPED_TITLE]
    for skipped in skipped_tasks:
        lines.append(f"  {skipped.task_id}: {_truncate_title(skipped.task_title)}")
    return "\n".join(lines)


def print_run_summary(
    outcomes: list[RunSummaryOutcome],
    *,
    skipped_tasks: list[RunSummarySkipped] | None = None,
    plain: bool = False,
    stderr_is_tty: bool | None = None,
) -> None:
    """Print the post-run summary after the live dashboard has stopped."""
    skipped = skipped_tasks or []
    if not outcomes and not skipped:
        return
    use_rich = not plain and (
        stderr_is_tty if stderr_is_tty is not None else sys.stderr.isatty()
    )
    if use_rich:
        console = Console(stderr=True)
        if outcomes:
            console.print(render_summary_table(outcomes))
        if skipped:
            console.print(render_skipped_tasks_table(skipped))
        return
    if outcomes:
        print(format_plain_summary(outcomes), file=sys.stderr)
    if skipped:
        plain_skipped = format_plain_skipped_summary(skipped)
        if plain_skipped:
            print(plain_skipped, file=sys.stderr)
