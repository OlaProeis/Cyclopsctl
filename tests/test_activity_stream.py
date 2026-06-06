"""Tests for live agent activity streaming into the Rich dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from cursor_sdk import RunResult

from cyclopsctl.runner import (
    BootstrapProgressWriter,
    bootstrap_progress_callback,
    consume_run_activity,
    format_activity_from_event,
    format_activity_line,
    is_prose_activity_line,
    merge_prose_activity,
    parse_todo_write_from_event,
    run_has_activity_stream,
    send_and_wait,
    truncate_activity_text,
)
from cyclopsctl.session import CycleSession
from cyclopsctl.tui import (
    DEFAULT_ACTIVITY_BUFFER_SIZE,
    AgentPlanItemStatus,
    AgentPlanState,
    RichCycleLogger,
    RunDashboardState,
    activity_callback_for_logger,
    activity_panel_content_width,
    format_activity_for_display,
    plan_callback_for_logger,
)
from cyclopsctl.logging import CycleLogger


@dataclass(frozen=True)
class _ToolMessage:
    type: str = "tool_call"
    name: str = ""
    args: Any = None
    result: Any = None


@dataclass(frozen=True)
class _StreamEvent:
    sdk_message: Any | None = None
    interaction_update: Any | None = None


@dataclass(frozen=True)
class _ToolCallUpdate:
    type: str
    tool_call: dict[str, Any]


class _StreamingRun:
    def __init__(
        self,
        run_id: str,
        *,
        events: list[Any] | None = None,
        status: str = "finished",
        stream_error: Exception | None = None,
    ) -> None:
        self.id = run_id
        self.agent_id = "agent-1"
        self._events = list(events or [])
        self._status = status
        self._stream_error = stream_error
        self.waited = False

    def stream(self):
        if self._stream_error is not None:
            raise self._stream_error
        yield from self._events

    def wait(self) -> RunResult:
        self.waited = True
        return RunResult(
            id=self.id,
            agent_id=self.agent_id,
            status=self._status,
        )


class _PlainRun:
    def __init__(self, run_id: str) -> None:
        self.id = run_id
        self.agent_id = "agent-1"

    def wait(self) -> RunResult:
        return RunResult(id=self.id, agent_id=self.agent_id, status="finished")


class _StreamingAgent:
    def __init__(self, events: list[Any]) -> None:
        self.agent_id = "agent-1"
        self._events = events

    def send(self, prompt: str) -> _StreamingRun:
        return _StreamingRun("run-1", events=self._events)


class _PlainAgent:
    def __init__(self) -> None:
        self.agent_id = "agent-1"

    def send(self, prompt: str) -> _PlainRun:
        return _PlainRun("run-1")


def test_truncate_activity_text_adds_ellipsis():
    text = "x" * 80
    truncated = truncate_activity_text(text, max_len=20)
    assert len(truncated) == 20
    assert truncated.endswith("…")


def test_format_activity_line_joins_tool_file_and_summary():
    line = format_activity_line(
        tool_name="Read",
        file_path="src/cyclopsctl/tui.py",
        summary="Inspect dashboard rendering",
    )
    assert line == "Read · src/cyclopsctl/tui.py · Inspect dashboard rendering"


def test_format_activity_from_tool_call_message():
    message = _ToolMessage(
        name="Grep",
        args={"path": "src", "pattern": "append_activity"},
    )
    line = format_activity_from_event(message)
    assert line == "Grep · src · append_activity"


def test_format_activity_from_interaction_update():
    event = _StreamEvent(
        interaction_update=_ToolCallUpdate(
            type="tool-call-started",
            tool_call={
                "name": "Shell",
                "args": {"command": "python -m pytest -v"},
            },
        )
    )
    line = format_activity_from_event(event)
    assert line == "Shell · python -m pytest -v"


def test_consume_run_activity_captures_streamed_lines():
    events = [
        _ToolMessage(name="Read", args={"path": "runner.py"}),
        _ToolMessage(name="Write", args={"path": "tui.py", "contents": "updated"}),
    ]
    run = _StreamingRun("run-1", events=events)
    captured: list[str] = []

    consume_run_activity(run, captured.append)

    assert captured == ["Read · runner.py", "Write · tui.py"]


def test_send_and_wait_streams_activity_before_wait():
    events = [_ToolMessage(name="Read", args={"path": "session.py"})]
    agent = _StreamingAgent(events)
    captured: list[str] = []

    result = send_and_wait(agent, "prompt", on_activity=captured.append)

    assert result.status == "finished"
    assert captured == ["Read · session.py"]


def test_send_and_wait_without_stream_falls_back_to_wait_only():
    agent = _PlainAgent()
    captured: list[str] = []

    result = send_and_wait(agent, "prompt", on_activity=captured.append)

    assert result.status == "finished"
    assert captured == []


def test_send_and_wait_stream_errors_fall_back_silently():
    agent = _StreamingAgent([])
    agent.send = lambda _prompt: _StreamingRun(  # type: ignore[method-assign]
        "run-1",
        stream_error=RuntimeError("stream unavailable"),
    )
    captured: list[str] = []

    result = send_and_wait(agent, "prompt", on_activity=captured.append)

    assert result.status == "finished"
    assert captured == []


def test_run_has_activity_stream_detects_supported_surfaces():
    assert run_has_activity_stream(_StreamingRun("run-1")) is True
    assert run_has_activity_stream(_PlainRun("run-1")) is False


@dataclass(frozen=True)
class _AssistantMessage:
    type: str = "assistant"
    message: Any = None


def _assistant_text_event(text: str) -> _AssistantMessage:
    return _AssistantMessage(message={"content": [{"text": text}]})


def test_is_prose_activity_line_distinguishes_tool_rows():
    assert is_prose_activity_line("Inspecting the codebase") is True
    assert is_prose_activity_line("Read · runner.py") is False


def test_bootstrap_progress_writer_coalesces_streaming_prose():
    captured: list[str] = []
    writer = BootstrapProgressWriter(captured.append, emit_threshold=20)

    writer("Testing")
    writer(" complexity")
    writer(" scores")
    writer.flush()

    assert captured == ["Testing complexity scores"]


def test_bootstrap_progress_writer_ignores_token_fragments_until_flush():
    captured: list[str] = []
    writer = BootstrapProgressWriter(captured.append)

    writer("ING")
    writer("WAS")
    writer("RUNNING")
    writer.flush()

    assert captured == ["RUNNING", "ING WAS"]


def test_bootstrap_progress_writer_flushes_on_finished():
    captured: list[str] = []
    writer = BootstrapProgressWriter(captured.append, emit_threshold=200)

    writer('{"tasks":')
    writer('[{"id":1')
    writer("FINISHED")

    assert captured[0].startswith('{"tasks":')
    assert captured[-1] == "FINISHED"


def test_bootstrap_progress_callback_is_writable():
    captured: list[str] = []
    callback = bootstrap_progress_callback(captured.append)
    assert isinstance(callback, BootstrapProgressWriter)


def test_merge_prose_activity_coalesces_word_deltas():
    merged = merge_prose_activity("Implementing", " the")
    assert merged == "Implementing the"
    merged = merge_prose_activity(merged, " activity fix")
    assert merged == "Implementing the activity fix"


def test_merge_prose_activity_handles_cumulative_updates():
    assert merge_prose_activity("Hello", "Hello world") == "Hello world"
    assert merge_prose_activity("Hello world", "Hello") == "Hello world"


def test_prose_stream_deltas_coalesce_into_one_logical_line():
    state = RunDashboardState()
    words = ["Implementing", " the", " recent", " activity", " rendering", " fix"]
    for word in words:
        state.append_activity(word)

    assert len(state.activity_lines) == 1
    assert state.activity_lines[0] == "Implementing the recent activity rendering fix"


def test_prose_coalescing_resets_after_tool_event():
    state = RunDashboardState()
    state.append_activity("Starting")
    state.append_activity(" analysis")
    state.append_activity("Read · runner.py")
    state.append_activity("Updating")
    state.append_activity(" dashboard")

    assert state.activity_lines == [
        "Starting analysis",
        "Read · runner.py",
        "Updating dashboard",
    ]


def test_consume_run_activity_coalesces_assistant_word_deltas():
    events = [
        _assistant_text_event("Coalescing"),
        _assistant_text_event(" streaming"),
        _assistant_text_event(" prose"),
    ]
    run = _StreamingRun("run-1", events=events)
    state = RunDashboardState()

    consume_run_activity(run, state.append_activity)

    assert len(state.activity_lines) == 1
    assert state.activity_lines[0] == "Coalescing streaming prose"


def test_dashboard_state_activity_buffer_counts_logical_entries():
    state = RunDashboardState()
    for index in range(DEFAULT_ACTIVITY_BUFFER_SIZE + 3):
        state.append_activity(f"Read · file-{index}.py")

    assert len(state.activity_lines) == DEFAULT_ACTIVITY_BUFFER_SIZE
    assert state.activity_lines[0] == "Read · file-3.py"
    assert state.activity_lines[-1] == f"Read · file-{DEFAULT_ACTIVITY_BUFFER_SIZE + 2}.py"


def test_format_activity_for_display_wraps_prose_to_panel_width():
    sentence = (
        "This is a long assistant sentence that should wrap cleanly across "
        "a few visual lines instead of breaking into one-word ladders."
    )
    wrapped = format_activity_for_display(
        sentence,
        width=activity_panel_content_width(120),
    )
    visual_lines = wrapped.splitlines()

    assert len(visual_lines) <= 3
    assert all(len(line) <= activity_panel_content_width(120) for line in visual_lines)


def test_format_activity_for_display_preserves_tool_truncation():
    tool_line = format_activity_line(
        tool_name="Shell",
        file_path="src/cyclopsctl/runner.py",
        summary="python -m pytest tests/test_activity_stream.py",
    )
    rendered = format_activity_for_display(
        tool_line,
        width=activity_panel_content_width(120),
    )
    assert rendered == tool_line
    assert "…" not in rendered or tool_line.endswith("…")


def test_rich_logger_throttles_activity_refreshes(monkeypatch):
    state = RunDashboardState()
    times = iter([0.0, 0.1, 1.0])
    updates: list[None] = []
    logger = RichCycleLogger(
        state=state,
        on_update=lambda: updates.append(None),
        activity_refresh_interval=0.25,
    )
    monkeypatch.setattr(logger, "_monotonic", lambda: next(times))

    logger.append_activity("Read · first.py")
    logger.append_activity("Write · second.py")
    logger.append_activity("Shell · python -m pytest")

    assert len(updates) == 2  # first at 0.0s, second at 1.0s
    assert state.activity_lines == [
        "Read · first.py",
        "Write · second.py",
        "Shell · python -m pytest",
    ]


def test_activity_callback_for_logger_plain_mode_returns_none():
    assert activity_callback_for_logger(CycleLogger()) is None
    assert activity_callback_for_logger(RichCycleLogger()) is not None


def _todo_write_message(
    todos: list[dict[str, str]],
    *,
    merge: bool = False,
    name: str = "TodoWrite",
) -> _ToolMessage:
    return _ToolMessage(
        name=name,
        args={"todos": todos, "merge": merge},
    )


def test_parse_todo_write_from_tool_call_message():
    message = _todo_write_message(
        [
            {"id": "1", "content": "Explore codebase", "status": "pending"},
            {"id": "2", "content": "Implement feature", "status": "in_progress"},
        ]
    )
    parsed = parse_todo_write_from_event(message)
    assert parsed is not None
    todos, merge = parsed
    assert merge is False
    assert len(todos) == 2
    assert todos[0]["id"] == "1"
    assert todos[1]["status"] == "in_progress"


def test_parse_todo_write_accepts_snake_case_tool_name():
    message = _todo_write_message(
        [{"id": "a", "content": "Task", "status": "pending"}],
        name="todo_write",
        merge=True,
    )
    parsed = parse_todo_write_from_event(message)
    assert parsed is not None
    todos, merge = parsed
    assert merge is True
    assert todos[0]["id"] == "a"


def test_parse_todo_write_from_interaction_update():
    event = _StreamEvent(
        interaction_update=_ToolCallUpdate(
            type="tool-call-started",
            tool_call={
                "name": "TodoWrite",
                "args": {
                    "merge": False,
                    "todos": [
                        {"id": "x", "content": "Run tests", "status": "completed"},
                    ],
                },
            },
        )
    )
    parsed = parse_todo_write_from_event(event)
    assert parsed is not None
    todos, merge = parsed
    assert merge is False
    assert todos[0]["status"] == "completed"


def test_parse_todo_write_ignores_non_todo_tool_calls():
    message = _ToolMessage(name="Read", args={"path": "runner.py"})
    assert parse_todo_write_from_event(message) is None


def test_agent_plan_state_replace_semantics():
    plan = AgentPlanState()
    plan.apply_todo_write(
        [
            {"id": "1", "content": "First", "status": "pending"},
            {"id": "2", "content": "Second", "status": "in_progress"},
        ],
        merge=False,
    )
    plan.apply_todo_write(
        [{"id": "3", "content": "Replaced", "status": "pending"}],
        merge=False,
    )
    assert [item.id for item in plan.items] == ["3"]
    assert plan.items[0].content == "Replaced"


def test_agent_plan_state_merge_updates_by_id_and_order():
    plan = AgentPlanState()
    plan.apply_todo_write(
        [
            {"id": "1", "content": "First", "status": "pending"},
            {"id": "2", "content": "Second", "status": "pending"},
        ],
        merge=False,
    )
    plan.apply_todo_write(
        [
            {"id": "2", "content": "Second", "status": "in_progress"},
            {"id": "3", "content": "Third", "status": "pending"},
        ],
        merge=True,
    )
    assert [item.id for item in plan.items] == ["1", "2", "3"]
    assert plan.items[1].status is AgentPlanItemStatus.IN_PROGRESS
    assert plan.items[2].content == "Third"


def test_consume_run_activity_applies_todo_write_updates():
    events = [
        _todo_write_message(
            [
                {"id": "1", "content": "Read handover", "status": "in_progress"},
                {"id": "2", "content": "Run pytest", "status": "pending"},
            ]
        ),
        _ToolMessage(name="Read", args={"path": "runner.py"}),
        _todo_write_message(
            [{"id": "1", "content": "Read handover", "status": "completed"}],
            merge=True,
        ),
    ]
    run = _StreamingRun("run-1", events=events)
    state = RunDashboardState()

    consume_run_activity(
        run,
        state.append_activity,
        on_plan_update=lambda todos, merge: state.agent_plan.apply_todo_write(
            todos,
            merge=merge,
        ),
    )
    assert len(state.agent_plan.items) == 2
    assert [item.id for item in state.agent_plan.items] == ["2", "1"]
    assert state.agent_plan.items[0].content == "Run pytest"
    assert state.agent_plan.items[1].status is AgentPlanItemStatus.COMPLETED
    assert state.activity_lines == ["TodoWrite", "Read · runner.py", "TodoWrite"]


def test_send_and_wait_streams_plan_updates():
    events = [
        _todo_write_message(
            [{"id": "1", "content": "Implement", "status": "in_progress"}]
        )
    ]
    agent = _StreamingAgent(events)
    state = RunDashboardState()

    send_and_wait(
        agent,
        "prompt",
        on_activity=state.append_activity,
        on_plan_update=lambda todos, merge: state.agent_plan.apply_todo_write(
            todos,
            merge=merge,
        ),
    )

    assert len(state.agent_plan.items) == 1
    assert state.agent_plan.items[0].content == "Implement"


def test_plan_callback_for_logger_plain_mode_returns_none():
    assert plan_callback_for_logger(CycleLogger()) is None
    assert plan_callback_for_logger(RichCycleLogger()) is not None


def test_rich_logger_clears_agent_plan_on_cycle_start():
    state = RunDashboardState()
    logger = RichCycleLogger(state=state)
    state.agent_plan.apply_todo_write(
        [{"id": "1", "content": "Stale", "status": "pending"}],
        merge=False,
    )

    logger.log_cycle_start(
        cycle_number=1,
        total_cycles=1,
        next_task_id=1,
        next_task_title="Fresh cycle",
        model_id="composer-2.5",
        handover_task_id=1,
    )

    assert state.agent_plan.items == []


def test_cycle_session_forwards_activity_callback(tmp_path):
    events = [_ToolMessage(name="Read", args={"path": "loop.py"})]
    captured: list[str] = []

    session = CycleSession(
        project_root=tmp_path,
        model=__import__("cursor_sdk").ModelSelection(id="composer-2.5"),
        create_agent=lambda **_kwargs: _StreamingAgent(events),
        on_activity=captured.append,
    )

    session.start_implementation("implement")

    assert captured == ["Read · loop.py"]
