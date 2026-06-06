"""Tests for post-run summary table rendering (task 12)."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO

import pytest
from rich.console import Console

from cyclopsctl.loop import CycleOutcome
from cyclopsctl.tui import (
    SUMMARY_COLUMNS,
    format_duration,
    format_plain_summary,
    print_run_summary,
    render_summary_table,
)


@dataclass(frozen=True)
class _Outcome:
    cycle_number: int
    task_id: int
    task_title: str
    model_id: str
    duration_seconds: float
    verification_result: str


def _sample_outcomes(*, verification: str = "passed") -> list[_Outcome]:
    return [
        _Outcome(
            cycle_number=1,
            task_id=8,
            task_title="Build dashboard",
            model_id="composer-2.5",
            duration_seconds=312.4,
            verification_result=verification,
        ),
        _Outcome(
            cycle_number=2,
            task_id=9,
            task_title="Wire CLI",
            model_id="composer-2.5",
            duration_seconds=45.0,
            verification_result=verification,
        ),
    ]


def test_format_duration_formats_sub_minute_and_longer_values():
    assert format_duration(4.2) == "4.20s"
    assert format_duration(12.6) == "12.6s"
    assert format_duration(75) == "1m 15s"
    assert format_duration(3665) == "1h 1m"


def test_render_summary_table_includes_expected_columns():
    table = render_summary_table(_sample_outcomes())
    console = Console(width=160, record=True, file=StringIO())
    console.print(table)
    rendered = console.export_text()

    for column in SUMMARY_COLUMNS:
        assert column in rendered
    assert "Build dashboard" in rendered
    assert "composer-2.5" in rendered
    assert "passed" in rendered


@pytest.mark.parametrize(
    ("verification", "expected_token"),
    [
        ("passed", "passed"),
        ("failed", "failed"),
        ("interrupted", "interrupted"),
    ],
)
def test_render_summary_table_styles_verification_results(
    verification: str,
    expected_token: str,
):
    table = render_summary_table(_sample_outcomes(verification=verification)[:1])
    console = Console(width=120, record=True, file=StringIO())
    console.print(table)
    rendered = console.export_text()
    assert expected_token in rendered


def test_format_plain_summary_renders_ascii_table_without_tty():
    text = format_plain_summary(_sample_outcomes())

    assert "Run summary" in text
    for column in SUMMARY_COLUMNS:
        assert column in text
    assert "Build dashboard" in text
    assert "Wire CLI" in text
    assert "passed" in text


def test_format_plain_summary_empty_outcomes_returns_empty_string():
    assert format_plain_summary([]) == ""


def test_print_run_summary_plain_writes_to_stderr(capsys):
    print_run_summary(_sample_outcomes(), plain=True, stderr_is_tty=False)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Run summary" in captured.err
    assert "Task ID" in captured.err
    assert "Build dashboard" in captured.err


def test_print_run_summary_rich_mode_uses_rich_table(capsys):
    print_run_summary(_sample_outcomes(), plain=False, stderr_is_tty=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Run summary" in captured.err
    assert "Verification" in captured.err


def test_print_run_summary_skips_empty_outcomes(capsys):
    print_run_summary([], plain=True, stderr_is_tty=False)

    captured = capsys.readouterr()
    assert captured.err == ""


def test_print_run_summary_shows_skipped_tasks_only(capsys):
    from cyclopsctl.loop import SkippedTask

    print_run_summary(
        [],
        skipped_tasks=[SkippedTask(task_id=8, task_title="Done already")],
        plain=True,
        stderr_is_tty=False,
    )

    captured = capsys.readouterr()
    assert "Skipped tasks (--resume)" in captured.err
    assert "Done already" in captured.err


def test_cycle_outcome_includes_duration_and_verification():
    outcome = CycleOutcome(
        cycle_number=1,
        task_id=12,
        task_title="Summary table",
        model_id="composer-2.5",
        agent_id="agent-1",
        impl_run_id="run-1",
        update_run_id="run-2",
        verification_result="passed",
        duration_seconds=90.5,
    )
    assert outcome.verification_result == "passed"
    assert outcome.duration_seconds == 90.5
