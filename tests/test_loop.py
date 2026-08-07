"""Integration-style tests for task 8: cycle orchestration loop."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from cursor_sdk import ModelSelection, RunResult

from cyclopsctl.config import CyclopsctlConfig, build_run_config
from cyclopsctl.alignment import HandoverAlignmentError
from cyclopsctl.logging import CycleLogger
from cyclopsctl.loop import run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.prompt import (
    AI_CONTEXT_SECTION_HEADER,
    HandoverSnapshot,
    snapshot_handover,
)
from cyclopsctl.verify import HandoverVerificationError
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.runner import AgentRunError
from cyclopsctl.session import CycleSession
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.task_selection import TaskSelectionError


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
    yield
    _FakeAgent._counter = 0


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


def _config(project_tree: Path, *, cycles: int = 2, **extra) -> CyclopsctlConfig:
    return build_run_config(
        {
            "cycles": cycles,
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
        complexity_report=ComplexityReport(scores={8: 8, 9: 5}),
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


def _empty_queue(tag: str = "master") -> NextTaskLookup:
    return NextTaskLookup.empty(tag=tag)


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


def test_run_cycles_retries_implementation_with_composer_after_opus_billing_failure(
    project_tree: Path,
):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 9\n\nImplement task 9.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1)
    implementation_attempts = {"count": 0}
    fail_next_implementation = {"value": True}
    fable = ModelSelection(id="claude-fable-5")
    router = ModelRouter(
        default_model=COMPOSER_MODEL_ID,
        complexity_report=ComplexityReport(scores={9: 9}),
        capabilities=ModelCapabilities(
            composer=ModelSelection(id=COMPOSER_MODEL_ID),
            composer_standard=ModelSelection(id=COMPOSER_MODEL_ID),
            opus_available=False,
            opus=None,
            fable_available=True,
            fable=fable,
        ),
    )

    class _BillingFailOnceAgent(_FakeAgent):
        def send(self, prompt: str) -> _FakeRun:
            run = super().send(prompt)
            if fail_next_implementation["value"]:
                fail_next_implementation["value"] = False
                run.wait = lambda: RunResult(
                    id=run.id,
                    agent_id=run.agent_id,
                    status="error",
                    result="Your credit balance is too low to access the Fable API",
                )
            return run

    class _CountingSession(CycleSession):
        def start_implementation(self, prompt: str):
            implementation_attempts["count"] += 1
            return super().start_implementation(prompt)

        def run_update(self, prompt: str):
            result = super().run_update(prompt)
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 10\n\nAdvanced.\n",
                encoding="utf-8",
            )
            return result

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(9)[0],
        router=router,
        session_factory=lambda **kwargs: _CountingSession(
            project_root=project_tree,
            model=kwargs["model"],
            create_agent=lambda **_kw: _BillingFailOnceAgent(),
        ),
    )

    assert result.completed_cycles == 1
    assert implementation_attempts["count"] == 2
    assert router.fable_runtime_enabled is False
    assert result.outcomes[0].model_id == COMPOSER_MODEL_ID


def test_run_cycles_executes_n_verified_cycles(project_tree: Path):
    cfg = _config(project_tree, cycles=2)
    next_queue = _next_tasks(8, 9)
    advance = {"count": 0}

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nCycle {advance['count']} done.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: next_queue.pop(0),
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert result.completed_cycles == 2
    assert len(result.outcomes) == 2
    assert result.outcomes[0].cycle_number == 1
    assert result.outcomes[0].task_id == 8
    assert result.outcomes[1].task_id == 9
    assert result.outcomes[0].agent_id != result.outcomes[1].agent_id


def test_run_cycles_injects_ai_context_into_agent_prompts(project_tree: Path):
    (project_tree / "ai-context.md").write_text(
        "# Cyclopsctl Rules\n\nAlways test.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1)
    sent_prompts: list[str] = []

    class _CaptureSession(CycleSession):
        def start_implementation(self, prompt: str):
            sent_prompts.append(prompt)
            agent = _FakeAgent()
            self._agent = agent
            from cyclopsctl.runner import SendRunResult

            self.implementation = SendRunResult(
                agent_id=agent.agent_id,
                run_id=f"{agent.agent_id}-run-1",
                status="finished",
            )
            return self.implementation

        def run_update(self, prompt: str):
            sent_prompts.append(prompt)
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 9\n\nAdvanced.\n",
                encoding="utf-8",
            )
            from cyclopsctl.runner import SendRunResult

            self.update = SendRunResult(
                agent_id=self._agent.agent_id,  # type: ignore[union-attr]
                run_id=f"{self._agent.agent_id}-run-2",  # type: ignore[union-attr]
                status="finished",
            )
            return self.update

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
        router=_router(),
        session_factory=lambda **kwargs: _CaptureSession(
            project_root=project_tree,
            model=kwargs["model"],
        ),
    )

    assert len(sent_prompts) == 2
    impl_prompt, update_prompt = sent_prompts
    assert impl_prompt.startswith(f"{AI_CONTEXT_SECTION_HEADER}\n\n# Cyclopsctl Rules")
    assert "# Task ID: 8\n\nImplement task 8.\n" in impl_prompt
    assert "# Update\n" in update_prompt
    assert AI_CONTEXT_SECTION_HEADER not in update_prompt


def test_run_cycles_sends_update_handover_template(project_tree: Path):
    (project_tree / "update-handover-prompt.md").write_text(
        "# Update Handover Instructions\n\nMark done and advance.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1)
    sent_prompts: list[str] = []

    class _CaptureSession(CycleSession):
        def start_implementation(self, prompt: str):
            from cyclopsctl.runner import SendRunResult

            agent = _FakeAgent()
            self._agent = agent
            self.implementation = SendRunResult(
                agent_id=agent.agent_id,
                run_id=f"{agent.agent_id}-run-1",
                status="finished",
            )
            return self.implementation

        def run_update(self, prompt: str):
            sent_prompts.append(prompt)
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 9\n\nAdvanced.\n",
                encoding="utf-8",
            )
            from cyclopsctl.runner import SendRunResult

            self.update = SendRunResult(
                agent_id=self._agent.agent_id,  # type: ignore[union-attr]
                run_id=f"{self._agent.agent_id}-run-2",  # type: ignore[union-attr]
                status="finished",
            )
            return self.update

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
        router=_router(),
        session_factory=lambda **kwargs: _CaptureSession(
            project_root=project_tree,
            model=kwargs["model"],
        ),
    )

    assert len(sent_prompts) == 1
    assert "# Update Handover Instructions" in sent_prompts[0]
    assert "Mark done and advance." in sent_prompts[0]
    assert AI_CONTEXT_SECTION_HEADER not in sent_prompts[0]


def test_run_cycles_cycle_one_uses_current_handover_when_present(project_tree: Path):
    cfg = _config(project_tree, cycles=1)
    impl_prompts: list[str] = []

    class _CaptureSession(CycleSession):
        def start_implementation(self, prompt: str):
            impl_prompts.append(prompt)
            agent = _FakeAgent()
            self._agent = agent
            from cyclopsctl.runner import SendRunResult

            self.implementation = SendRunResult(
                agent_id=agent.agent_id,
                run_id=f"{agent.agent_id}-run-1",
                status="finished",
            )
            return self.implementation

        def run_update(self, prompt: str):
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 9\n\nAdvanced.\n",
                encoding="utf-8",
            )
            from cyclopsctl.runner import SendRunResult

            self.update = SendRunResult(
                agent_id=self._agent.agent_id,  # type: ignore[union-attr]
                run_id=f"{self._agent.agent_id}-run-2",  # type: ignore[union-attr]
                status="finished",
            )
            return self.update

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
        router=_router(),
        session_factory=lambda **kwargs: _CaptureSession(
            project_root=project_tree,
            model=kwargs["model"],
        ),
    )

    assert len(impl_prompts) == 1
    assert "# Task ID: 8\n\nImplement task 8.\n" in impl_prompts[0]
    assert "# First-run prompt\n" not in impl_prompts[0]


def test_run_cycles_later_cycles_use_current_handover(project_tree: Path):
    cfg = _config(project_tree, cycles=2)
    initial_handover = "# Task ID: 8\n\nImplement task 8.\n"
    impl_prompts: list[str] = []
    update_count = {"n": 0}

    def on_update(_prompt: str) -> None:
        update_count["n"] += 1
        if update_count["n"] == 1:
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 9\n\nNext task.\n",
                encoding="utf-8",
            )
        else:
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 10\n\nLater task.\n",
                encoding="utf-8",
            )

    class _CaptureSession(CycleSession):
        def start_implementation(self, prompt: str):
            impl_prompts.append(prompt)
            agent = _FakeAgent()
            self._agent = agent
            from cyclopsctl.runner import SendRunResult

            self.implementation = SendRunResult(
                agent_id=agent.agent_id,
                run_id=f"{agent.agent_id}-run-1",
                status="finished",
            )
            return self.implementation

        def run_update(self, prompt: str):
            on_update(prompt)
            from cyclopsctl.runner import SendRunResult

            self.update = SendRunResult(
                agent_id=self._agent.agent_id,  # type: ignore[union-attr]
                run_id=f"{self._agent.agent_id}-run-2",  # type: ignore[union-attr]
                status="finished",
            )
            return self.update

    next_queue = _next_tasks(8, 9)

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: next_queue.pop(0),
        router=_router(),
        session_factory=lambda **kwargs: _CaptureSession(
            project_root=project_tree,
            model=kwargs["model"],
        ),
    )

    assert "# Task ID: 8\n\nImplement task 8.\n" in impl_prompts[0]
    assert "# Task ID: 9\n\nNext task.\n" in impl_prompts[1]
    assert initial_handover == "# Task ID: 8\n\nImplement task 8.\n"


def test_run_cycles_same_agent_for_update_different_agent_per_cycle(project_tree: Path):
    cfg = _config(project_tree, cycles=2)
    per_cycle_agents: list[tuple[str, str]] = []

    class _TrackSession(CycleSession):
        def start_implementation(self, prompt: str):
            result = super().start_implementation(prompt)
            self._impl_agent = result.agent_id
            return result

        def run_update(self, prompt: str):
            result = super().run_update(prompt)
            per_cycle_agents.append((self._impl_agent, result.agent_id))  # type: ignore[attr-defined]
            (project_tree / "current-handover-prompt.md").write_text(
                f"# Task ID: {9 + len(per_cycle_agents)}\n\n",
                encoding="utf-8",
            )
            return result

    next_queue = _next_tasks(8, 9)

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: next_queue.pop(0),
        router=_router(),
        session_factory=lambda **kwargs: _TrackSession(
            project_root=project_tree,
            model=kwargs["model"],
            create_agent=lambda **_kw: _FakeAgent(),
        ),
    )

    assert len(per_cycle_agents) == 2
    assert per_cycle_agents[0][0] == per_cycle_agents[0][1]
    assert per_cycle_agents[1][0] == per_cycle_agents[1][1]
    assert per_cycle_agents[0][0] != per_cycle_agents[1][0]


def test_run_cycles_stops_on_backend_next_error(project_tree: Path):
    cfg = _config(project_tree, cycles=3)

    with pytest.raises(TaskSelectionError, match="backend next failed"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: (_ for _ in ()).throw(
                TaskSelectionError("backend next failed")
            ),
            router=_router(),
            session_factory=_make_session_factory(project_tree),
        )


def test_run_cycles_stops_on_agent_run_error_without_next_cycle(project_tree: Path):
    cfg = _config(project_tree, cycles=3, retry_on="off")
    calls = {"next": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        calls["next"] += 1
        return _next_tasks(8)[0]

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\n",
            encoding="utf-8",
        )

    with pytest.raises(AgentRunError):
        run_cycles(
            cfg,
            get_next_task_fn=get_next,
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                on_update=on_update,
                agent_factory=_ErrorAgent,
            ),
        )

    assert calls["next"] == 1


def test_run_cycles_stops_on_verification_failure(project_tree: Path):
    cfg = _config(project_tree, cycles=2)
    calls = {"next": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        calls["next"] += 1
        return _next_tasks(8, 9)[calls["next"] - 1]

    with pytest.raises(HandoverVerificationError, match="unchanged"):
        run_cycles(
            cfg,
            get_next_task_fn=get_next,
            router=_router(),
            session_factory=_make_session_factory(project_tree),
        )

    assert calls["next"] == 1


def test_run_cycles_snapshot_before_update_uses_allow_missing_on_cycle_one(
    project_tree: Path,
):
    cfg = _config(project_tree, cycles=1)
    (project_tree / "current-handover-prompt.md").unlink()
    snapshots: list[HandoverSnapshot] = []
    original = snapshot_handover

    def tracking_snapshot(path: Path, *, allow_missing: bool = False) -> HandoverSnapshot:
        snap = original(path, allow_missing=allow_missing)
        snapshots.append(snap)
        return snap

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nCreated.\n",
            encoding="utf-8",
        )

    with patch("cyclopsctl.verify.snapshot_handover", side_effect=tracking_snapshot):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
            router=_router(),
            session_factory=_make_session_factory(project_tree, on_update=on_update),
        )

    assert snapshots[0].missing is True
    assert snapshots[1].missing is False
    current_snaps = [
        snap
        for snap in snapshots
        if snap.path.name == "current-handover-prompt.md"
    ]
    assert current_snaps[0].missing is True
    pre_update_current = next(
        snap for snap in current_snaps[1:] if snap.missing or snap.task_id is not None
    )
    assert pre_update_current.missing is True
    assert current_snaps[-1].missing is False


def test_run_cycles_restores_handover_when_impl_modifies_it(project_tree: Path):
    """Impl agent must not edit handover; cyclopsctl restores before update."""
    cfg = _config(project_tree, cycles=1)
    update_path = project_tree / "update-handover-prompt.md"
    update_hash_at_start = snapshot_handover(update_path).content_hash

    class _ImplMutatingSession(CycleSession):
        def start_implementation(self, prompt: str):
            result = super().start_implementation(prompt)
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 8\n\nEdited by impl agent.\n",
                encoding="utf-8",
            )
            (project_tree / "update-handover-prompt.md").write_text(
                "# corrupted\n",
                encoding="utf-8",
            )
            return result

        def run_update(self, prompt: str):
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 9\n\nAdvanced by update.\n",
                encoding="utf-8",
            )
            from cyclopsctl.runner import SendRunResult

            self.update = SendRunResult(
                agent_id=self.agent_id or "fake",
                run_id="update-run",
                status="finished",
            )
            return self.update

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
        router=_router(),
        session_factory=lambda **kwargs: _ImplMutatingSession(
            project_root=project_tree,
            model=kwargs["model"],
            create_agent=lambda **_kw: _FakeAgent(),
        ),
    )

    assert result.completed_cycles == 1
    assert (project_tree / "current-handover-prompt.md").read_text(encoding="utf-8") == (
        "# Task ID: 9\n\nAdvanced by update.\n"
    )
    assert (
        snapshot_handover(update_path).content_hash == update_hash_at_start
    )


def test_run_cycles_strict_fails_when_impl_modifies_handover(project_tree: Path):
    from cyclopsctl.verify import ImplementationHandoverViolationError

    cfg = build_run_config(
        {**_base_cli_dict(project_tree, cycles=1), "strict_handover": True}
    )

    class _ImplMutatingSession(CycleSession):
        def start_implementation(self, prompt: str):
            result = super().start_implementation(prompt)
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 8\n\nEdited by impl.\n",
                encoding="utf-8",
            )
            return result

    with pytest.raises(ImplementationHandoverViolationError, match="implementation"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
            router=_router(),
            session_factory=lambda **kwargs: _ImplMutatingSession(
                project_root=project_tree,
                model=kwargs["model"],
                create_agent=lambda **_kw: _FakeAgent(),
            ),
        )


def test_cli_run_delegates_to_loop(project_tree: Path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from cyclopsctl.cli import main
    from cyclopsctl.loop import RunLoopResult

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch("cyclopsctl.cli.run_cycles") as mock_run:
            mock_run.return_value = RunLoopResult(completed_cycles=1)
            code = main(
                [
                    "run",
                    "--cycles",
                    "1",
                    "--project-root",
                    str(project_tree),
                    "--first-prompt",
                    str(project_tree / "prompts" / "first.md"),
                    "--current-handover",
                    str(project_tree / "current-handover-prompt.md"),
                    "--update-handover",
                    str(project_tree / "update-handover-prompt.md"),
                ]
            )

        assert code == 0
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["api_key"] == "test-api-key"
        mock_bridge.assert_called_once_with(project_tree.resolve())


def test_run_cycles_empty_queue_before_first_cycle(project_tree: Path):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 0\n\nQueue complete.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=3)

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _empty_queue(),
        list_pending_tasks_fn=lambda _root, tag=None: [],
        router=_router(),
        session_factory=_make_session_factory(project_tree),
    )

    assert result.completed_cycles == 0
    assert result.empty_queue is True
    assert result.empty_queue_tag == "master"
    assert result.outcomes == []


def test_run_cycles_empty_queue_after_completed_cycles(project_tree: Path):
    from tests.conftest import seed_native_tasks

    seed_native_tasks(project_tree, task_ids=(8, 9))
    cfg = _config(project_tree, cycles=5)
    next_queue = _next_tasks(8, 9)
    advance = {"count": 0}

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nCycle {advance['count']} done.\n",
            encoding="utf-8",
        )

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        if next_queue:
            return next_queue.pop(0)
        return _empty_queue()

    result = run_cycles(
        cfg,
        get_next_task_fn=get_next,
        list_pending_tasks_fn=lambda _root, tag=None: [],
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert result.completed_cycles == 2
    assert result.empty_queue is True
    assert len(result.outcomes) == 2


def test_cli_run_empty_queue_exits_zero(project_tree: Path, monkeypatch, capsys):
    from unittest.mock import MagicMock, patch

    from cyclopsctl.cli import main
    from cyclopsctl.loop import RunLoopResult

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch("cyclopsctl.cli.run_cycles") as mock_run:
            mock_run.return_value = RunLoopResult(
                completed_cycles=2,
                empty_queue=True,
                empty_queue_tag="master",
            )
            code = main(
                [
                    "run",
                    "--cycles",
                    "5",
                    "--project-root",
                    str(project_tree),
                    "--first-prompt",
                    str(project_tree / "prompts" / "first.md"),
                    "--current-handover",
                    str(project_tree / "current-handover-prompt.md"),
                    "--update-handover",
                    str(project_tree / "update-handover-prompt.md"),
                ]
            )

    captured = capsys.readouterr()
    assert code == 0
    assert "Task queue empty (tag='master')" in captured.err
    assert "Completed 2 verified cycle(s) in this run" in captured.err


def test_run_cycles_alignment_mismatch_warns_and_continues(project_tree: Path, caplog):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 16\n\nWrong task.\n",
        encoding="utf-8",
    )
    cfg = build_run_config(
        {
            **_base_cli_dict(project_tree, cycles=1),
            "task_source": "handover",
        }
    )

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        result = run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
            router=_router(),
            session_factory=_make_session_factory(project_tree, on_update=on_update),
        )

    assert result.completed_cycles == 1
    warning_text = caplog.text
    assert "Selected task ID 16 does not match backend next task ID 8" in warning_text
    assert "current-handover-prompt.md" in warning_text
    assert str(project_tree) in warning_text


def test_run_cycles_handover_mode_allows_next_priority_ahead_of_handover(
    project_tree: Path,
):
    """Handover 21 with backend next 22 must not fail when 21 is selected."""
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 21\n\nImplement task 21.\n",
        encoding="utf-8",
    )
    cfg = build_run_config(
        {
            **_base_cli_dict(project_tree, cycles=1),
            "strict_handover": True,
            "task_source": "handover",
        }
    )
    agent_created = {"value": False}

    def _lookup(task_id: int) -> NextTaskLookup:
        tasks = {
            21: NextTaskResult(
                task_id="21",
                title="Task 21",
                status="pending",
                priority="medium",
                complexity=6,
                tag="master",
            ),
        }
        task = tasks.get(task_id)
        if task is None:
            return NextTaskLookup.empty(tag="master")
        return NextTaskLookup.from_task(task, tag="master")

    class _TrackSession(CycleSession):
        def start_implementation(self, prompt: str):
            agent_created["value"] = True
            return super().start_implementation(prompt)

        def run_update(self, prompt: str):
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 22\n\nAdvanced.\n",
                encoding="utf-8",
            )
            return super().run_update(prompt)

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(22)[0],
        get_task_by_id_fn=lambda _root, task_id, tag=None: _lookup(int(task_id)),
        router=ModelRouter(
            default_model=COMPOSER_MODEL_ID,
            complexity_report=ComplexityReport(scores={21: 6, 22: 5}),
            capabilities=_router().capabilities,
        ),
        session_factory=lambda **kwargs: _TrackSession(
            project_root=project_tree,
            model=kwargs["model"],
            create_agent=lambda **_kw: _FakeAgent(),
        ),
    )

    assert result.completed_cycles == 1
    assert agent_created["value"] is True


def test_run_cycles_alignment_mismatch_strict_fails_before_agent(project_tree: Path):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 16\n\nWrong task.\n",
        encoding="utf-8",
    )
    cfg = build_run_config(
        {
            **_base_cli_dict(project_tree, cycles=1),
            "strict_handover": True,
            "task_source": "sequential",
        }
    )
    agent_created = {"value": False}

    class _NoAgentSession(CycleSession):
        def start_implementation(self, prompt: str):
            agent_created["value"] = True
            return super().start_implementation(prompt)

    with pytest.raises(HandoverAlignmentError) as exc_info:
        run_cycles(
            cfg,
            list_pending_tasks_fn=lambda _root, tag=None: [_task(8)],
            router=_router(),
            session_factory=lambda **kwargs: _NoAgentSession(
                project_root=project_tree,
                model=kwargs["model"],
                create_agent=lambda **_kw: _FakeAgent(),
            ),
        )

    message = str(exc_info.value)
    assert "Handover Task ID 16" in message
    assert "lowest pending task ID 8" in message
    assert str(project_tree / "current-handover-prompt.md") in message
    assert str(project_tree) in message
    assert agent_created["value"] is False


def test_run_cycles_alignment_runs_before_agent_creation(project_tree: Path):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 16\n\nWrong task.\n",
        encoding="utf-8",
    )
    cfg = build_run_config(
        {
            **_base_cli_dict(project_tree, cycles=1),
            "strict_handover": True,
            "task_source": "sequential",
        }
    )
    call_order: list[str] = []

    def tracking_alignment(*args, **kwargs):
        call_order.append("alignment")
        from cyclopsctl import alignment as alignment_module

        return alignment_module.verify_handover_alignment(*args, **kwargs)

    class _TrackSession(CycleSession):
        def start_implementation(self, prompt: str):
            call_order.append("agent")
            return super().start_implementation(prompt)

    with pytest.raises(HandoverAlignmentError):
        with patch("cyclopsctl.loop.verify_handover_alignment", side_effect=tracking_alignment):
            run_cycles(
                cfg,
                list_pending_tasks_fn=lambda _root, tag=None: [_task(8)],
                router=_router(),
                session_factory=lambda **kwargs: _TrackSession(
                    project_tree=project_tree,
                    model=kwargs["model"],
                    create_agent=lambda **_kw: _FakeAgent(),
                ),
            )

    assert call_order == ["alignment"]


def test_run_cycles_cycle_one_missing_handover_skips_alignment(project_tree: Path, caplog):
    cfg = _config(project_tree, cycles=1)
    (project_tree / "current-handover-prompt.md").unlink()

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nCreated.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        result = run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
            router=_router(),
            session_factory=_make_session_factory(project_tree, on_update=on_update),
        )

    assert result.completed_cycles == 1
    assert "Skipping handover alignment check" in caplog.text
    assert "handover file missing" in caplog.text.lower()


def _base_cli_dict(project_tree: Path, *, cycles: int) -> dict:
    return {
        "cycles": cycles,
        "project_root": project_tree,
        "first_prompt": project_tree / "prompts" / "first.md",
        "current_handover": project_tree / "current-handover-prompt.md",
        "update_handover": project_tree / "update-handover-prompt.md",
    }


def _task(task_id: int) -> NextTaskResult:
    return NextTaskResult(
        task_id=str(task_id),
        title=f"Task {task_id}",
        status="pending",
        priority="high",
        complexity=8,
        tag="master",
    )


def _lookup(task_id: int) -> NextTaskLookup:
    return NextTaskLookup.from_task(_task(task_id))


def test_run_cycles_handover_mode_routes_by_handover_id(project_tree: Path):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 16\n\nImplement task 16.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1, task_source="handover")

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 17\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _lookup(8),
        get_task_by_id_fn=lambda _root, task_id, tag=None: _lookup(int(task_id)),
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert result.completed_cycles == 1
    assert result.outcomes[0].task_id == 16


def test_run_cycles_handover_task_id_zero_falls_back_to_next(project_tree: Path):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 0\n\nQueue complete.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1, task_source="handover")

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 17\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _lookup(17),
        list_pending_tasks_fn=lambda _root, tag=None: [],
        get_task_by_id_fn=lambda _root, task_id, tag=None: (
            NextTaskLookup.empty()
            if int(task_id) == 0
            else _lookup(int(task_id))
        ),
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert result.completed_cycles == 1
    assert result.outcomes[0].task_id == 17


def test_run_cycles_sequential_mode_picks_lowest_pending(project_tree: Path):
    cfg = _config(project_tree, cycles=1, task_source="sequential")

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 3\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _lookup(7),
        list_pending_tasks_fn=lambda _root, tag=None: [_task(7), _task(2)],
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert result.completed_cycles == 1
    assert result.outcomes[0].task_id == 2


def test_run_cycles_warns_when_selected_differs_from_backend_next(
    project_tree: Path, caplog
):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 16\n\nImplement task 16.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1, task_source="handover")

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 17\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        result = run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _lookup(8),
            get_task_by_id_fn=lambda _root, task_id, tag=None: _lookup(int(task_id)),
            router=_router(),
            session_factory=_make_session_factory(project_tree, on_update=on_update),
        )

    assert result.completed_cycles == 1
    assert "Selected task ID 16" in caplog.text
    assert "backend next task ID 8" in caplog.text
    assert "task-source: handover" in caplog.text


def test_run_cycles_no_mismatch_warning_when_selected_matches_backend_next(
    project_tree: Path, caplog
):
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 8\n\nCorrect task.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, cycles=1, task_source="handover")

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        result = run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _lookup(8),
            get_task_by_id_fn=lambda _root, task_id, tag=None: _lookup(int(task_id)),
            router=_router(),
            session_factory=_make_session_factory(project_tree, on_update=on_update),
        )

    assert result.completed_cycles == 1
    assert "does not match backend next" not in caplog.text


def test_run_cycles_clears_state_on_success(project_tree: Path):
    state_path = project_tree / ".cyclopsctl" / "state.json"
    cfg = _config(project_tree, cycles=1)

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert not state_path.is_file()


def test_run_cycles_marks_state_failed_on_agent_failure(project_tree: Path):
    state_path = project_tree / ".cyclopsctl" / "state.json"
    cfg = _config(project_tree, cycles=2, retry_on="off")

    with pytest.raises(AgentRunError):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(8, 9)[0],
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                agent_factory=_ErrorAgent,
            ),
        )

    from cyclopsctl.state import RunStateStatus, read_state

    result = read_state(state_path)
    assert result.kind == "ok"
    assert result.state is not None
    assert result.state.status is RunStateStatus.FAILED
    assert result.state.phase == "implementation"
    assert result.state.last_event == "implementation failed"
    assert result.state.agent_id is not None


def test_run_cycles_no_state_when_disabled(project_tree: Path):
    state_path = project_tree / ".cyclopsctl" / "state.json"
    cfg = _config(project_tree, cycles=1, state_file="")

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert not state_path.is_file()


def _pending_tasks(*task_ids: int) -> list[NextTaskResult]:
    return [
        NextTaskResult(
            task_id=str(task_id),
            title=f"Task {task_id}",
            status="pending",
            priority="high",
            complexity=8,
            tag="master",
        )
        for task_id in task_ids
    ]


def test_run_cycles_captures_queue_snapshot_from_list_pending(
    project_tree: Path, caplog
):
    import logging

    cfg = _config(project_tree, cycles=1)
    list_calls: list[tuple] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    def list_pending(_root, tag=None):
        list_calls.append((tag,))
        return _pending_tasks(8, 9, 10)

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_tasks(8)[0],
            list_pending_tasks_fn=list_pending,
            router=_router(),
            session_factory=_make_session_factory(project_tree, on_update=on_update),
        )

    assert len(list_calls) == 1
    assert "Queue: [●8] [9] [10] (3 pending)" in caplog.text


def test_run_cycles_queue_snapshot_includes_session_completed_ids(project_tree: Path):
    cfg = _config(project_tree, cycles=2)
    next_queue = _next_tasks(8, 9)
    advance = {"count": 0}

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nDone.\n",
            encoding="utf-8",
        )

    def list_pending(_root, tag=None):
        return _pending_tasks(8, 9, 10, 11)

    cycle_start_logs: list[str] = []

    class _CaptureLogger(CycleLogger):
        def log_cycle_start(self, **kwargs):
            snapshot = kwargs.get("queue_snapshot")
            if snapshot is not None:
                from cyclopsctl.tui import TaskQueueStripState, format_queue_strip_line

                strip = TaskQueueStripState()
                strip.apply_snapshot(snapshot)
                cycle_start_logs.append(format_queue_strip_line(strip))
            return super().log_cycle_start(**kwargs)

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: next_queue.pop(0),
        list_pending_tasks_fn=list_pending,
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
        cycle_logger=_CaptureLogger(),
    )

    assert cycle_start_logs == [
        "Queue: [●8] [9] [10] [11] (4 pending)",
        "Queue: [✓8] [●9] [10] [11] … (4 pending)",
    ]
