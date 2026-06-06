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
from cyclopsctl.session import CycleSession
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


class _ErrorAgent(_FakeAgent):
    def send(self, prompt: str) -> _FakeRun:
        run = super().send(prompt)
        if len(self.sent) == 1:
            run.wait = lambda: RunResult(
                id=run.id,
                agent_id=run.agent_id,
                status="error",
            )
        return run


@pytest.fixture(autouse=True)
def _reset_agent_counter():
    _FakeAgent._counter = 0
    _TransientThenSuccessAgent._transient_failures_remaining = 1
    yield
    _FakeAgent._counter = 0
    _TransientThenSuccessAgent._transient_failures_remaining = 1


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


def test_run_cycles_does_not_retry_agent_logical_error(project_tree: Path):
    cfg = _config(project_tree, retry_on="transient", retry_max_attempts=3)
    sleeps: list[float] = []

    with pytest.raises(AgentRunError):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                agent_factory=_ErrorAgent,
            ),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

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
