"""Tests for task 10: structured cycle logging."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cyclopsctl.config import build_run_config
from cyclopsctl.logging import (
    CycleLogRecord,
    CycleLogger,
    log_cycle,
    log_error,
    log_warning,
)
from cyclopsctl.loop import run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.session import CycleSession
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.verify import HandoverVerificationError


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

    def send(self, prompt: str) -> _FakeRun:
        return _FakeRun(f"{self.agent_id}-run-1", self.agent_id)

    def close(self) -> None:
        return None


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


def _config(project_tree: Path, *, cycles: int = 1):
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


def test_log_cycle_emits_required_fields_in_order(caplog):
    started = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 6, 5, 10, 5, 0, tzinfo=timezone.utc)
    record = CycleLogRecord(
        cycle_number=1,
        total_cycles=2,
        next_task_id=8,
        next_task_title="Task 8",
        model_id="composer-2.5",
        agent_id="agent-1",
        impl_run_id="agent-1-run-1",
        impl_status="finished",
        update_run_id="agent-1-run-2",
        update_status="finished",
        handover_before_task_id=8,
        handover_after_task_id=9,
        handover_before_hash="a" * 64,
        handover_after_hash="b" * 64,
        verification_result="passed",
        started_at=started,
        completed_at=completed,
    )

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        log_cycle(record)

    text = caplog.text
    assert "next_task_id=8" in text
    assert "handover_before_task_id=8" in text
    assert "handover_after_task_id=9" in text
    assert "model=composer-2.5" in text
    assert "agent_id=agent-1" in text
    assert "impl_run_id=agent-1-run-1" in text
    assert "update_run_id=agent-1-run-2" in text
    assert "verification_result=passed" in text
    assert "started_at=" in text
    assert "completed_at=" in text

    ordered_fields = [
        "next_task_id=8",
        "handover_before_task_id=8",
        "handover_after_task_id=9",
        "model=composer-2.5",
        "agent_id=agent-1",
        "impl_run_id=agent-1-run-1",
        "update_run_id=agent-1-run-2",
        "verification_result=passed",
    ]
    positions = [text.index(field) for field in ordered_fields]
    assert positions == sorted(positions)


def test_log_warning_and_error_include_actionable_context(caplog):
    with caplog.at_level(logging.WARNING, logger="cyclopsctl.cycle"):
        log_warning("Opus unavailable", fallback_model="composer-2.5", cycle_number=1)
    with caplog.at_level(logging.ERROR, logger="cyclopsctl.cycle"):
        log_error(
            "Handover verification failed",
            cycle_number=1,
            before_task_id=8,
            after_task_id=8,
            error="unchanged",
        )

    assert "Opus unavailable" in caplog.text
    assert "fallback_model='composer-2.5'" in caplog.text
    assert "Handover verification failed" in caplog.text
    assert "before_task_id=8" in caplog.text
    assert "error='unchanged'" in caplog.text


def test_jsonl_output_writes_structured_records(tmp_path: Path, caplog):
    jsonl_path = tmp_path / "cycles.jsonl"
    cycle_logger = CycleLogger(jsonl_path=jsonl_path)

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        cycle_logger.log_cycle_start(
            cycle_number=1,
            total_cycles=1,
            next_task_id=8,
            next_task_title="Task 8",
            model_id="composer-2.5",
            handover_task_id=8,
        )

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "cycle_start"
    assert payload["next_task_id"] == 8
    assert payload["model_id"] == "composer-2.5"


def test_run_cycles_logs_snapshots_for_initial_cycle(project_tree: Path, caplog):
    cfg = _config(project_tree, cycles=1)
    (project_tree / "current-handover-prompt.md").unlink()

    class _Session(CycleSession):
        def run_update(self, prompt: str):
            (project_tree / "current-handover-prompt.md").write_text(
                "# Task ID: 9\n\nCreated.\n",
                encoding="utf-8",
            )
            return super().run_update(prompt)

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: _next_task(8),
            router=_router(),
            session_factory=lambda **kwargs: _Session(
                project_root=project_tree,
                model=kwargs["model"],
                create_agent=lambda **_kw: _FakeAgent(),
            ),
        )

    text = caplog.text
    assert "snapshot before" in text
    assert "missing=True" in text
    assert "snapshot after" in text
    assert "task_id=9" in text
    assert "verification result=passed" in text


def test_run_cycles_logs_snapshots_for_later_cycle(project_tree: Path, caplog):
    cfg = _config(project_tree, cycles=2)
    advance = {"count": 0}

    def on_update(_prompt: str) -> None:
        advance["count"] += 1
        (project_tree / "current-handover-prompt.md").write_text(
            f"# Task ID: {8 + advance['count']}\n\nCycle {advance['count']} done.\n",
            encoding="utf-8",
        )

    class _Session(CycleSession):
        def run_update(self, prompt: str):
            on_update(prompt)
            return super().run_update(prompt)

    next_queue = [_next_task(8), _next_task(9)]

    with caplog.at_level(logging.INFO, logger="cyclopsctl.cycle"):
        run_cycles(
            cfg,
            get_next_task_fn=lambda _root, tag=None: next_queue.pop(0),
            router=_router(),
            session_factory=lambda **kwargs: _Session(
                project_root=project_tree,
                model=kwargs["model"],
                create_agent=lambda **_kw: _FakeAgent(),
            ),
        )

    text = caplog.text
    assert text.count("snapshot before") >= 2
    assert text.count("snapshot after") >= 2
    assert "task_id=9" in text
    assert "task_id=10" in text
    assert "impl_run_id=" in text
    assert "update_run_id=" in text


def test_cli_logs_actionable_error_context(project_tree: Path, caplog, monkeypatch):
    from unittest.mock import patch

    from cyclopsctl.cli import main

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    cfg_args = [
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

    with caplog.at_level(logging.ERROR, logger="cyclopsctl.cycle"):
        with patch("cyclopsctl.cli.run_cycles") as mock_run:
            mock_run.side_effect = HandoverVerificationError("Handover unchanged after update")
            code = main(cfg_args)

    assert code == 2
    assert "Cyclopsctl run failed" in caplog.text
    assert "error='Handover unchanged after update'" in caplog.text
    assert "error_type='HandoverVerificationError'" in caplog.text
