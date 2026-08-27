"""Tests for task 10: selective retry on transient SDK failures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from cursor_sdk import CursorAgentError, ModelSelection, RunResult

from cyclopsctl.config import build_run_config
from cyclopsctl.loop import run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.runner import AgentRunError, RunFailureKind, STARTUP_EXIT_CODE
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.session import EMPTY_RUN_ERROR_CONTINUE_PROMPT, CycleSession
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.verify import HandoverVerificationError


class _FakeRun:
    def __init__(self, run_id: str, agent_id: str) -> None:
        self.id = run_id
        self.agent_id = agent_id

    def wait(self) -> RunResult:
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


class _TransientThenSuccessAgent(_FakeAgent):
    _transient_failures_remaining = 1

    def send(self, prompt: str) -> _FakeRun:
        if _TransientThenSuccessAgent._transient_failures_remaining > 0:
            _TransientThenSuccessAgent._transient_failures_remaining -= 1
            self.sent.append(prompt)
            raise CursorAgentError("network timeout", is_retryable=True)
        return super().send(prompt)


class _AuthFailureAgent(_FakeAgent):
    def send(self, prompt: str) -> _FakeRun:
        raise CursorAgentError("auth failed", is_retryable=False)


class _RunErrorOnceAgent(_FakeAgent):
    """First run across all instances errors with an empty SDK result."""

    _error_runs_remaining = 1

    def send(self, prompt: str) -> _FakeRun:
        run = super().send(prompt)
        if _RunErrorOnceAgent._error_runs_remaining > 0:
            _RunErrorOnceAgent._error_runs_remaining -= 1
            run.wait = lambda: RunResult(
                id=run.id,
                agent_id=run.agent_id,
                status="error",
            )
        return run


class _ProductiveDropAgent(_FakeAgent):
    """Every send errors with last-prose context from a mid-suite drop."""

    def send(self, prompt: str) -> _FakeRun:
        run = super().send(prompt)

        def conversation_json() -> str:
            return (
                '[{"text": "Running the phase 13 integration test '
                'and the full cargo test suite."}]'
            )

        run.conversation_json = conversation_json
        run.wait = lambda: RunResult(
            id=run.id,
            agent_id=run.agent_id,
            status="error",
        )
        return run


class _DetailedRunErrorAgent(_FakeAgent):
    """Run errors with an SDK-reported detail (agent logical failure)."""

    def send(self, prompt: str) -> _FakeRun:
        run = super().send(prompt)
        if len(self.sent) == 1:
            run.wait = lambda: RunResult(
                id=run.id,
                agent_id=run.agent_id,
                status="error",
                result="agent reported: tests failed",
            )
        return run


@pytest.fixture(autouse=True)
def _reset_agent_counter():
    _FakeAgent._counter = 0
    _TransientThenSuccessAgent._transient_failures_remaining = 1
    _RunErrorOnceAgent._error_runs_remaining = 1
    yield
    _FakeAgent._counter = 0
    _TransientThenSuccessAgent._transient_failures_remaining = 1
    _RunErrorOnceAgent._error_runs_remaining = 1


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "prompts" / "first.md").write_text("# First-run prompt\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 8\n\nImplement task 8.\n",
        encoding="utf-8",
    )
    (root / "update-handover-prompt.md").write_text(
        "# Update\n\nMark done and advance handover.\n",
        encoding="utf-8",
    )
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 8, "complexityScore": 8}]}',
        encoding="utf-8",
    )
    from tests.conftest import seed_native_tasks
    seed_native_tasks(root)
    return root.resolve()


def _config(project_tree: Path, **extra) -> object:
    return build_run_config(
        {
            "cycles": 1,
            "project_root": project_tree,
            "first_prompt": project_tree / "prompts" / "first.md",
            "current_handover": project_tree / "current-handover-prompt.md",
            "update_handover": project_tree / "update-handover-prompt.md",
            "task_source": "handover",
            **extra,
        }
    )


def _router() -> ModelRouter:
    return ModelRouter(
        default_model=COMPOSER_MODEL_ID,
        complexity_report=ComplexityReport(scores={8: 8}),
        capabilities=ModelCapabilities(
            composer=ModelSelection(id=COMPOSER_MODEL_ID),
            composer_standard=ModelSelection(id=COMPOSER_MODEL_ID),
            opus_available=False,
            opus=None,
        ),
    )


def _next_task(task_id: int = 8) -> NextTaskLookup:
    return NextTaskLookup.from_task(
        NextTaskResult(
            task_id=str(task_id),
            title=f"Task {task_id}",
            status="pending",
            priority="high",
            complexity=8,
            tag="master",
        )
    )


def _make_session_factory(
    project_tree: Path,
    *,
    on_update: Callable[[str], None] | None = None,
    agent_factory: Callable[[], _FakeAgent] | None = None,
) -> Callable[..., CycleSession]:
    factory = agent_factory or _FakeAgent

    class _Session(CycleSession):
        def run_update(self, prompt: str):
            result = super().run_update(prompt)
            if on_update is not None:
                on_update(prompt)
            return result

    def session_factory(**kwargs) -> CycleSession:
        return _Session(
            project_root=project_tree,
            model=kwargs["model"],
            create_agent=lambda **_kw: factory(),
        )

    return session_factory


def test_run_cycles_retries_transient_failure_and_completes(project_tree: Path):
    cfg = _config(
        project_tree,
        retry_on="transient",
        retry_max_attempts=3,
        retry_backoff_seconds=(5, 15),
    )
    sleeps: list[float] = []
    warnings: list[dict[str, object]] = []

    class _CaptureLogger:
        def log_warning(self, message: str, **fields: object) -> None:
            warnings.append({"message": message, **fields})

        def log_cycle_start(self, **_fields: object) -> None:
            return None

        def log_run_complete(self, **_fields: object) -> None:
            return None

        def log_handover_snapshot(self, **_fields: object) -> None:
            return None

        def log_verification_result(self, **_fields: object) -> None:
            return None

        def log_cycle(self, _record: object) -> None:
            return None

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(),
        router=_router(),
        session_factory=_make_session_factory(
            project_tree,
            on_update=on_update,
            agent_factory=_TransientThenSuccessAgent,
        ),
        cycle_logger=_CaptureLogger(),
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result.completed_cycles == 1
    assert sleeps == [5]
    retry_warnings = [
        w for w in warnings if w["message"] == "Transient agent failure; retrying cycle"
    ]
    assert len(retry_warnings) == 1
    assert retry_warnings[0]["attempt"] == 1
    assert retry_warnings[0]["max_attempts"] == 3
    assert retry_warnings[0]["retry_delay_seconds"] == 5


def test_run_cycles_does_not_retry_auth_failure(project_tree: Path):
    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)
    sleeps: list[float] = []

    with pytest.raises(AgentRunError) as exc_info:
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                agent_factory=_AuthFailureAgent,
            ),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

    assert exc_info.value.kind == RunFailureKind.STARTUP
    assert exc_info.value.exit_code == STARTUP_EXIT_CODE
    assert sleeps == []


def test_run_cycles_continues_same_agent_on_empty_run_error(project_tree: Path):
    """Empty-detail run errors recover on the same agent before a cycle retry."""
    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)
    sleeps: list[float] = []
    created: list[_FakeAgent] = []

    class _TrackingRunErrorOnce(_RunErrorOnceAgent):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(),
        router=_router(),
        session_factory=_make_session_factory(
            project_tree,
            on_update=on_update,
            agent_factory=_TrackingRunErrorOnce,
        ),
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result.completed_cycles == 1
    assert sleeps == []
    assert len(created) == 1
    assert created[0].sent[1] == EMPTY_RUN_ERROR_CONTINUE_PROMPT


def test_run_cycles_does_not_fresh_retry_productive_run_drop(project_tree: Path):
    """Mid-suite empty-detail drops must not start a second full implementation."""
    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)
    sleeps: list[float] = []

    with pytest.raises(AgentRunError) as exc_info:
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                agent_factory=_ProductiveDropAgent,
            ),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

    err = exc_info.value
    assert err.kind == RunFailureKind.RUN
    assert err.same_agent_continued is True
    assert "full cargo test suite" in (err.diagnostic_detail or "")
    assert sleeps == []


def test_run_cycles_does_not_retry_run_error_with_detail(project_tree: Path):
    """A run error carrying an SDK detail is a logical failure; never retried."""
    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)
    sleeps: list[float] = []

    with pytest.raises(AgentRunError) as exc_info:
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                agent_factory=_DetailedRunErrorAgent,
            ),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

    assert exc_info.value.kind == RunFailureKind.RUN
    assert exc_info.value.result_detail == "agent reported: tests failed"
    assert sleeps == []


def test_run_cycles_does_not_retry_verification_failure(project_tree: Path):
    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)
    sleeps: list[float] = []

    with pytest.raises(HandoverVerificationError, match="unchanged"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(),
            router=_router(),
            session_factory=_make_session_factory(project_tree),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

    assert sleeps == []


class _AlwaysTransientAgent(_FakeAgent):
    def send(self, prompt: str) -> _FakeRun:
        self.sent.append(prompt)
        raise CursorAgentError("network timeout", is_retryable=True)


class _FakeBridge:
    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.process = type("P", (), {"pid": 1234})()

    def is_alive(self) -> bool:
        return self.alive


class _NullLogger:
    def __init__(self) -> None:
        self.warnings: list[dict[str, object]] = []

    def log_warning(self, message: str, **fields: object) -> None:
        self.warnings.append({"message": message, **fields})


def _bridge_connect_error() -> AgentRunError:
    return AgentRunError(
        "Agent startup failed: Bridge request failed: ConnectError: "
        "[WinError 10061] No connection could be made because the target "
        "machine actively refused it",
        kind=RunFailureKind.STARTUP,
        exit_code=STARTUP_EXIT_CODE,
        phase="startup",
    )


def test_is_bridge_connect_failure_matches_winerror_10061():
    from cyclopsctl.loop import _is_bridge_connect_failure

    assert _is_bridge_connect_failure(_bridge_connect_error()) is True


def test_is_bridge_connect_failure_ignores_other_errors():
    from cyclopsctl.loop import _is_bridge_connect_failure

    exc = AgentRunError(
        "implementation wait failed: network timeout",
        kind=RunFailureKind.STARTUP,
        exit_code=STARTUP_EXIT_CODE,
        phase="implementation",
    )
    assert _is_bridge_connect_failure(exc) is False


def test_recover_bridge_before_retry_relaunches_dead_bridge(
    project_tree: Path, monkeypatch
):
    from cyclopsctl import loop

    recovered: list[Path] = []
    monkeypatch.setattr(
        loop, "active_managed_bridge", lambda: _FakeBridge(alive=False)
    )
    monkeypatch.setattr(
        loop, "recover_managed_bridge", lambda root: recovered.append(root)
    )
    log = _NullLogger()

    loop._recover_bridge_before_retry(
        _config(project_tree),
        exc=AgentRunError(
            "implementation wait failed: network timeout",
            kind=RunFailureKind.STARTUP,
            exit_code=STARTUP_EXIT_CODE,
        ),
        log=log,
        cycle_number=1,
    )

    assert recovered == [project_tree]
    assert log.warnings[0]["message"] == (
        "cursor-sdk-bridge is down; relaunching before retry"
    )


def test_recover_bridge_before_retry_relaunches_on_connect_refused(
    project_tree: Path, monkeypatch
):
    """Even an alive-but-unreachable (frozen) bridge is replaced."""
    from cyclopsctl import loop

    recovered: list[Path] = []
    monkeypatch.setattr(
        loop, "active_managed_bridge", lambda: _FakeBridge(alive=True)
    )
    monkeypatch.setattr(
        loop, "recover_managed_bridge", lambda root: recovered.append(root)
    )

    loop._recover_bridge_before_retry(
        _config(project_tree),
        exc=_bridge_connect_error(),
        log=_NullLogger(),
        cycle_number=1,
    )

    assert recovered == [project_tree]


def test_recover_bridge_before_retry_skips_healthy_bridge(
    project_tree: Path, monkeypatch
):
    from cyclopsctl import loop

    recovered: list[Path] = []
    monkeypatch.setattr(
        loop, "active_managed_bridge", lambda: _FakeBridge(alive=True)
    )
    monkeypatch.setattr(
        loop, "recover_managed_bridge", lambda root: recovered.append(root)
    )

    loop._recover_bridge_before_retry(
        _config(project_tree),
        exc=AgentRunError(
            "implementation wait failed: network timeout",
            kind=RunFailureKind.STARTUP,
            exit_code=STARTUP_EXIT_CODE,
        ),
        log=_NullLogger(),
        cycle_number=1,
    )

    assert recovered == []


def test_recover_bridge_before_retry_noop_without_managed_bridge(
    project_tree: Path, monkeypatch
):
    from cyclopsctl import loop

    monkeypatch.setattr(loop, "active_managed_bridge", lambda: None)
    monkeypatch.setattr(
        loop,
        "recover_managed_bridge",
        lambda _root: pytest.fail("must not recover"),
    )

    loop._recover_bridge_before_retry(
        _config(project_tree),
        exc=_bridge_connect_error(),
        log=_NullLogger(),
        cycle_number=1,
    )


def test_run_cycles_recovers_bridge_during_transient_retry(
    project_tree: Path, monkeypatch
):
    """End to end: dead bridge is relaunched before the retry attempt."""
    from cyclopsctl import loop

    recovered: list[Path] = []
    monkeypatch.setattr(
        loop, "active_managed_bridge", lambda: _FakeBridge(alive=False)
    )
    monkeypatch.setattr(
        loop, "recover_managed_bridge", lambda root: recovered.append(root)
    )

    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(),
        router=_router(),
        session_factory=_make_session_factory(
            project_tree,
            on_update=on_update,
            agent_factory=_TransientThenSuccessAgent,
        ),
        sleep_fn=lambda _seconds: None,
    )

    assert result.completed_cycles == 1
    assert recovered == [project_tree]


def test_run_cycles_exhausts_retry_budget(project_tree: Path):
    cfg = _config(
        project_tree,
        retry_on="transient",
        retry_max_attempts=2,
        retry_backoff_seconds=(7,),
    )
    sleeps: list[float] = []

    with pytest.raises(AgentRunError):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                agent_factory=_AlwaysTransientAgent,
            ),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

    assert sleeps == [7]
