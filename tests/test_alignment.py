"""Tests for handover vs backend next alignment."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyclopsctl.alignment import (
    HandoverAlignmentError,
    compare_handover_to_backend_next,
    format_alignment_mismatch_message,
    verify_handover_alignment,
)
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.prompt import PromptError


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root.resolve()


def _handover(root: Path, task_id: int | None, *, filename: str = "current-handover-prompt.md") -> Path:
    path = root / filename
    if task_id is None:
        path.write_text("# Session Handover\n\nNo task id here.\n", encoding="utf-8")
    else:
        path.write_text(f"# Task ID: {task_id}\n\nDo the work.\n", encoding="utf-8")
    return path


def test_matching_ids_returns_aligned(project_root: Path):
    handover = _handover(project_root, 6)

    result = verify_handover_alignment(
        handover,
        6,
        project_root=project_root,
    )

    assert result.checked is True
    assert result.aligned is True
    assert result.handover_task_id == 6
    assert result.next_task_id == 6
    assert result.skipped_reason is None


def test_mismatched_ids_warn_mode_returns_not_aligned(project_root: Path):
    handover = _handover(project_root, 6)

    result = verify_handover_alignment(
        handover,
        8,
        project_root=project_root,
        strict=False,
    )

    assert result.checked is True
    assert result.aligned is False
    assert result.handover_task_id == 6
    assert result.next_task_id == 8


def test_mismatched_ids_strict_raises_with_both_ids_and_paths(project_root: Path):
    handover = _handover(project_root, 6)

    with pytest.raises(HandoverAlignmentError) as exc_info:
        verify_handover_alignment(
            handover,
            8,
            project_root=project_root,
            strict=True,
            expected_source="backend next",
        )

    message = str(exc_info.value)
    assert "Handover Task ID 6" in message
    assert "backend next task ID 8" in message
    assert str(handover) in message
    assert str(project_root) in message


def test_format_alignment_mismatch_message_includes_ids_and_paths(project_root: Path):
    handover = _handover(project_root, 6)
    result = verify_handover_alignment(
        handover,
        8,
        project_root=project_root,
        expected_source="selected handover",
    )

    message = format_alignment_mismatch_message(result)
    assert "Handover Task ID 6" in message
    assert "selected handover task ID 8" in message
    assert str(handover) in message
    assert str(project_root) in message


def test_missing_handover_on_cycle_one_skips_check(project_root: Path):
    handover = project_root / "current-handover-prompt.md"
    assert not handover.is_file()

    result = verify_handover_alignment(
        handover,
        8,
        project_root=project_root,
        skip_when_handover_missing=True,
    )

    assert result.checked is False
    assert result.aligned is None
    assert result.handover_task_id is None
    assert "missing" in (result.skipped_reason or "").lower()


def test_missing_handover_without_skip_flag_raises(project_root: Path):
    handover = project_root / "current-handover-prompt.md"

    with pytest.raises(PromptError, match="Handover file not found"):
        verify_handover_alignment(
            handover,
            8,
            project_root=project_root,
            skip_when_handover_missing=False,
        )


class _AlignmentBackend:
    def get_next(self, _project_root, *, tag=None):
        return NextTaskLookup.from_task(
            NextTaskResult(
                task_id="8",
                title="Expected next",
                status="pending",
                priority=None,
                complexity=None,
                tag=tag,
            )
        )


def test_compare_handover_to_backend_next_warn_mode(project_root: Path):
    handover = _handover(project_root, 6)
    backend = _AlignmentBackend()

    result = compare_handover_to_backend_next(
        handover,
        project_root,
        backend,
    )

    assert result.checked is True
    assert result.aligned is False
    assert result.handover_task_id == 6
    assert result.next_task_id == 8
    assert result.expected_source == "backend.get_next()"


def test_handover_without_task_id_marker_skips_check(project_root: Path):
    handover = _handover(project_root, None)

    result = verify_handover_alignment(
        handover,
        8,
        project_root=project_root,
    )

    assert result.checked is False
    assert result.aligned is None
    assert "Task ID marker" in (result.skipped_reason or "")
