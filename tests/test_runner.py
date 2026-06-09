"""Tests for task 7: Cursor SDK run execution wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest
from cursor_sdk import CursorAgentError, ModelSelection, RunResult

from cyclopsctl.runner import (
    RUN_FAILURE_EXIT_CODE,
    STARTUP_EXIT_CODE,
    AgentRunError,
    RunFailureKind,
    create_local_agent,
    extract_run_error_detail,
    is_opus_billing_failure,
    is_transient_agent_failure,
    is_transient_cursor_agent_error,
    retry_delay_seconds,
    send_and_wait,
    should_retry_transient_failure,
    should_retry_with_composer_after_opus_failure,
)


class _FakeRun:
    def __init__(
        self,
        run_id: str,
        *,
        agent_id: str = "agent-1",
        status: str = "finished",
        result: str = "done",
        wait_error: CursorAgentError | None = None,
    ) -> None:
        self.id = run_id
        self.agent_id = agent_id
        self._status = status
        self._result = result
        self._wait_error = wait_error

    def wait(self) -> RunResult:
        if self._wait_error is not None:
            raise self._wait_error
        return RunResult(
            id=self.id,
            agent_id=self.agent_id,
            status=self._status,
            result=self._result,
        )


class _FakeAgent:
    def __init__(self, agent_id: str = "agent-1") -> None:
        self.agent_id = agent_id
        self.sent: list[str] = []
        self.closed = False
        self._send_error: CursorAgentError | None = None
        self._run_status = "finished"
        self._run_result = "done"

    def send(self, prompt: str) -> _FakeRun:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(prompt)
        return _FakeRun(
            f"run-{len(self.sent)}",
            agent_id=self.agent_id,
            status=self._run_status,
            result=self._run_result,
        )

    def close(self) -> None:
        self.closed = True


def test_create_local_agent_passes_model_and_cwd(tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_create(**kwargs: object) -> _FakeAgent:
        captured.update(kwargs)
        return _FakeAgent("agent-new")

    model = ModelSelection(id="composer-2.5")
    agent = create_local_agent(
        model=model,
        project_root=tmp_path,
        api_key="test-key",
        create_agent=fake_create,
    )

    assert agent.agent_id == "agent-new"
    assert captured["model"] == model
    assert captured["api_key"] == "test-key"
    assert captured["local"].cwd == str(tmp_path)


def test_create_local_agent_startup_failure():
    def fake_create(**_kwargs: object) -> _FakeAgent:
        raise CursorAgentError("auth failed", is_retryable=False)

    with pytest.raises(AgentRunError) as exc_info:
        create_local_agent(
            model=ModelSelection(id="composer-2.5"),
            project_root=Path("/project"),
            create_agent=fake_create,
        )

    err = exc_info.value
    assert err.kind == RunFailureKind.STARTUP
    assert err.exit_code == STARTUP_EXIT_CODE


def test_send_and_wait_returns_metadata():
    agent = _FakeAgent()
    result = send_and_wait(agent, "implement task")

    assert result.agent_id == "agent-1"
    assert result.run_id == "run-1"
    assert result.status == "finished"
    assert agent.sent == ["implement task"]


def test_send_and_wait_startup_failure_on_send():
    agent = _FakeAgent()
    agent._send_error = CursorAgentError("network down")

    with pytest.raises(AgentRunError) as exc_info:
        send_and_wait(agent, "prompt")

    err = exc_info.value
    assert err.kind == RunFailureKind.STARTUP
    assert err.exit_code == STARTUP_EXIT_CODE
    assert err.agent_id == "agent-1"


def test_send_and_wait_startup_failure_on_wait():
    agent = _FakeAgent()

    def failing_wait(_run: _FakeRun) -> RunResult:
        raise CursorAgentError("wait rpc failed")

    with pytest.raises(AgentRunError) as exc_info:
        send_and_wait(agent, "prompt", wait_fn=failing_wait)

    err = exc_info.value
    assert err.kind == RunFailureKind.STARTUP
    assert err.run_id == "run-1"


def test_send_and_wait_run_failure_on_error_status():
    agent = _FakeAgent()
    agent._run_status = "error"

    with pytest.raises(AgentRunError) as exc_info:
        send_and_wait(agent, "prompt")

    err = exc_info.value
    assert err.kind == RunFailureKind.RUN
    assert err.exit_code == RUN_FAILURE_EXIT_CODE
    assert err.run_id == "run-1"


def test_extract_run_error_detail_prefers_error_keys():
    class _ConversationRun:
        def conversation_json(self) -> str:
            return (
                '[{"turn": {"steps": [{"text": "working on it"},'
                ' {"errorMessage": "ConnectError: stream closed"}]}}]'
            )

    assert (
        extract_run_error_detail(_ConversationRun())
        == "ConnectError: stream closed"
    )


def test_extract_run_error_detail_falls_back_to_last_text():
    class _ConversationRun:
        def conversation_json(self) -> str:
            return '[{"text": "first"}, {"text": "last words"}]'

    assert (
        extract_run_error_detail(_ConversationRun())
        == "last agent output: last words"
    )


def test_extract_run_error_detail_never_raises():
    class _BrokenRun:
        def conversation_json(self) -> str:
            raise RuntimeError("conversation unavailable")

    assert extract_run_error_detail(_BrokenRun()) == ""
    assert extract_run_error_detail(object()) == ""


def test_send_and_wait_attaches_diagnostic_detail_on_empty_error_result():
    agent = _FakeAgent()
    agent._run_status = "error"
    agent._run_result = ""

    def send_with_conversation(target_agent: _FakeAgent, prompt: str) -> _FakeRun:
        run = target_agent.send(prompt)
        run.conversation_json = lambda: '[{"error": "model connection dropped"}]'
        return run

    with pytest.raises(AgentRunError) as exc_info:
        send_and_wait(agent, "prompt", send_fn=send_with_conversation)

    err = exc_info.value
    assert err.result_detail is None
    assert err.diagnostic_detail == "model connection dropped"
    assert "model connection dropped" in str(err)
    # Diagnostic context must not affect retry classification.
    assert is_transient_agent_failure(err) is True


def test_send_and_wait_run_failure_includes_result_detail():
    agent = _FakeAgent()
    agent._run_status = "error"
    agent._run_result = "Your credit balance is too low to access the Opus API"

    with pytest.raises(AgentRunError) as exc_info:
        send_and_wait(agent, "prompt")

    err = exc_info.value
    assert "credit balance is too low" in str(err)
    assert err.result_detail == agent._run_result


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Your credit balance is too low", True),
        ("Insufficient credits for this model", True),
        ("network timeout", False),
        ("implementation run failed", False),
    ],
)
def test_is_opus_billing_failure(message: str, expected: bool):
    assert is_opus_billing_failure(message) is expected


def test_should_retry_with_composer_after_opus_failure():
    billing = AgentRunError(
        "implementation run failed: agent=a run=r: credit balance is too low",
        kind=RunFailureKind.RUN,
        exit_code=RUN_FAILURE_EXIT_CODE,
        result_detail="Your credit balance is too low",
    )
    startup = AgentRunError(
        "send failed",
        kind=RunFailureKind.STARTUP,
        exit_code=STARTUP_EXIT_CODE,
    )
    assert should_retry_with_composer_after_opus_failure(billing) is True
    assert should_retry_with_composer_after_opus_failure(startup) is False


@pytest.mark.parametrize(
    ("message", "is_retryable", "expected"),
    [
        ("network timeout", True, True),
        ("connection reset by peer", False, True),
        ("auth failed", False, False),
        ("invalid api key", False, False),
        ("rpc unavailable", False, True),
    ],
)
def test_is_transient_cursor_agent_error(message: str, is_retryable: bool, expected: bool):
    exc = CursorAgentError(message, is_retryable=is_retryable)
    assert is_transient_cursor_agent_error(exc) is expected


def test_is_transient_agent_failure_startup_with_transient_cause():
    cause = CursorAgentError("network down", is_retryable=True)
    err = AgentRunError(
        "send failed",
        kind=RunFailureKind.STARTUP,
        exit_code=STARTUP_EXIT_CODE,
        cause=cause,
    )
    assert is_transient_agent_failure(err) is True


def test_is_transient_agent_failure_accepts_run_kind_without_detail():
    """Errored run with no SDK detail = upstream blip; retryable."""
    err = AgentRunError(
        "run failed",
        kind=RunFailureKind.RUN,
        exit_code=RUN_FAILURE_EXIT_CODE,
    )
    assert is_transient_agent_failure(err) is True


def test_is_transient_agent_failure_rejects_run_kind_with_detail():
    err = AgentRunError(
        "run failed: tests failed",
        kind=RunFailureKind.RUN,
        exit_code=RUN_FAILURE_EXIT_CODE,
        result_detail="tests failed",
    )
    assert is_transient_agent_failure(err) is False


def test_is_transient_agent_failure_rejects_non_agent_errors():
    assert is_transient_agent_failure(ValueError("network down")) is False


def test_retry_delay_seconds_uses_backoff_sequence():
    assert retry_delay_seconds((5, 15), failed_attempt=1) == 5
    assert retry_delay_seconds((5, 15), failed_attempt=2) == 15
    assert retry_delay_seconds((5, 15), failed_attempt=3) == 15


def test_should_retry_transient_failure_respects_attempt_budget():
    cause = CursorAgentError("network down", is_retryable=True)
    err = AgentRunError(
        "send failed",
        kind=RunFailureKind.STARTUP,
        exit_code=STARTUP_EXIT_CODE,
        cause=cause,
    )
    assert should_retry_transient_failure(
        err,
        retry_on_transient=True,
        failed_attempt=1,
        max_attempts=3,
    )
    assert not should_retry_transient_failure(
        err,
        retry_on_transient=True,
        failed_attempt=3,
        max_attempts=3,
    )
    assert not should_retry_transient_failure(
        err,
        retry_on_transient=False,
        failed_attempt=1,
        max_attempts=3,
    )
