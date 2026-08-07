"""Tests for Rich live cycle dashboard (tui module)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cyclopsctl.logging import CycleLogRecord, CycleLogger
from cyclopsctl.tasks.types import NextTaskResult
from cyclopsctl.tui import (
    AgentPlanItem,
    AgentPlanItemStatus,
    AgentPlanState,
    DEFAULT_ACTIVITY_MAX_VISUAL_LINES,
    RichCycleLogger,
    RunDashboardState,
    StepStatus,
    TaskQueueSnapshot,
    TaskQueueStripState,
    activity_callback_for_logger,
    activity_panel_content_width,
    build_task_queue_snapshot,
    HEARTBEAT_IDLE_THRESHOLD_SECONDS,
    format_activity_for_display,
    format_elapsed_short,
    format_launch_menu_plain,
    format_launch_overview_plain,
    format_queue_strip_line,
    managed_cycle_display,
    plan_callback_for_logger,
    render_activity_visual_lines,
    render_agent_plan_section,
    render_dashboard,
    render_heartbeat,
    render_launch_menu,
    render_queue_strip_section,
    use_rich_display,
)


def test_use_rich_display_respects_plain_and_tty():
    with patch.object(sys.stderr, "isatty", return_value=True):
        assert use_rich_display(plain=False) is True
        assert use_rich_display(plain=True) is False
    with patch.object(sys.stderr, "isatty", return_value=False):
        assert use_rich_display(plain=False) is False


def test_managed_cycle_display_plain_yields_standard_logger():
    with managed_cycle_display(plain=True) as logger:
        assert isinstance(logger, CycleLogger)
        assert not isinstance(logger, RichCycleLogger)


def test_managed_cycle_display_non_tty_yields_standard_logger():
    with patch.object(sys.stderr, "isatty", return_value=False):
        with managed_cycle_display(plain=False) as logger:
            assert isinstance(logger, CycleLogger)
            assert not isinstance(logger, RichCycleLogger)


def test_managed_cycle_display_tty_yields_rich_logger():
    mock_live = MagicMock()
    mock_live.__enter__ = MagicMock(return_value=mock_live)
    mock_live.__exit__ = MagicMock(return_value=False)

    with patch.object(sys.stderr, "isatty", return_value=True):
        with patch("cyclopsctl.tui.Live", return_value=mock_live) as live_ctor:
            with managed_cycle_display(plain=False) as logger:
                assert isinstance(logger, RichCycleLogger)
                mock_live.refresh.assert_not_called()
            live_ctor.assert_called_once()
            assert live_ctor.call_args.kwargs["auto_refresh"] is True
            assert live_ctor.call_args.kwargs["refresh_per_second"] >= 1
            assert live_ctor.call_args.kwargs["get_renderable"] is not None


def test_managed_cycle_display_interrupt_stops_live_without_leaving_running_steps():
    from cyclopsctl.interrupt import RunInterruptController

    mock_live = MagicMock()
    mock_live.__enter__ = MagicMock(return_value=mock_live)
    mock_live.__exit__ = MagicMock(return_value=False)
    interrupt = RunInterruptController()

    with patch.object(sys.stderr, "isatty", return_value=True):
        with patch("cyclopsctl.tui.Live", return_value=mock_live):
            with managed_cycle_display(plain=False, interrupt=interrupt) as logger:
                logger.log_cycle_start(
                    cycle_number=1,
                    total_cycles=1,
                    next_task_id=1,
                    next_task_title="Task",
                    model_id="composer-2.5",
                    handover_task_id=1,
                )
                interrupt.request_stop()

    mock_live.stop.assert_called_once()
    assert logger.state.phase == "interrupted"  # type: ignore[attr-defined]
    assert logger.state.step_status["implementation"] is StepStatus.PENDING  # type: ignore[attr-defined]


def test_rich_logger_updates_state_on_cycle_start():
    state = RunDashboardState()
    updates: list[None] = []
    logger = RichCycleLogger(state=state, on_update=lambda: updates.append(None))

    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=3,
        next_task_id=42,
        next_task_title="Build dashboard",
        model_id="composer-2.5",
        handover_task_id=42,
    )

    assert state.cycle_number == 1
    assert state.total_cycles == 3
    assert state.next_task_id == 42
    assert state.next_task_title == "Build dashboard"
    assert state.phase == "implementation"
    assert state.step_status["resolve"] is StepStatus.DONE
    assert state.step_status["implementation"] is StepStatus.RUNNING
    assert len(updates) == 1


def test_rich_logger_advances_steps_through_cycle():
    state = RunDashboardState()
    logger = RichCycleLogger(state=state)

    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Task 1",
        model_id="composer-2.5",
        handover_task_id=1,
    )
    logger.log_run_complete(
        cycle_number=1,
        total_cycles=1,
        phase="implementation",
        agent_id="agent-1",
        run_id="run-1",
        status="finished",
    )
    logger.log_handover_snapshot(
        cycle_number=1,
        total_cycles=1,
        label="before",
        path=Path("handover.md"),
        task_id=1,
        content_hash="a" * 64,
        missing=False,
    )
    logger.log_run_complete(
        cycle_number=1,
        total_cycles=1,
        phase="update",
        agent_id="agent-1",
        run_id="run-2",
        status="finished",
    )
    logger.log_handover_snapshot(
        cycle_number=1,
        total_cycles=1,
        label="after",
        path=Path("handover.md"),
        task_id=2,
        content_hash="b" * 64,
        missing=False,
    )
    logger.log_verification_result(
        cycle_number=1,
        total_cycles=1,
        result="passed",
        before_task_id=1,
        after_task_id=2,
        before_hash="a" * 64,
        after_hash="b" * 64,
    )

    assert state.impl_status == "finished"
    assert state.update_status == "finished"
    assert state.verification_result == "passed"
    assert state.phase == "complete"
    assert all(status is StepStatus.DONE for status in state.step_status.values())


def test_rich_logger_suppresses_plain_logs_but_keeps_jsonl(tmp_path: Path, caplog):
    import logging

    jsonl_path = tmp_path / "cycles.jsonl"
    logger = RichCycleLogger(jsonl_path=jsonl_path, suppress_plain_logs=True)

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        logger.log_cycle_start(
            cycle_number=1,
            total_cycles=1,
            next_task_id=1,
            next_task_title="Task 1",
            model_id="composer-2.5",
            handover_task_id=1,
        )

    assert caplog.text == ""
    payload = json.loads(jsonl_path.read_text(encoding="utf-8").strip())
    assert payload["event"] == "cycle_start"
    assert payload["next_task_id"] == 1


def test_render_dashboard_produces_panel():
    state = RunDashboardState(
        cycle_number=2,
        total_cycles=5,
        next_task_id=3,
        next_task_title="Wire CLI",
        model_id="composer-2.5",
        phase="implementation",
    )
    state.set_step("resolve", StepStatus.DONE)
    state.set_step("implementation", StepStatus.RUNNING)

    rendered = render_dashboard(state)
    assert rendered is not None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m"),
        (90, "1m 30s"),
        (3600, "1h"),
        (3660, "1h 1m"),
        (-5, "0s"),
    ],
)
def test_format_elapsed_short(seconds, expected):
    assert format_elapsed_short(seconds) == expected


def test_render_heartbeat_none_outside_active_phase():
    state = RunDashboardState(phase="verify", last_activity_monotonic=0.0)
    assert render_heartbeat(state, now=1000.0) is None


def test_render_heartbeat_none_when_recently_active():
    state = RunDashboardState(phase="implementation", last_activity_monotonic=100.0)
    now = 100.0 + HEARTBEAT_IDLE_THRESHOLD_SECONDS - 1
    assert render_heartbeat(state, now=now) is None


def test_render_heartbeat_shows_idle_and_elapsed():
    state = RunDashboardState(
        phase="implementation",
        last_activity_monotonic=100.0,
        phase_started_monotonic=40.0,
    )
    heartbeat = render_heartbeat(state, now=130.0)
    assert heartbeat is not None
    text = heartbeat.plain
    assert "still running" in text
    assert "30s" in text
    assert "1m 30s elapsed" in text


def test_render_dashboard_includes_heartbeat_when_idle():
    from rich.console import Console

    state = RunDashboardState(
        phase="implementation",
        last_activity_monotonic=0.0,
        phase_started_monotonic=0.0,
    )
    console = Console(width=120, record=True)
    console.print(render_dashboard(state, console_width=120, now=120.0))
    rendered_text = console.export_text()
    assert "still running" in rendered_text


def test_rich_logger_tracks_activity_timestamps():
    clock = iter([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    logger = RichCycleLogger(_monotonic=lambda: next(clock))
    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="t",
        model_id="m",
        handover_task_id=None,
    )
    assert logger.state.phase_started_monotonic == 10.0
    assert logger.state.last_activity_monotonic == 10.0
    logger.append_activity("shell · pytest")
    assert logger.state.last_activity_monotonic == 11.0


def test_render_agent_plan_section_hidden_when_empty():
    assert render_agent_plan_section(AgentPlanState()) is None


def test_render_agent_plan_section_shows_status_icons():
    from rich.console import Console

    plan = AgentPlanState()
    plan.items = [
        AgentPlanItem(id="1", content="Done step", status=AgentPlanItemStatus.COMPLETED),
        AgentPlanItem(id="2", content="Active step", status=AgentPlanItemStatus.IN_PROGRESS),
        AgentPlanItem(id="3", content="Pending step", status=AgentPlanItemStatus.PENDING),
        AgentPlanItem(id="4", content="Cancelled step", status=AgentPlanItemStatus.CANCELLED),
    ]
    console = Console(width=120, record=True)
    console.print(render_agent_plan_section(plan))
    rendered_text = console.export_text()

    assert "Agent plan" in rendered_text
    assert "Done step" in rendered_text
    assert "Active step" in rendered_text
    assert "Pending step" in rendered_text
    assert "Cancelled step" in rendered_text


def test_render_dashboard_includes_agent_plan_above_recent_activity():
    from rich.console import Console

    state = RunDashboardState(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Stream activity",
        model_id="composer-2.5",
        phase="implementation",
    )
    state.agent_plan.items = [
        AgentPlanItem(id="1", content="Explore codebase", status=AgentPlanItemStatus.IN_PROGRESS),
        AgentPlanItem(id="2", content="Run pytest", status=AgentPlanItemStatus.PENDING),
    ]
    state.append_activity("Read · runner.py")

    console = Console(width=120, record=True)
    console.print(render_dashboard(state, console_width=120))
    rendered_text = console.export_text()

    assert "Agent plan" in rendered_text
    assert "Explore codebase" in rendered_text
    assert "Recent activity" in rendered_text
    assert rendered_text.index("Agent plan") < rendered_text.index("Recent activity")


def test_render_dashboard_hides_agent_plan_when_empty():
    from rich.console import Console

    state = RunDashboardState(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="No plan",
        model_id="composer-2.5",
        phase="implementation",
    )
    state.append_activity("Read · runner.py")

    console = Console(width=120, record=True)
    console.print(render_dashboard(state, console_width=120))
    rendered_text = console.export_text()

    assert "Agent plan" not in rendered_text
    assert "Recent activity" in rendered_text


def test_render_agent_plan_section_caps_visible_items():
    from rich.console import Console

    plan = AgentPlanState()
    plan.items = [
        AgentPlanItem(id=str(index), content=f"Task {index}", status=AgentPlanItemStatus.PENDING)
        for index in range(14)
    ]
    console = Console(width=120, record=True)
    console.print(render_agent_plan_section(plan, max_visible=12))
    rendered_text = console.export_text()

    assert "Task 0" in rendered_text
    assert "Task 11" in rendered_text
    assert "Task 12" not in rendered_text
    assert "+2 more" in rendered_text


def test_agent_plan_persists_through_update_phase():
    state = RunDashboardState()
    logger = RichCycleLogger(state=state)
    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Task 1",
        model_id="composer-2.5",
        handover_task_id=1,
    )
    logger.apply_plan_update(
        [{"id": "1", "content": "Implement", "status": "in_progress"}],
        False,
    )
    logger.log_run_complete(
        cycle_number=1,
        total_cycles=1,
        phase="implementation",
        agent_id="agent-1",
        run_id="run-1",
        status="finished",
    )
    logger.log_handover_snapshot(
        cycle_number=1,
        total_cycles=1,
        label="before",
        path=Path("handover.md"),
        task_id=1,
        content_hash="a" * 64,
        missing=False,
    )
    logger.apply_plan_update(
        [{"id": "1", "content": "Implement", "status": "completed"}],
        True,
    )

    assert len(state.agent_plan.items) == 1
    assert state.agent_plan.items[0].status is AgentPlanItemStatus.COMPLETED


def test_plan_callback_for_logger_returns_rich_sink():
    assert plan_callback_for_logger(RichCycleLogger()) is not None
    assert plan_callback_for_logger(CycleLogger()) is None


def test_render_dashboard_includes_recent_activity():
    from rich.console import Console

    state = RunDashboardState(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Stream activity",
        model_id="composer-2.5",
        phase="implementation",
    )
    state.append_activity("Read · runner.py")
    state.append_activity("Write · tui.py · dashboard update")

    console = Console(width=120, record=True)
    console.print(render_dashboard(state, console_width=120))
    rendered_text = console.export_text()

    assert "Recent activity" in rendered_text
    assert "Read · runner.py" in rendered_text
    assert "Write · tui.py" in rendered_text


def test_render_dashboard_wraps_long_prose_within_terminal_width():
    prose = " ".join(["word"] * 80)
    wrapped = format_activity_for_display(
        prose,
        width=activity_panel_content_width(120),
    )
    visual_lines = wrapped.splitlines()

    assert len(visual_lines) <= 4
    assert all(len(line) <= activity_panel_content_width(120) for line in visual_lines)


def test_render_activity_visual_lines_keeps_fixed_height():
    width = activity_panel_content_width(120)
    prose = " ".join(["word"] * 80)
    visual_lines = render_activity_visual_lines([prose], width=width)

    assert len(visual_lines) == DEFAULT_ACTIVITY_MAX_VISUAL_LINES
    assert all(len(line) <= width for line in visual_lines if line)


def test_render_activity_visual_lines_preserves_tool_lines():
    width = activity_panel_content_width(120)
    tool_line = "Read · runner.py · open file"
    visual_lines = render_activity_visual_lines([tool_line], width=width)

    assert tool_line in visual_lines


def test_rich_logger_clears_activity_on_cycle_start():
    state = RunDashboardState()
    logger = RichCycleLogger(state=state)
    state.append_activity("stale line")

    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Fresh cycle",
        model_id="composer-2.5",
        handover_task_id=1,
    )

    assert state.activity_lines == []


def test_managed_cycle_display_plain_has_no_activity_callback():
    with managed_cycle_display(plain=True) as logger:
        assert activity_callback_for_logger(logger) is None


def test_render_launch_menu_lists_actions():
    rendered = render_launch_menu()
    assert rendered is not None
    plain = format_launch_menu_plain()
    assert "Run cyclopsctl cycles" in plain
    assert "Bootstrap from PRD" in plain
    assert "Run doctor diagnostics" in plain
    assert "Inspect available models" in plain


def test_format_launch_overview_includes_profile_and_routing():
    status = type(
        "S",
        (),
        {
            "config": type("C", (), {"project_root": Path("/tmp/project")})(),
            "handover_task_id": 14,
            "next_task": None,
            "pending_count": 2,
            "suggested_cycles": 2,
            "resume_available": True,
            "profile_names": ("daytime-fast", "overnight-quality"),
            "default_composer_tier": "fast",
            "default_grok_tier": "standard",
            "default_fable_enabled": False,
            "next_task_error": None,
            "checks": [],
        },
    )()
    plain = format_launch_overview_plain(status)
    assert "Profiles: daytime-fast, overnight-quality" in plain
    assert "Composer tier: fast" in plain
    assert "Grok tier: standard" in plain
    assert "Fable enabled: no" in plain


def test_log_cycle_record_still_emits_jsonl(tmp_path: Path):
    jsonl_path = tmp_path / "cycles.jsonl"
    logger = RichCycleLogger(jsonl_path=jsonl_path)
    started = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 6, 5, 10, 5, 0, tzinfo=timezone.utc)
    record = CycleLogRecord(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Task 1",
        model_id="composer-2.5",
        agent_id="agent-1",
        impl_run_id="run-1",
        impl_status="finished",
        update_run_id="run-2",
        update_status="finished",
        handover_before_task_id=1,
        handover_after_task_id=2,
        handover_before_hash="a" * 64,
        handover_after_hash="b" * 64,
        verification_result="passed",
        started_at=started,
        completed_at=completed,
    )

    logger.log_cycle(record)

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "cycle_complete"


def _task(task_id: int) -> NextTaskResult:
    return NextTaskResult(
        task_id=str(task_id),
        title=f"Task {task_id}",
        status="pending",
        priority="medium",
        complexity=5,
        tag="master",
    )


def test_build_task_queue_snapshot_from_pending_tasks():
    pending = [_task(6), _task(7), _task(8)]
    snapshot = build_task_queue_snapshot(
        pending,
        current_task_id=6,
        completed_ids=[4, 5],
        upcoming_cap=2,
    )
    assert snapshot.completed_ids == (4, 5)
    assert snapshot.current_task_id == 6
    assert snapshot.pending_ids == (6, 7, 8)
    assert snapshot.total_pending == 3
    assert snapshot.upcoming_cap == 2


def test_format_queue_strip_line_matches_expected_layout():
    strip = TaskQueueStripState()
    strip.apply_snapshot(
        build_task_queue_snapshot(
            [_task(index) for index in range(6, 18)],
            current_task_id=6,
            completed_ids=[4, 5],
            upcoming_cap=3,
        )
    )
    assert (
        format_queue_strip_line(strip)
        == "Queue: [✓4] [✓5] [●6] [7] [8] [9] … (12 pending)"
    )


def test_format_queue_strip_line_respects_upcoming_cap():
    strip = TaskQueueStripState()
    strip.apply_snapshot(
        build_task_queue_snapshot(
            [_task(1), _task(2), _task(3), _task(4), _task(5)],
            current_task_id=1,
            upcoming_cap=2,
        )
    )
    assert format_queue_strip_line(strip) == "Queue: [●1] [2] [3] … (5 pending)"


def test_task_queue_strip_mark_completed_advances_display():
    strip = TaskQueueStripState()
    strip.apply_snapshot(
        build_task_queue_snapshot([_task(6), _task(7), _task(8)], current_task_id=6)
    )
    strip.mark_task_completed(6)
    assert strip.completed_ids == [6]
    assert strip.current_task_id is None
    assert strip.pending_ids == [7, 8]
    assert strip.total_pending == 2
    assert format_queue_strip_line(strip) == "Queue: [✓6] [7] [8] (2 pending)"


def test_rich_logger_applies_queue_snapshot_at_cycle_start():
    state = RunDashboardState()
    logger = RichCycleLogger(state=state)
    snapshot = build_task_queue_snapshot([_task(6), _task(7)], current_task_id=6)

    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=2,
        next_task_id=6,
        next_task_title="Queue strip",
        model_id="composer-2.5",
        handover_task_id=6,
        queue_snapshot=snapshot,
    )

    assert state.queue_strip.current_task_id == 6
    assert state.queue_strip.pending_ids == [6, 7]
    assert format_queue_strip_line(state.queue_strip) == "Queue: [●6] [7] (2 pending)"


def test_rich_logger_marks_queue_strip_complete_on_verify():
    state = RunDashboardState()
    logger = RichCycleLogger(state=state)
    snapshot = build_task_queue_snapshot([_task(6), _task(7)], current_task_id=6)
    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=1,
        next_task_id=6,
        next_task_title="Queue strip",
        model_id="composer-2.5",
        handover_task_id=6,
        queue_snapshot=snapshot,
    )

    logger.log_verification_result(
        cycle_number=1,
        total_cycles=1,
        result="passed",
        before_task_id=6,
        after_task_id=7,
        before_hash="a" * 64,
        after_hash="b" * 64,
    )

    assert state.queue_strip.completed_ids == [6]
    assert state.queue_strip.current_task_id is None
    assert format_queue_strip_line(state.queue_strip) == "Queue: [✓6] [7] (1 pending)"


def test_render_dashboard_includes_queue_strip():
    from rich.console import Console

    state = RunDashboardState(
        cycle_number=1,
        total_cycles=3,
        next_task_id=6,
        next_task_title="Queue strip",
        model_id="composer-2.5",
        phase="implementation",
    )
    state.queue_strip.apply_snapshot(
        build_task_queue_snapshot([_task(6), _task(7), _task(8)], current_task_id=6)
    )

    console = Console(width=120, record=True)
    console.print(render_dashboard(state, console_width=120))
    rendered_text = console.export_text()

    assert "Queue:" in rendered_text
    assert "(3 pending)" in rendered_text
    assert "7" in rendered_text
    assert "8" in rendered_text


def test_render_queue_strip_section_highlights_current_task():
    from rich.console import Console

    strip = TaskQueueStripState()
    strip.apply_snapshot(
        build_task_queue_snapshot([_task(6), _task(7)], current_task_id=6)
    )
    console = Console(width=80, record=True)
    console.print(render_queue_strip_section(strip))
    rendered_text = console.export_text()

    assert "Queue:" in rendered_text
    assert "6" in rendered_text
    assert "7" in rendered_text


def test_plain_cycle_logger_emits_queue_summary_line(caplog):
    import logging

    from cyclopsctl.logging import CycleLogger

    logger = CycleLogger()
    snapshot = TaskQueueSnapshot(
        completed_ids=(4, 5),
        current_task_id=6,
        pending_ids=tuple(range(6, 18)),
        total_pending=12,
        upcoming_cap=3,
    )

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        logger.log_cycle_start(
            cycle_number=1,
            total_cycles=3,
            next_task_id=6,
            next_task_title="Queue strip",
            model_id="composer-2.5",
            handover_task_id=6,
            queue_snapshot=snapshot,
        )

    assert "Queue: [✓4] [✓5] [●6] [7] [8] [9] … (12 pending)" in caplog.text
