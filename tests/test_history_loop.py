"""Loop integration tests for task 13: resume-aware cycle startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyclopsctl.config import build_run_config
from cyclopsctl.history import read_history
from cyclopsctl.loop import run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.session import CycleSession
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult


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

    def send(self, prompt: str) -> _FakeRun:
        self.sent.append(prompt)
        return _FakeRun(f"{self.agent_id}-run-{len(self.sent)}", self.agent_id)

    def close(self) -> None:
        pass


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


def _config(project_tree: Path, **extra):
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
        complexity_report=ComplexityReport(scores={8: 8, 9: 5}),
        capabilities=ModelCapabilities(
            composer=__import__("cursor_sdk").ModelSelection(id=COMPOSER_MODEL_ID),
            composer_standard=__import__("cursor_sdk").ModelSelection(id=COMPOSER_MODEL_ID),
            opus_available=False,
            opus=None,
        ),
    )


def _next_task(task_id: int) -> NextTaskLookup:
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
    impl_prompts: list[str] | None = None,
    on_update=None,
):
    class _CaptureSession(CycleSession):
        def start_implementation(self, prompt: str):
            if impl_prompts is not None:
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
            if on_update is not None:
                on_update(prompt)
            from cyclopsctl.runner import SendRunResult

            self.update = SendRunResult(
                agent_id=self._agent.agent_id,  # type: ignore[union-attr]
                run_id=f"{self._agent.agent_id}-run-2",  # type: ignore[union-attr]
                status="finished",
            )
            return self.update

    return lambda **kwargs: _CaptureSession(project_root=project_tree, model=kwargs["model"])


def test_fresh_run_uses_handover_when_present_and_writes_history(project_tree: Path):
    cfg = _config(project_tree)
    impl_prompts: list[str] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(8),
        router=_router(),
        session_factory=_make_session_factory(project_tree, impl_prompts=impl_prompts, on_update=on_update),
    )

    assert len(impl_prompts) == 1
    assert "# Task ID: 8\n\nImplement task 8.\n" in impl_prompts[0]
    history_path = project_tree / ".cyclopsctl" / "run-history.json"
    result = read_history(history_path)
    assert result.kind == "ok"
    assert result.history is not None
    assert result.history.bootstrap_complete is True
    assert result.history.completed_cycle_task_ids == [8]
    assert result.history.last_handover_task_id == 9


def test_resumed_run_uses_handover_for_cycle_one(project_tree: Path):
    history_path = project_tree / ".cyclopsctl" / "run-history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        """{
  "bootstrap_complete": true,
  "completed_cycle_task_ids": [8],
  "last_handover_content_hash": "ignored",
  "last_handover_task_id": 9,
  "last_run_at": "2026-06-05T12:00:00+00:00",
  "project_root": "%s",
  "version": 1
}"""
        % str(project_tree).replace("\\", "/"),
        encoding="utf-8",
    )
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 9\n\nNext task.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree)
    impl_prompts: list[str] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 10\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(9),
        router=_router(),
        session_factory=_make_session_factory(project_tree, impl_prompts=impl_prompts, on_update=on_update),
    )

    assert len(impl_prompts) == 1
    assert "# Task ID: 9\n\nNext task.\n" in impl_prompts[0]


def test_fresh_flag_forces_first_prompt_despite_history(project_tree: Path, caplog):
    history_path = project_tree / ".cyclopsctl" / "run-history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        """{
  "bootstrap_complete": true,
  "completed_cycle_task_ids": [8],
  "last_handover_content_hash": "ignored",
  "last_handover_task_id": 9,
  "last_run_at": "2026-06-05T12:00:00+00:00",
  "project_root": "%s",
  "version": 1
}"""
        % str(project_tree).replace("\\", "/"),
        encoding="utf-8",
    )
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 9\n\nNext task.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, fresh=True)
    impl_prompts: list[str] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 10\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(9),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree, impl_prompts=impl_prompts, on_update=on_update
            ),
        )

    assert len(impl_prompts) == 1
    assert "# First-run prompt\n" in impl_prompts[0]
    assert "Starting fresh bootstrap" in caplog.text


def test_history_handover_mismatch_warns_and_continues(project_tree: Path, caplog):
    history_path = project_tree / ".cyclopsctl" / "run-history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        """{
  "bootstrap_complete": true,
  "completed_cycle_task_ids": [8],
  "last_handover_content_hash": "ignored",
  "last_handover_task_id": 9,
  "last_run_at": "2026-06-05T12:00:00+00:00",
  "project_root": "%s",
  "version": 1
}"""
        % str(project_tree).replace("\\", "/"),
        encoding="utf-8",
    )
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 10\n\nWrong task.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree)
    impl_prompts: list[str] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 11\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(8),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree, impl_prompts=impl_prompts, on_update=on_update
            ),
        )

    assert len(impl_prompts) == 1
    assert "# Task ID: 10\n\nWrong task.\n" in impl_prompts[0]
    assert "Handover was updated since the last run" in caplog.text
    assert "history recorded 9" in caplog.text


def test_history_handover_updated_from_queue_complete_proceeds(
    project_tree: Path, caplog
):
    """Handover moved from Task ID 0 to a new task — must not block the run."""
    from tests.conftest import seed_native_tasks

    seed_native_tasks(project_tree, task_ids=(1, 2, 21, 22))
    history_path = project_tree / ".cyclopsctl" / "run-history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        """{
  "bootstrap_complete": true,
  "completed_cycle_task_ids": [21, 22, 23],
  "last_handover_content_hash": "ignored",
  "last_handover_task_id": 0,
  "last_run_at": "2026-06-05T12:00:00+00:00",
  "project_root": "%s",
  "version": 1
}"""
        % str(project_tree).replace("\\", "/"),
        encoding="utf-8",
    )
    (project_tree / "current-handover-prompt.md").write_text(
        "# Task ID: 1\n\nPhase 4 task 1.\n",
        encoding="utf-8",
    )
    cfg = _config(project_tree, strict_handover=True, cycles=1)
    impl_prompts: list[str] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 2\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(1),
            router=_router(),
            session_factory=_make_session_factory(
                project_tree, impl_prompts=impl_prompts, on_update=on_update
            ),
        )

    assert len(impl_prompts) == 1
    assert "# Task ID: 1" in impl_prompts[0]
    assert "history recorded 0" in caplog.text


def test_multi_invocation_resume_after_successful_run(project_tree: Path):
    cfg = _config(project_tree)

    def on_update_first(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(8),
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update_first),
    )

    impl_prompts: list[str] = []

    def on_update_second(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 10\n\nLater.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        get_next_task_fn=lambda _root, tag=None: _next_task(9),
        router=_router(),
        session_factory=_make_session_factory(
            project_tree, impl_prompts=impl_prompts, on_update=on_update_second
        ),
    )

    assert len(impl_prompts) == 1
    assert "# Task ID: 9\n\nAdvanced.\n" in impl_prompts[0]
    result = read_history(project_tree / ".cyclopsctl" / "run-history.json")
    assert result.history is not None
    assert result.history.last_handover_task_id == 10
