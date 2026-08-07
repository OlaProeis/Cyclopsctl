"""Tests for task 9: handover snapshotting and advancement verification."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from cyclopsctl.prompt import HandoverSnapshot, snapshot_handover
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.task_selection import TaskSelectionError
from cyclopsctl.verify import (
    AI_CONTEXT_SOFT_MAX_LINES,
    AiContextVerificationError,
    HandoverVerificationError,
    TimedHandoverSnapshot,
    capture_ai_context_snapshot,
    capture_pre_update_snapshot,
    read_post_update_snapshot,
    verify_ai_context_after_update,
    verify_handover_advanced,
)

_MINIMAL_AI_CONTEXT = """\
# Project - AI Context

## Rules (DO NOT UPDATE)
- Stay focused.

## Implementation Phase Rules
- Implement only.

## Update Phase Rules
- Update memory only.

## Project Memory
- Important fact from phase 1
"""


def _snap(
    *,
    task_id: int | None = 8,
    content_hash: str = "aaa",
    raw: str = "# Task ID: 8\n",
    missing: bool = False,
    path: Path | None = None,
) -> HandoverSnapshot:
    return HandoverSnapshot(
        path=path or Path("handover.md"),
        missing=missing,
        task_id=task_id,
        raw=raw,
        normalized=raw.strip(),
        content_hash=content_hash,
    )


def _timed(snap: HandoverSnapshot, *, captured_at: datetime | None = None) -> TimedHandoverSnapshot:
    return TimedHandoverSnapshot(
        snapshot=snap,
        captured_at=captured_at or datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc),
    )


def _next_task(task_id: int) -> NextTaskLookup:
    return NextTaskLookup.from_task(
        NextTaskResult(
            task_id=str(task_id),
            title=f"Task {task_id}",
            status="pending",
            priority="high",
            complexity=7,
            tag="master",
        )
    )


def test_capture_pre_update_snapshot_includes_timestamp_and_fields(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    path.write_text("# Task ID: 9\n\nWork.\n", encoding="utf-8")

    timed = capture_pre_update_snapshot(path)

    assert timed.captured_at.tzinfo is not None
    assert timed.snapshot.task_id == 9
    assert timed.snapshot.content_hash == snapshot_handover(path).content_hash
    assert timed.snapshot.missing is False


def test_capture_pre_update_snapshot_allow_missing(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    timed = capture_pre_update_snapshot(path, allow_missing=True)
    assert timed.snapshot.missing is True
    assert timed.snapshot.task_id is None


def test_read_post_update_snapshot_reads_marker_and_hash(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    path.write_text("# Task ID: 10\n\nDone.\n", encoding="utf-8")

    snap = read_post_update_snapshot(path)

    assert snap.task_id == 10
    assert snap.content_hash == snapshot_handover(path).content_hash


def test_read_post_update_snapshot_missing_file_raises(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    with pytest.raises(HandoverVerificationError, match="missing or unreadable"):
        read_post_update_snapshot(path)


def test_verify_passes_on_task_id_change():
    before = _timed(_snap(task_id=8, content_hash="aaa"))
    after = _snap(task_id=9, content_hash="aaa", raw="# Task ID: 9\n")
    verify_handover_advanced(before, after)


def test_verify_passes_on_content_change_with_secondary_check():
    before = _timed(_snap(task_id=8, content_hash="aaa", raw="# Task ID: 8\nold"))
    after = _snap(task_id=8, content_hash="bbb", raw="# Task ID: 8\nnew")
    verify_handover_advanced(
        before,
        after,
        project_root=Path("/proj"),
        get_next_task_fn=lambda _root, tag=None: _next_task(9),
    )


def test_verify_passes_on_content_change_with_empty_queue_secondary_check():
    before = _timed(_snap(task_id=8, content_hash="aaa", raw="# Task ID: 8\nold"))
    after = _snap(task_id=8, content_hash="bbb", raw="# Task ID: 8\nnew")
    verify_handover_advanced(
        before,
        after,
        project_root=Path("/proj"),
        get_next_task_fn=lambda _root, tag=None: NextTaskLookup.empty(tag="master"),
    )


def test_verify_fails_when_content_changed_but_secondary_stuck():
    before = _timed(_snap(task_id=8, content_hash="aaa"))
    after = _snap(task_id=8, content_hash="bbb", raw="# Task ID: 8\nnew")
    with pytest.raises(HandoverVerificationError, match="stuck on the same task"):
        verify_handover_advanced(
            before,
            after,
            project_root=Path("/proj"),
            get_next_task_fn=lambda _root, tag=None: _next_task(8),
        )


def test_verify_fails_when_content_changed_without_secondary_check():
    before = _timed(_snap(task_id=8, content_hash="aaa"))
    after = _snap(task_id=8, content_hash="bbb", raw="# Task ID: 8\nnew")
    with pytest.raises(HandoverVerificationError, match="secondary backend next check"):
        verify_handover_advanced(before, after)


def test_verify_fails_when_unchanged():
    snap = _snap(task_id=8, content_hash="same")
    with pytest.raises(HandoverVerificationError, match="unchanged"):
        verify_handover_advanced(_timed(snap), snap)


def test_verify_fails_when_after_missing():
    before = _timed(_snap())
    after = _snap(missing=True, task_id=None, content_hash="", raw="")
    with pytest.raises(HandoverVerificationError, match="missing or unreadable"):
        verify_handover_advanced(before, after)


def test_verify_fails_when_missing_marker_after_update():
    before = _timed(_snap(task_id=8))
    after = _snap(task_id=None, content_hash="bbb", raw="# Session\nno marker\n")
    with pytest.raises(HandoverVerificationError, match="missing required Task ID marker"):
        verify_handover_advanced(before, after)


def test_verify_cycle_one_before_missing_passes_with_marker():
    before = _timed(_snap(missing=True, task_id=None, content_hash="", raw=""))
    after = _snap(task_id=9, content_hash="new", raw="# Task ID: 9\n")
    verify_handover_advanced(before, after)


def test_verify_cycle_one_before_missing_fails_without_marker():
    before = _timed(_snap(missing=True, task_id=None, content_hash="", raw=""))
    after = _snap(task_id=None, content_hash="new", raw="# Session\n")
    with pytest.raises(HandoverVerificationError, match="missing required Task ID marker"):
        verify_handover_advanced(before, after)


def test_verify_secondary_check_backend_next_error():
    before = _timed(_snap(task_id=8, content_hash="aaa"))
    after = _snap(task_id=8, content_hash="bbb", raw="# Task ID: 8\nnew")

    def failing_next(_root: Path, *, tag: str | None = None) -> NextTaskLookup:
        raise TaskSelectionError("backend next failed")

    with pytest.raises(HandoverVerificationError, match="Secondary backend next check failed"):
        verify_handover_advanced(
            before,
            after,
            project_root=Path("/proj"),
            get_next_task_fn=failing_next,
        )


def test_read_post_update_snapshot_unreadable_file(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    path.write_bytes(b"\xff\xfe invalid utf-8 \x00")
    with pytest.raises(HandoverVerificationError, match="missing or unreadable"):
        read_post_update_snapshot(path)


def test_guard_restores_current_handover_when_impl_modified(tmp_path: Path):
    from cyclopsctl.verify import guard_handover_files_after_implementation

    current = tmp_path / "current-handover-prompt.md"
    update = tmp_path / "update-handover-prompt.md"
    original = "# Task ID: 8\n\nOriginal.\n"
    current.write_text(original, encoding="utf-8")
    update.write_text("# Update template\n", encoding="utf-8")

    at_start = capture_pre_update_snapshot(current)
    update_at_start = capture_pre_update_snapshot(update)

    current.write_text("# Task ID: 8\n\nAgent edited during impl.\n", encoding="utf-8")

    result = guard_handover_files_after_implementation(
        current_handover_path=current,
        update_handover_path=update,
        current_handover_at_start=at_start,
        update_handover_at_start=update_at_start,
        strict=False,
    )

    assert result.current_handover_modified is True
    assert result.current_handover_restored is True
    assert snapshot_handover(current).content_hash == at_start.snapshot.content_hash


def test_guard_strict_raises_when_impl_modified_handover(tmp_path: Path):
    from cyclopsctl.verify import (
        ImplementationHandoverViolationError,
        guard_handover_files_after_implementation,
    )

    current = tmp_path / "current-handover-prompt.md"
    update = tmp_path / "update-handover-prompt.md"
    current.write_text("# Task ID: 8\n\nOriginal.\n", encoding="utf-8")
    update.write_text("# Update template\n", encoding="utf-8")

    at_start = capture_pre_update_snapshot(current)
    update_at_start = capture_pre_update_snapshot(update)
    current.write_text("# Task ID: 8\n\nAgent edited.\n", encoding="utf-8")

    with pytest.raises(ImplementationHandoverViolationError, match="implementation"):
        guard_handover_files_after_implementation(
            current_handover_path=current,
            update_handover_path=update,
            current_handover_at_start=at_start,
            update_handover_at_start=update_at_start,
            strict=True,
        )


def test_guard_restores_update_template_when_impl_modified(tmp_path: Path):
    from cyclopsctl.verify import guard_handover_files_after_implementation

    current = tmp_path / "current-handover-prompt.md"
    update = tmp_path / "update-handover-prompt.md"
    current.write_text("# Task ID: 8\n\nOriginal.\n", encoding="utf-8")
    template = "# Update template\n\nDo not edit.\n"
    update.write_text(template, encoding="utf-8")

    at_start = capture_pre_update_snapshot(current)
    update_at_start = capture_pre_update_snapshot(update)
    update.write_text("# Update template\n\nCorrupted by impl.\n", encoding="utf-8")

    result = guard_handover_files_after_implementation(
        current_handover_path=current,
        update_handover_path=update,
        current_handover_at_start=at_start,
        update_handover_at_start=update_at_start,
        strict=False,
    )

    assert result.update_handover_modified is True
    assert result.update_handover_restored is True
    assert snapshot_handover(update).content_hash == update_at_start.snapshot.content_hash


def test_capture_ai_context_snapshot_reads_lines(tmp_path: Path):
    path = tmp_path / "ai-context.md"
    path.write_text(_MINIMAL_AI_CONTEXT, encoding="utf-8")

    snap = capture_ai_context_snapshot(path)

    assert snap.missing is False
    assert snap.line_count == len(_MINIMAL_AI_CONTEXT.splitlines())
    assert "Project Memory" in snap.raw


def test_capture_ai_context_snapshot_allow_missing(tmp_path: Path):
    path = tmp_path / "missing-ai-context.md"
    snap = capture_ai_context_snapshot(path, allow_missing=True)
    assert snap.missing is True
    assert snap.line_count == 0


def test_verify_ai_context_additive_update_passes(tmp_path: Path):
    path = tmp_path / "ai-context.md"
    path.write_text(_MINIMAL_AI_CONTEXT, encoding="utf-8")
    before = capture_ai_context_snapshot(path)
    path.write_text(
        _MINIMAL_AI_CONTEXT + "- New durable fact from this task\n",
        encoding="utf-8",
    )

    result = verify_ai_context_after_update(before)

    assert result.warnings == ()


def test_verify_ai_context_fails_when_protected_section_removed(tmp_path: Path):
    path = tmp_path / "ai-context.md"
    path.write_text(_MINIMAL_AI_CONTEXT, encoding="utf-8")
    before = capture_ai_context_snapshot(path)
    path.write_text(
        "# Project - AI Context\n\n## Rules (DO NOT UPDATE)\n- Stay focused.\n",
        encoding="utf-8",
    )

    with pytest.raises(AiContextVerificationError, match="protected section"):
        verify_ai_context_after_update(before)


def test_verify_ai_context_fails_on_destructive_shrink(tmp_path: Path):
    path = tmp_path / "ai-context.md"
    lines = [
        "# Project - AI Context",
        "",
        "## Rules (DO NOT UPDATE)",
        "- Stay focused.",
        "",
        "## Implementation Phase Rules",
        "- Implement only.",
        "",
        "## Update Phase Rules",
        "- Update memory only.",
        "",
        "## Project Memory",
    ]
    lines.extend(f"- Durable fact {i}" for i in range(90))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = capture_ai_context_snapshot(path)
    assert before.line_count >= 80

    # Keep protected markers but wipe Project Memory (phase-style rewrite).
    path.write_text(
        "\n".join(lines[:12]) + "\n- Only current phase\n",
        encoding="utf-8",
    )

    with pytest.raises(AiContextVerificationError, match="shrank destructively"):
        verify_ai_context_after_update(before)


def test_verify_ai_context_fails_when_file_deleted(tmp_path: Path):
    path = tmp_path / "ai-context.md"
    path.write_text(_MINIMAL_AI_CONTEXT, encoding="utf-8")
    before = capture_ai_context_snapshot(path)
    path.unlink()

    with pytest.raises(AiContextVerificationError, match="missing or unreadable"):
        verify_ai_context_after_update(before)


def test_verify_ai_context_warns_when_over_soft_max(tmp_path: Path):
    path = tmp_path / "ai-context.md"
    header = (
        "# Project - AI Context\n\n"
        "## Rules (DO NOT UPDATE)\n- Stay focused.\n\n"
        "## Implementation Phase Rules\n- Implement only.\n\n"
        "## Update Phase Rules\n- Update memory only.\n\n"
        "## Project Memory\n"
    )
    pad = "\n".join(f"- pad {i}" for i in range(AI_CONTEXT_SOFT_MAX_LINES))
    path.write_text(header + pad + "\n", encoding="utf-8")
    before = capture_ai_context_snapshot(path)

    result = verify_ai_context_after_update(before)

    assert len(result.warnings) == 1
    assert "soft max" in result.warnings[0]

