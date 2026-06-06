"""Tests for task 7: per-cycle agent session lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest
from cursor_sdk import ModelSelection

from cyclopsctl.runner import AgentRunError, RunFailureKind, SendRunResult
from cyclopsctl.session import CycleSession, SessionError


class _FakeRun:
    def __init__(self, run_id: str, agent_id: str) -> None:
        self.id = run_id
        self.agent_id = agent_id

    def wait(self):
        from cursor_sdk import RunResult

        return RunResult(id=self.id, agent_id=self.agent_id, status="finished")


class _FakeAgent:
    _counter = 0

    def __init__(self) -> None:
        _FakeAgent._counter += 1
        self.agent_id = f"agent-{_FakeAgent._counter}"
        self.sent: list[str] = []
        self.closed = False

    def send(self, prompt: str) -> _FakeRun:
        self.sent.append(prompt)
        return _FakeRun(f"{self.agent_id}-run-{len(self.sent)}", self.agent_id)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_agent_counter():
    _FakeAgent._counter = 0
    yield
    _FakeAgent._counter = 0


def _session(tmp_path: Path) -> CycleSession:
    return CycleSession(
        project_root=tmp_path,
        model=ModelSelection(id="composer-2.5"),
        create_agent=lambda **_kwargs: _FakeAgent(),
    )


def test_cycle_session_new_agent_per_implementation_cycle(tmp_path: Path):
    session_one = _session(tmp_path)
    session_two = _session(tmp_path)

    impl_one = session_one.start_implementation("cycle-1 impl")
    impl_two = session_two.start_implementation("cycle-2 impl")

    assert impl_one.agent_id == "agent-1"
    assert impl_two.agent_id == "agent-2"
    assert impl_one.agent_id != impl_two.agent_id


def test_cycle_session_reuses_same_agent_for_update(tmp_path: Path):
    session = _session(tmp_path)

    impl = session.start_implementation("do work")
    update = session.run_update("update docs")

    assert impl.agent_id == update.agent_id == "agent-1"
    assert impl.run_id == "agent-1-run-1"
    assert update.run_id == "agent-1-run-2"


def test_cycle_session_passes_prompts_unchanged(tmp_path: Path):
    session = _session(tmp_path)
    impl_prompt = "# Task\n\nImplement only task 6."
    update_prompt = "# Update\n\nMark done."

    session.start_implementation(impl_prompt)
    session.run_update(update_prompt)

    agent = session._agent
    assert agent is not None
    assert agent.sent == [impl_prompt, update_prompt]


def test_cycle_session_update_before_implementation_raises(tmp_path: Path):
    session = _session(tmp_path)

    with pytest.raises(SessionError, match="before implementation"):
        session.run_update("too early")


def test_cycle_session_close_disposes_agent(tmp_path: Path):
    session = _session(tmp_path)
    session.start_implementation("impl")

    agent = session._agent
    assert agent is not None
    session.close()

    assert agent.closed is True
    assert session.agent_id is None


def test_cycle_session_context_manager_closes_agent(tmp_path: Path):
    with _session(tmp_path) as session:
        session.start_implementation("impl")
        agent = session._agent
        assert agent is not None

    assert agent.closed is True


def test_cycle_session_restart_implementation_creates_fresh_agent(tmp_path: Path):
    session = _session(tmp_path)

    first = session.start_implementation("first impl")
    first_agent = session._agent
    assert first_agent is not None

    second = session.start_implementation("second impl")

    assert second.agent_id == "agent-2"
    assert first.agent_id != second.agent_id
    assert first_agent.closed is True


def test_cycle_session_propagates_run_failure(tmp_path: Path):
    class _ErrorAgent(_FakeAgent):
        def send(self, prompt: str) -> _FakeRun:
            run = super().send(prompt)
            run.wait = lambda: __import__("cursor_sdk").RunResult(
                id=run.id,
                agent_id=run.agent_id,
                status="error",
            )
            return run

    session = CycleSession(
        project_root=tmp_path,
        model=ModelSelection(id="composer-2.5"),
        create_agent=lambda **_kwargs: _ErrorAgent(),
    )

    with pytest.raises(AgentRunError) as exc_info:
        session.start_implementation("fail")

    assert exc_info.value.kind == RunFailureKind.RUN
