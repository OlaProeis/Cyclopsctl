"""Handover Task ID vs backend next alignment (task 6)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cyclopsctl.prompt import snapshot_handover
from cyclopsctl.tasks.backend import TaskBackend


class HandoverAlignmentError(RuntimeError):
    """Handover Task ID does not match the expected cycle task."""


@dataclass(frozen=True)
class HandoverAlignmentResult:
    """Outcome of comparing handover Task ID to the expected cycle task."""

    checked: bool
    aligned: bool | None
    handover_task_id: int | None
    next_task_id: int
    handover_path: Path
    project_root: Path
    expected_source: str = "backend next"
    skipped_reason: str | None = None


def format_alignment_mismatch_message(result: HandoverAlignmentResult) -> str:
    """Build a warning/error message including both IDs and both paths."""
    return (
        f"Handover Task ID {result.handover_task_id} does not match "
        f"{result.expected_source} task ID {result.next_task_id} "
        f"(handover: {result.handover_path}; "
        f"project: {result.project_root})"
    )


def verify_handover_alignment(
    handover_path: Path,
    expected_task_id: int,
    *,
    project_root: Path,
    strict: bool = False,
    skip_when_handover_missing: bool = False,
    force_skip_reason: str | None = None,
    expected_source: str = "backend next",
) -> HandoverAlignmentResult:
    """
    Compare handover ``# Task ID:`` to the expected task for this cycle.

    In ``handover`` / ``sequential`` mode the expected id is the **selected**
    task (handover file or lowest pending), not backend next priority.

    When ``skip_when_handover_missing`` is True (cycle 1 bootstrap), a missing
    handover file skips the check instead of comparing IDs.

    When ``force_skip_reason`` is set, the check is skipped (for example when
    the cyclopsctl defers to backend next because the handover is stale).
    """
    if force_skip_reason:
        snap = snapshot_handover(handover_path, allow_missing=True)
        return HandoverAlignmentResult(
            checked=False,
            aligned=None,
            handover_task_id=snap.task_id,
            next_task_id=expected_task_id,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
            skipped_reason=force_skip_reason,
        )

    snap = snapshot_handover(handover_path, allow_missing=skip_when_handover_missing)

    if snap.missing:
        return HandoverAlignmentResult(
            checked=False,
            aligned=None,
            handover_task_id=None,
            next_task_id=expected_task_id,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
            skipped_reason="handover file missing (cycle 1 bootstrap)",
        )

    handover_task_id = snap.task_id
    if handover_task_id is None:
        return HandoverAlignmentResult(
            checked=False,
            aligned=None,
            handover_task_id=None,
            next_task_id=expected_task_id,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
            skipped_reason="handover missing Task ID marker",
        )

    if handover_task_id == expected_task_id:
        return HandoverAlignmentResult(
            checked=True,
            aligned=True,
            handover_task_id=handover_task_id,
            next_task_id=expected_task_id,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
        )

    result = HandoverAlignmentResult(
        checked=True,
        aligned=False,
        handover_task_id=handover_task_id,
        next_task_id=expected_task_id,
        handover_path=handover_path,
        project_root=project_root,
        expected_source=expected_source,
    )
    if strict:
        raise HandoverAlignmentError(format_alignment_mismatch_message(result))
    return result


def compare_handover_to_backend_next(
    handover_path: Path,
    project_root: Path,
    backend: TaskBackend,
    *,
    tag: str | None = None,
    strict: bool = False,
    expected_source: str = "backend.get_next()",
) -> HandoverAlignmentResult:
    """Compare handover Task ID to ``backend.get_next()`` for brownfield diagnostics."""
    snap = snapshot_handover(handover_path, allow_missing=True)
    if snap.missing:
        return HandoverAlignmentResult(
            checked=False,
            aligned=None,
            handover_task_id=None,
            next_task_id=0,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
            skipped_reason="handover file missing",
        )
    if snap.task_id is None:
        return HandoverAlignmentResult(
            checked=False,
            aligned=None,
            handover_task_id=None,
            next_task_id=0,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
            skipped_reason="handover missing Task ID marker",
        )

    lookup = backend.get_next(project_root, tag=tag)
    if not lookup.found or lookup.task is None:
        return HandoverAlignmentResult(
            checked=False,
            aligned=None,
            handover_task_id=snap.task_id,
            next_task_id=0,
            handover_path=handover_path,
            project_root=project_root,
            expected_source=expected_source,
            skipped_reason="task queue is empty",
        )

    return verify_handover_alignment(
        handover_path,
        lookup.task.numeric_id,
        project_root=project_root,
        strict=strict,
        expected_source=expected_source,
    )
