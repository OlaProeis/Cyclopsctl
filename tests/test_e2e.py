"""End-to-end CLI tests for task 12: preflight, exit codes, stop conditions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from cursor_sdk import CursorAgentError, ModelSelection, RunResult

from cyclopsctl.cli import main
from cyclopsctl.errors import GENERAL_EXIT_CODE, STARTUP_EXIT_CODE, exit_code_for
from cyclopsctl.loop import run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.preflight import PreflightError, check_cursor_api_key, run_preflight
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.runner import AgentRunError, RunFailureKind, RUN_FAILURE_EXIT_CODE
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


class _StartupErrorAgent:
    def __init__(self) -> None:
        raise CursorAgentError("auth failed")


class _RunErrorAgent(_FakeAgent):
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


def _run_argv(project_tree: Path, *, cycles: int = 1) -> list[str]:
    return [
        "run",
        "--cycles",
        str(cycles),
        "--project-root",
        str(project_tree),
        "--first-prompt",
        str(project_tree / "prompts" / "first.md"),
        "--current-handover",
        str(project_tree / "current-handover-prompt.md"),
        "--update-handover",
        str(project_tree / "update-handover-prompt.md"),
        "--task-source",
        "handover",
    ]


def _pass_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")


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


def _session_factory(
    project_tree: Path,
    *,
    on_update: Callable[[str], None] | None = None,
    agent_factory: Callable[[], object] | None = None,
) -> Callable[..., CycleSession]:
    factory = agent_factory or _FakeAgent

    class _Session(CycleSession):
        def run_update(self, prompt: str):
            result = super().run_update(prompt)
            if on_update is not None:
                on_update(prompt)
            return result

    def make(**kwargs) -> CycleSession:
        return _Session(
            project_root=project_tree,
            model=kwargs["model"],
            create_agent=lambda **_kw: factory(),
        )

    return make


def test_preflight_requires_cursor_api_key():
    with pytest.raises(PreflightError, match="CURSOR_API_KEY"):
        check_cursor_api_key(env={})


def test_run_preflight_returns_api_key():
    key = run_preflight(env={"CURSOR_API_KEY": " secret "})
    assert key == "secret"


def test_exit_code_for_agent_errors():
    startup = AgentRunError("startup", kind=RunFailureKind.STARTUP, exit_code=1)
    run_fail = AgentRunError("run", kind=RunFailureKind.RUN, exit_code=2)
    assert exit_code_for(startup) == STARTUP_EXIT_CODE
    assert exit_code_for(run_fail) == RUN_FAILURE_EXIT_CODE
    assert exit_code_for(TaskSelectionError("cli failed")) == GENERAL_EXIT_CODE


def test_e2e_preflight_missing_api_key(project_tree: Path, monkeypatch, capsys):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    code = main(_run_argv(project_tree))

    captured = capsys.readouterr()
    assert code == GENERAL_EXIT_CODE
    assert "Preflight check failed" in captured.err or "CURSOR_API_KEY" in captured.err
    assert "cyclopsctl: error:" in captured.err


def test_e2e_missing_first_prompt_ok_when_handover_ready(
    project_tree: Path, monkeypatch, capsys
):
    """first-prompt is unused when current-handover has a Task ID."""
    _pass_preflight(monkeypatch)
    (project_tree / "prompts" / "first.md").unlink()
    advance = {"count": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        return _next_tasks(8)[0]

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nCycle {advance['count']} done.\n",
            encoding="utf-8",
        )

    def run_with_router(*args, **kwargs):
        kwargs["router"] = _router()
        kwargs["get_next_task_fn"] = get_next
        kwargs["session_factory"] = _session_factory(project_tree, on_update=on_update)
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_with_router):
        code = main(_run_argv(project_tree))

    captured = capsys.readouterr()
    assert code == 0
    assert "first-prompt not found" not in captured.err


def test_e2e_missing_first_prompt_fails_on_bootstrap_cycle(
    project_tree: Path, monkeypatch, capsys
):
    _pass_preflight(monkeypatch)
    (project_tree / "prompts" / "first.md").unlink()

    argv = _run_argv(project_tree) + ["--fresh"]

    def run_with_router(*args, **kwargs):
        kwargs["router"] = _router()
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_with_router):
        code = main(argv)

    captured = capsys.readouterr()
    assert code == GENERAL_EXIT_CODE
    assert "first-prompt not found" in captured.err


def test_e2e_run_failure_halts_with_exit_code_2(
    project_tree: Path,
    monkeypatch,
    capsys,
):
    _pass_preflight(monkeypatch)
    next_calls = {"count": 0}

    def failing_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        next_calls["count"] += 1
        raise TaskSelectionError("backend next failed with exit code 1")

    def run_with_cycle_failure(*args, **kwargs):
        kwargs["get_next_task_fn"] = failing_next
        kwargs["router"] = _router()
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_with_cycle_failure):
        code = main(_run_argv(project_tree, cycles=3))

    captured = capsys.readouterr()
    assert code == GENERAL_EXIT_CODE
    assert next_calls["count"] == 1
    assert "backend next failed" in captured.err


def test_e2e_startup_failure_exits_with_code_1(project_tree: Path, monkeypatch, capsys):
    _pass_preflight(monkeypatch)
    next_calls = {"count": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        next_calls["count"] += 1
        return _next_tasks(8)[0]

    def run_with_startup_failure(*args, **kwargs):
        kwargs["get_next_task_fn"] = get_next
        kwargs["router"] = _router()
        kwargs["session_factory"] = _session_factory(
            project_tree,
            agent_factory=_StartupErrorAgent,
        )
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_with_startup_failure):
        code = main(_run_argv(project_tree, cycles=3))

    captured = capsys.readouterr()
    assert code == STARTUP_EXIT_CODE
    assert next_calls["count"] == 1
    assert "cyclopsctl: error:" in captured.err


def test_e2e_run_failure_exits_with_code_2(project_tree: Path, monkeypatch, capsys):
    _pass_preflight(monkeypatch)
    next_calls = {"count": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        next_calls["count"] += 1
        return _next_tasks(8)[0]

    def on_update(_prompt: str) -> None:
        (project_tree / "current-handover-prompt.md").write_text(
            "# Task ID: 9\n\n",
            encoding="utf-8",
        )

    def run_with_run_failure(*args, **kwargs):
        kwargs["get_next_task_fn"] = get_next
        kwargs["router"] = _router()
        kwargs["session_factory"] = _session_factory(
            project_tree,
            on_update=on_update,
            agent_factory=_RunErrorAgent,
        )
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_with_run_failure):
        code = main(_run_argv(project_tree, cycles=3))

    captured = capsys.readouterr()
    assert code == RUN_FAILURE_EXIT_CODE
    assert next_calls["count"] == 1
    assert "cyclopsctl: error:" in captured.err


def test_e2e_verification_failure_stops_before_next_cycle(
    project_tree: Path,
    monkeypatch,
    capsys,
):
    _pass_preflight(monkeypatch)
    next_queue = _next_tasks(8, 9)
    next_calls = {"count": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        next_calls["count"] += 1
        return next_queue[next_calls["count"] - 1]

    def run_without_handover_advance(*args, **kwargs):
        kwargs["get_next_task_fn"] = get_next
        kwargs["router"] = _router()
        kwargs["session_factory"] = _session_factory(project_tree)
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_without_handover_advance):
        code = main(_run_argv(project_tree, cycles=2))

    captured = capsys.readouterr()
    assert code == GENERAL_EXIT_CODE
    assert next_calls["count"] == 1
    assert "unchanged" in captured.err.lower() or "verification" in captured.err.lower()


def test_e2e_success_stops_after_configured_cycles(project_tree: Path, monkeypatch, capsys):
    _pass_preflight(monkeypatch)
    next_queue = _next_tasks(8, 9, 10)
    advance = {"count": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        return next_queue.pop(0)

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nCycle {advance['count']} done.\n",
            encoding="utf-8",
        )

    def run_success(*args, **kwargs):
        kwargs["get_next_task_fn"] = get_next
        kwargs["router"] = _router()
        kwargs["session_factory"] = _session_factory(project_tree, on_update=on_update)
        return run_cycles(*args, **kwargs)

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_success):
        code = main(_run_argv(project_tree, cycles=2))

    captured = capsys.readouterr()
    assert code == 0
    assert "Completed 2 verified cycle(s)" in captured.err
    assert "Run summary" in captured.err
    assert "Task ID" in captured.err
    assert len(next_queue) == 1


def test_e2e_run_summary_appears_in_plain_mode_after_success(
    project_tree: Path,
    monkeypatch,
    capsys,
):
    _pass_preflight(monkeypatch)
    next_queue = _next_tasks(8)
    advance = {"count": 0}

    def get_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        return next_queue.pop(0)

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nCycle {advance['count']} done.\n",
            encoding="utf-8",
        )

    def run_success(*args, **kwargs):
        kwargs["get_next_task_fn"] = get_next
        kwargs["router"] = _router()
        kwargs["session_factory"] = _session_factory(project_tree, on_update=on_update)
        return run_cycles(*args, **kwargs)

    argv = _run_argv(project_tree, cycles=1) + ["--plain"]

    with patch("cyclopsctl.cli.run_cycles", side_effect=run_success):
        code = main(argv)

    captured = capsys.readouterr()
    assert code == 0
    assert "Run summary" in captured.err
    assert "Task 8" in captured.err
    assert "Verification" in captured.err
    assert "Duration" in captured.err


def test_e2e_plain_flag_passes_plain_to_cycle_display(project_tree: Path, monkeypatch):
    _pass_preflight(monkeypatch)
    seen: dict[str, bool] = {}

    def fake_display(*, plain: bool = False, jsonl_path=None, interrupt=None):
        from contextlib import contextmanager

        seen["plain"] = plain

        @contextmanager
        def _cm():
            from cyclopsctl.logging import CycleLogger

            yield CycleLogger()

        return _cm()

    argv = _run_argv(project_tree) + ["--plain"]

    with patch("cyclopsctl.cli.managed_cycle_display", side_effect=fake_display):
        with patch(
            "cyclopsctl.cli.run_cycles",
            return_value=__import__("cyclopsctl.loop", fromlist=["RunLoopResult"]).RunLoopResult(0),
        ):
            code = main(argv)

    assert code == 0
    assert seen.get("plain") is True
