"""Tests for agent-run failure diagnostics (failure_report + state FAILED)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyclopsctl.cli import _log_and_exit_agent_run
import json

from cyclopsctl.failure_report import (
    encode_project_slug,
    format_failure_report,
    resolve_transcript_path,
    summarize_transcript_tail,
)
from cyclopsctl.runner import (
    RUN_FAILURE_EXIT_CODE,
    AgentRunError,
    RunFailureKind,
)
from cyclopsctl.state import (
    RunState,
    RunStateStatus,
    RunStateTracker,
    format_state_summary,
    read_state,
)


def _run_error(
    *,
    agent_id: str | None = "agent-db077b6c",
    run_id: str | None = "run-a9ee18a0",
    result_detail: str | None = None,
    diagnostic_detail: str | None = None,
    phase: str = "implementation",
    same_agent_continued: bool = False,
) -> AgentRunError:
    return AgentRunError(
        "implementation run failed: agent=agent-db077b6c run=run-a9ee18a0",
        kind=RunFailureKind.RUN,
        exit_code=RUN_FAILURE_EXIT_CODE,
        agent_id=agent_id,
        run_id=run_id,
        result_detail=result_detail,
        diagnostic_detail=diagnostic_detail,
        phase=phase,
        same_agent_continued=same_agent_continued,
    )


# --- encode_project_slug -------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"g:\DEV\CursorOrchestrator", "g-DEV-CursorOrchestrator"),
        (r"G:\GAMEDESIGN\test2", "g-GAMEDESIGN-test2"),
        (r"C:\Users\lbh\proj", "c-Users-lbh-proj"),
    ],
)
def test_encode_project_slug_windows(path: str, expected: str):
    assert encode_project_slug(path) == expected


# --- resolve_transcript_path ---------------------------------------------


def _make_transcript(home: Path, slug: str, agent_id: str) -> Path:
    transcript = (
        home
        / ".cursor"
        / "projects"
        / slug
        / "agent-transcripts"
        / agent_id
        / f"{agent_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n", encoding="utf-8")
    return transcript


def test_resolve_transcript_path_found(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    slug = encode_project_slug(project_root)
    home = tmp_path / "home"
    expected = _make_transcript(home, slug, "agent-xyz")

    found = resolve_transcript_path(project_root, "agent-xyz", home=home)
    assert found == expected


def test_resolve_transcript_path_case_insensitive_slug(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    slug = encode_project_slug(project_root)
    home = tmp_path / "home"
    expected = _make_transcript(home, slug.upper(), "agent-xyz")

    found = resolve_transcript_path(project_root, "agent-xyz", home=home)
    assert found == expected


def test_resolve_transcript_path_missing(tmp_path: Path):
    assert resolve_transcript_path(tmp_path, "agent-none", home=tmp_path) is None


def test_resolve_transcript_path_no_agent_id(tmp_path: Path):
    assert resolve_transcript_path(tmp_path, None, home=tmp_path) is None


# --- format_failure_report -----------------------------------------------


def test_format_failure_report_with_transcript(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    slug = encode_project_slug(project_root)
    home = tmp_path / "home"
    transcript = _make_transcript(home, slug, "agent-db077b6c")

    report = format_failure_report(
        _run_error(),
        project_root=project_root,
        home=home,
    )
    assert "Agent run failed during implementation" in report
    assert "agent-db077b6c" in report
    assert "run-a9ee18a0" in report
    assert str(transcript) in report
    assert "(SDK returned no detail)" in report


def _write_transcript(home: Path, slug: str, agent_id: str, records: list[dict]) -> Path:
    transcript = (
        home
        / ".cursor"
        / "projects"
        / slug
        / "agent-transcripts"
        / agent_id
        / f"{agent_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return transcript


def _assistant(*blocks: dict) -> dict:
    return {"role": "assistant", "message": {"content": list(blocks)}}


def test_summarize_transcript_tail_extracts_actions(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    records = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "huge prompt"}]}},
        _assistant(
            {"type": "text", "text": "[REDACTED]"},
            {"type": "tool_use", "name": "Shell", "input": {"command": "npm run test:unit 2>&1"}},
        ),
        _assistant(
            {"type": "tool_use", "name": "StrReplace", "input": {"path": "G:\\x\\StressSystem.test.ts"}},
        ),
        _assistant({"type": "text", "text": "Re-running unit tests to confirm."}),
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    tail = summarize_transcript_tail(transcript)

    assert "Shell: npm run test:unit 2>&1" in tail
    assert any("StressSystem.test.ts" in entry for entry in tail)
    assert any("Re-running unit tests" in entry for entry in tail)
    assert all("huge prompt" not in entry for entry in tail)  # user prompt excluded
    assert all(entry != "[REDACTED]" for entry in tail)


def test_summarize_transcript_tail_limits_entries(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    records = [
        _assistant({"type": "tool_use", "name": "Shell", "input": {"command": f"cmd {i}"}})
        for i in range(20)
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    tail = summarize_transcript_tail(transcript, max_entries=5)

    assert len(tail) == 5
    assert tail[-1] == "Shell: cmd 19"


def test_summarize_transcript_tail_missing_file(tmp_path: Path):
    assert summarize_transcript_tail(tmp_path / "nope.jsonl") == []


def test_format_failure_report_includes_transcript_tail(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    slug = encode_project_slug(project_root)
    home = tmp_path / "home"
    _write_transcript(
        home,
        slug,
        "agent-db077b6c",
        [
            _assistant(
                {"type": "tool_use", "name": "Shell", "input": {"command": "npm test 2>&1"}},
            ),
        ],
    )

    report = format_failure_report(_run_error(), project_root=project_root, home=home)

    assert "last activity (from transcript):" in report
    assert "Shell: npm test 2>&1" in report


def test_format_failure_report_with_detail_and_no_transcript(tmp_path: Path):
    report = format_failure_report(
        _run_error(result_detail="boom: tests failed"),
        project_root=tmp_path,
        home=tmp_path,
    )
    assert "boom: tests failed" in report
    assert "(local transcript not found)" in report


def test_format_failure_report_notes_same_agent_continue_after_midwork_drop(
    tmp_path: Path,
):
    report = format_failure_report(
        _run_error(
            diagnostic_detail=(
                "last agent output: Running the phase 13 integration test "
                "and the full cargo test suite."
            ),
            same_agent_continued=True,
        ),
        project_root=tmp_path,
        home=tmp_path,
    )
    assert "same-agent continue already attempted" in report
    assert "dropped mid-work; not retrying a fresh agent" in report


# --- state FAILED status -------------------------------------------------


def test_run_state_failed_round_trip(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    path = project_root / ".cyclopsctl" / "state.json"
    tracker = RunStateTracker(path, project_root=project_root)
    tracker.persist(
        cycle_number=1,
        total_cycles=1,
        phase="implementation",
        last_event="implementation failed",
        task_id=20,
        task_title="Task 20",
        agent_id="agent-db077b6c",
        run_id="run-a9ee18a0",
        status=RunStateStatus.FAILED,
    )

    result = read_state(path)
    assert result.kind == "ok"
    assert result.state is not None
    assert result.state.status is RunStateStatus.FAILED
    assert result.state.agent_id == "agent-db077b6c"
    assert result.state.run_id == "run-a9ee18a0"


def test_format_state_summary_failed_note(tmp_path: Path):
    state = RunState(
        cycle_number=1,
        total_cycles=1,
        phase="implementation",
        task_id=20,
        task_title="Task 20",
        agent_id="agent-db077b6c",
        run_id="run-a9ee18a0",
        last_event="implementation failed",
        status=RunStateStatus.FAILED,
        updated_at="2026-06-09T08:00:00+00:00",
        project_root=str(tmp_path),
    )
    from cyclopsctl.state import StateReadResult

    summary = format_state_summary(
        StateReadResult(kind="ok", path=tmp_path / "state.json", state=state)
    )
    assert "Status: failed" in summary
    assert "failed during implementation" in summary
    assert "running; the process may still be active" not in summary


# --- CLI handler ----------------------------------------------------------


def test_log_and_exit_agent_run_prints_report(tmp_path: Path, capsys, monkeypatch):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    slug = encode_project_slug(project_root)
    home = tmp_path / "home"
    transcript = _make_transcript(home, slug, "agent-db077b6c")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    code = _log_and_exit_agent_run(_run_error(), project_root=project_root)
    assert code == RUN_FAILURE_EXIT_CODE

    err = capsys.readouterr().err
    assert "cyclopsctl: error:" in err
    assert "Agent run failed during implementation" in err
    assert str(transcript) in err


# --- loop failure persistence --------------------------------------------


def test_persist_phase_failure_records_ids(tmp_path: Path):
    from types import SimpleNamespace

    from cyclopsctl.loop import _persist_phase_failure
    from cyclopsctl.tasks.types import NextTaskResult

    project_root = tmp_path / "repo"
    project_root.mkdir()
    path = project_root / ".cyclopsctl" / "state.json"
    tracker = RunStateTracker(path, project_root=project_root)
    tracker.persist(
        cycle_number=1,
        total_cycles=1,
        phase="implementation",
        last_event="cycle started",
        task_id=20,
        task_title="Task 20",
    )

    task = NextTaskResult(
        task_id="20",
        title="Task 20",
        status="pending",
        priority=None,
        complexity=None,
        tag=None,
    )
    _persist_phase_failure(
        tracker,
        config=SimpleNamespace(cycles=1),
        cycle_number=1,
        task=task,
        exc=_run_error(),
        phase="implementation",
    )

    result = read_state(path)
    assert result.state is not None
    assert result.state.status is RunStateStatus.FAILED
    assert result.state.last_event == "implementation failed"
    assert result.state.agent_id == "agent-db077b6c"
    assert result.state.run_id == "run-a9ee18a0"


def test_persist_phase_failure_no_tracker_is_noop():
    from types import SimpleNamespace

    from cyclopsctl.loop import _persist_phase_failure
    from cyclopsctl.tasks.types import NextTaskResult

    task = NextTaskResult(
        task_id="20",
        title="Task 20",
        status="pending",
        priority=None,
        complexity=None,
        tag=None,
    )
    _persist_phase_failure(
        None,
        config=SimpleNamespace(cycles=1),
        cycle_number=1,
        task=task,
        exc=_run_error(),
        phase="implementation",
    )
