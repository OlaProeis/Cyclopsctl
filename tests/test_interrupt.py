"""Tests for graceful Ctrl+C handling during cyclopsctl runs (task 7)."""

from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cursor_sdk import ModelSelection, RunResult

from cyclopsctl.cli import main
from cyclopsctl.config import build_run_config
from cyclopsctl.errors import INTERRUPT_EXIT_CODE, exit_code_for
from cyclopsctl.interrupt import RunInterruptController, RunInterruptedError
from cyclopsctl.loop import run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.runner import SendRunResult
from cyclopsctl.session import CycleSession
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult


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


@pytest.fixture(autouse=True)
def _reset_agent_counter():
    _FakeAgent._counter = 0
    yield
    _FakeAgent._counter = 0


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "prompts" / "first.md").write_text("# First-run prompt\n", encoding="utf-8")
    handover = "# Task ID: 7\n\nImplement task 7.\n"
    (root / "current-handover-prompt.md").write_text(handover, encoding="utf-8")
    (root / "update-handover-prompt.md").write_text(
        "# Update\n\nMark done and advance handover.\n",
        encoding="utf-8",
    )
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 7, "complexityScore": 8}]}',
        encoding="utf-8",
    )
    from tests.conftest import seed_native_tasks
    seed_native_tasks(root)
    return root.resolve()


def _config(project_tree: Path, *, cycles: int = 2):
    return build_run_config(
        {
            "cycles": cycles,
            "project_root": project_tree,
            "first_prompt": project_tree / "prompts" / "first.md",
            "current_handover": project_tree / "current-handover-prompt.md",
            "update_handover": project_tree / "update-handover-prompt.md",
            "task_source": "handover",
        }
    )


def _router() -> ModelRouter:
    return ModelRouter(
        default_model=COMPOSER_MODEL_ID,
        complexity_report=ComplexityReport(scores={7: 8, 8: 5}),
        capabilities=ModelCapabilities(
            composer=ModelSelection(id=COMPOSER_MODEL_ID),
            composer_standard=ModelSelection(id=COMPOSER_MODEL_ID),
            opus_available=False,
            opus=None,
        ),
    )


def _next_tasks(*task_ids: int) -> list[NextTaskLookup]:
    return [
        NextTaskLookup.from_task(
            NextTaskResult(
                task_id=str(task_id),
                title=f"Task {task_id}",
                status="pending",
                priority="high",
                complexity=8,
                tag="master",
            )
        )
        for task_id in task_ids
    ]


def test_interrupt_exit_code_mapping():
    exc = RunInterruptedError(cycle_number=1, phase="implementation", agent_id="a1")
    assert exc.exit_code == INTERRUPT_EXIT_CODE
    assert exit_code_for(exc) == 130


def test_run_interrupt_controller_registers_sigint():
    controller = RunInterruptController()
    controller.register()
    try:
        controller.request_stop()
        assert controller.stop_requested is True
    finally:
        controller.restore()


def test_run_cycles_interrupt_closes_session_and_preserves_handover(project_tree: Path):
    cfg = _config(project_tree, cycles=2)
    initial_handover = (project_tree / "current-handover-prompt.md").read_text(encoding="utf-8")
    interrupt = RunInterruptController()
    agents: list[_FakeAgent] = []

    class _InterruptSession(CycleSession):
        def start_implementation(self, prompt: str):
            agent = _FakeAgent()
            agents.append(agent)
            self._agent = agent
            self.implementation = SendRunResult(
                agent_id=agent.agent_id,
                run_id=f"{agent.agent_id}-run-1",
                status="finished",
            )
            interrupt.request_stop()
            return self.implementation

    with pytest.raises(RunInterruptedError) as exc_info:
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(7, 8)[0],
            router=_router(),
            session_factory=lambda **kwargs: _InterruptSession(
                project_root=project_tree,
                model=kwargs["model"],
            ),
            interrupt=interrupt,
        )

    assert exc_info.value.cycle_number == 1
    assert exc_info.value.phase == "implementation"
    assert exc_info.value.agent_id == "agent-1"
    assert len(agents) == 1
    assert agents[0].closed is True
    assert (project_tree / "current-handover-prompt.md").read_text(encoding="utf-8") == initial_handover


