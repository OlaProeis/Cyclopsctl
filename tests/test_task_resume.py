"""Tests for task-level resume (--resume) skipping completed parent tasks (task 22)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyclopsctl.config import ConfigError, build_run_config
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
        '{"complexityAnalysis": [{"taskId": 8, "complexityScore": 8}, '
        '{"taskId": 11, "complexityScore": 5}]}',
        encoding="utf-8",
    )
    from tests.conftest import seed_native_tasks
    seed_native_tasks(root)
    return root.resolve()


def _task(task_id: int) -> NextTaskResult:
    return NextTaskResult(
        task_id=str(task_id),
        title=f"Task {task_id}",
        status="pending",
        priority="high",
        complexity=8,
        tag="master",
    )


def _config(project_tree: Path, **extra):
    return build_run_config(
        {
            "cycles": 1,
            "project_root": project_tree,
            "first_prompt": project_tree / "prompts" / "first.md",
            "current_handover": project_tree / "current-handover-prompt.md",
            "update_handover": project_tree / "update-handover-prompt.md",
            "task_source": "sequential",
            **extra,
        }
    )


def _router() -> ModelRouter:
    return ModelRouter(
        default_model=COMPOSER_MODEL_ID,
        complexity_report=ComplexityReport(scores={8: 8, 9: 5, 10: 5, 11: 5}),
        capabilities=ModelCapabilities(
            composer=__import__("cursor_sdk").ModelSelection(id=COMPOSER_MODEL_ID),
            composer_standard=__import__("cursor_sdk").ModelSelection(id=COMPOSER_MODEL_ID),
            opus_available=False,
            opus=None,
        ),
    )


def _seed_history(project_tree: Path, completed_ids: list[int]) -> None:
    history_path = project_tree / ".cyclopsctl" / "run-history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    ids_json = ", ".join(str(task_id) for task_id in completed_ids)
    history_path.write_text(
        f"""{{
  "bootstrap_complete": true,
  "completed_cycle_task_ids": [{ids_json}],
  "last_handover_content_hash": "ignored",
  "last_handover_task_id": 11,
  "last_run_at": "2026-06-05T12:00:00+00:00",
  "project_root": "{str(project_tree).replace(chr(92), '/')}",
  "version": 1
}}""",
        encoding="utf-8",
    )


def _make_session_factory(
    project_tree: Path,
    *,
    impl_prompts: list[str] | None = None,
    update_calls: list[int] | None = None,
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
            if update_calls is not None:
                update_calls.append(1)
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


def test_build_run_config_resume_mutually_exclusive_with_fresh(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    base = {
        "cycles": 1,
        "project_root": project_root.resolve(),
        "first_prompt": project_root / "first.md",
        "current_handover": project_root / "current.md",
        "update_handover": project_root / "update.md",
        "complexity_report": project_root / "report.json",
    }
    for name in ("first.md", "current.md", "update.md", "report.json"):
        (project_root / name).write_text("x", encoding="utf-8")

    with pytest.raises(ConfigError, match="mutually exclusive"):
        build_run_config({**base, "fresh": True, "resume": True})


def test_build_run_config_resume_rejects_no_history(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    base = {
        "cycles": 1,
        "project_root": project_root.resolve(),
        "first_prompt": project_root / "first.md",
        "current_handover": project_root / "current.md",
        "update_handover": project_root / "update.md",
        "complexity_report": project_root / "report.json",
    }
    for name in ("first.md", "current.md", "update.md", "report.json"):
        (project_root / name).write_text("x", encoding="utf-8")

    with pytest.raises(ConfigError, match="requires run history"):
        build_run_config({**base, "resume": True, "history_file": ""})


def test_resume_skips_completed_tasks_and_runs_next(project_tree: Path, caplog):
    _seed_history(project_tree, [8, 9, 10])
    cfg = _config(project_tree, resume=True, cycles=1)
    pending = [_task(8), _task(9), _task(10), _task(11)]
    impl_prompts: list[str] = []
    update_calls: list[int] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 12\n\nAdvanced.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        result = run_cycles(
            cfg,
            list_pending_tasks_fn=lambda _root, tag=None: pending,
            router=_router(),
            session_factory=_make_session_factory(
                project_tree,
                impl_prompts=impl_prompts,
                update_calls=update_calls,
                on_update=on_update,
            ),
        )

    assert [skipped.task_id for skipped in result.skipped_tasks] == [8, 9, 10]
    assert result.completed_cycles == 1
    assert result.outcomes[0].task_id == 11
    assert len(impl_prompts) == 1
    assert len(update_calls) == 1
    assert "Skipping already completed task" in caplog.text


def test_resume_partial_cycles_exhaustion(project_tree: Path):
    _seed_history(project_tree, [8, 9])
    cfg = _config(project_tree, resume=True, cycles=3)
    pending_ids = [8, 9, 10, 11]
    history_completed = {8, 9}

    def list_pending(_root, tag=None):
        return [_task(task_id) for task_id in pending_ids]

    def on_update(_prompt: str) -> None:
        runnable = sorted(
            task_id for task_id in pending_ids if task_id not in history_completed
        )
        assert runnable, "expected a runnable task before update"
        completed_id = runnable[0]
        pending_ids.remove(completed_id)
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {completed_id + 1}\n\nAdvanced.\n",
            encoding="utf-8",
        )

    result = run_cycles(
        cfg,
        list_pending_tasks_fn=list_pending,
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    assert [skipped.task_id for skipped in result.skipped_tasks] == [8, 9]
    assert result.completed_cycles == 2
    assert [outcome.task_id for outcome in result.outcomes] == [10, 11]
    assert result.empty_queue is True


def test_resume_empty_remainder_after_skips(project_tree: Path):
    _seed_history(project_tree, [8, 9, 10])
    cfg = _config(project_tree, resume=True, cycles=2)
    pending = [_task(8), _task(9), _task(10)]

    result = run_cycles(
        cfg,
        list_pending_tasks_fn=lambda _root, tag=None: pending,
        router=_router(),
        session_factory=_make_session_factory(project_tree),
    )

    assert result.completed_cycles == 0
    assert result.empty_queue is True
    assert [skipped.task_id for skipped in result.skipped_tasks] == [8, 9, 10]
    assert result.outcomes == []


def test_resume_merges_completed_ids_into_history(project_tree: Path):
    _seed_history(project_tree, [8, 9, 10])
    cfg = _config(project_tree, resume=True, cycles=1)
    pending = [_task(8), _task(9), _task(10), _task(11)]

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 12\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        list_pending_tasks_fn=lambda _root, tag=None: pending,
        router=_router(),
        session_factory=_make_session_factory(project_tree, on_update=on_update),
    )

    history = read_history(project_tree / ".cyclopsctl" / "run-history.json")
    assert history.history is not None
    assert history.history.completed_cycle_task_ids == [8, 9, 10, 11]


def test_skipped_tasks_do_not_invoke_update_phase(project_tree: Path):
    _seed_history(project_tree, [8])
    cfg = _config(project_tree, resume=True, cycles=1)
    pending = [_task(8), _task(9)]
    update_calls: list[int] = []

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 10\n\nAdvanced.\n",
            encoding="utf-8",
        )

    run_cycles(
        cfg,
        list_pending_tasks_fn=lambda _root, tag=None: pending,
        router=_router(),
        session_factory=_make_session_factory(
            project_tree,
            update_calls=update_calls,
            on_update=on_update,
        ),
    )

    assert update_calls == [1]