def test_run_cycles_interrupt_prevents_next_cycle(project_tree: Path):
    cfg = _config(project_tree, cycles=3)
    interrupt = RunInterruptController()
    calls = {"next": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        calls["next"] += 1
        return _next_tasks(7)[0]

    class _CompleteThenInterruptSession(CycleSession):
        def start_implementation(self, prompt: str):
            agent = _FakeAgent()
            self._agent = agent
            self.implementation = SendRunResult(
                agent_id=agent.agent_id,
                run_id=f"{agent.agent_id}-run-1",
                status="finished",
            )
            return self.implementation

        def run_update(self, prompt: str):
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 8\n\nAdvanced.\n",
                encoding="utf-8",
            )
            self.update = SendRunResult(
                agent_id=self._agent.agent_id,  # type: ignore[union-attr]
                run_id=f"{self._agent.agent_id}-run-2",  # type: ignore[union-attr]
                status="finished",
            )
            interrupt.request_stop()
            return self.update

    with pytest.raises(RunInterruptedError) as exc_info:
        run_cycles(
            cfg,
            get_next_task_fn=get_next,
            router=_router(),
            session_factory=lambda **kwargs: _CompleteThenInterruptSession(
                project_root=project_tree,
                model=kwargs["model"],
            ),
            interrupt=interrupt,
        )

    assert exc_info.value.phase == "update"
    assert calls["next"] == 1
    partial = exc_info.value.partial_result
    assert partial is not None
    assert partial.completed_cycles == 0
    assert partial.outcomes == []


def test_cli_run_returns_interrupt_exit_code(project_tree: Path, monkeypatch, capsys):
    from cyclopsctl.loop import CycleOutcome, RunLoopResult

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    partial = RunLoopResult(
        completed_cycles=1,
        outcomes=[
            CycleOutcome(
                cycle_number=1,
                task_id=7,
                task_title="Interrupted task",
                model_id="composer-2.5",
                agent_id="agent-1",
                impl_run_id="run-1",
                update_run_id="run-2",
                verification_result="passed",
                duration_seconds=120.0,
            )
        ],
    )
    exc = RunInterruptedError(
        cycle_number=2,
        phase="implementation",
        agent_id="agent-42",
        run_id="run-42",
        partial_result=partial,
    )

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch("cyclopsctl.cli.run_cycles") as mock_run:
            mock_run.side_effect = exc
            code = main(
                [
                    "run",
                    "--cycles",
                    "2",
                    "--project-root",
                    str(project_tree),
                    "--first-prompt",
                    str(project_tree / "prompts" / "first.md"),
                    "--current-handover",
                    str(project_tree / "current-handover-prompt.md"),
                    "--update-handover",
                    str(project_tree / "update-handover-prompt.md"),
                    "--plain",
                ]
            )

    captured = capsys.readouterr()
    assert code == 130
    assert "Run summary" in captured.err
    assert "Interrupted task" in captured.err


def test_managed_cycle_display_stops_live_on_interrupt():
    from cyclopsctl.tui import RichCycleLogger, managed_cycle_display

    mock_live = MagicMock()
    mock_live.__enter__ = MagicMock(return_value=mock_live)
    mock_live.__exit__ = MagicMock(return_value=False)

    interrupt = RunInterruptController()

    with patch("cyclopsctl.tui.sys.stderr") as mock_stderr:
        mock_stderr.isatty.return_value = True
        with patch("cyclopsctl.tui.Live", return_value=mock_live):
            with managed_cycle_display(plain=False, interrupt=interrupt) as logger:
                assert isinstance(logger, RichCycleLogger)
                interrupt.request_stop()

    mock_live.stop.assert_called_once()


def test_run_dashboard_state_mark_interrupted():
    from cyclopsctl.tui import RunDashboardState, StepStatus

    state = RunDashboardState(phase="implementation")
    state.set_step("implementation", StepStatus.RUNNING)
    state.set_step("resolve", StepStatus.DONE)

    state.mark_interrupted()

    assert state.phase == "interrupted"
    assert state.step_status["implementation"] is StepStatus.PENDING
    assert state.step_status["resolve"] is StepStatus.DONE
