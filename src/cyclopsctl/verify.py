"""Pre/post update handover snapshot comparison (task 9)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cyclopsctl.prompt import HandoverSnapshot, PromptError, snapshot_handover
from cyclopsctl.task_selection import TaskSelectionError
from cyclopsctl.tasks.cli import TasksCliError
from cyclopsctl.tasks.types import NextTaskLookup

GetNextTaskFn = Callable[..., NextTaskLookup]

# Whole-project ai-context.md integrity (post-update).
AI_CONTEXT_PROTECTED_MARKERS: tuple[str, ...] = (
    "Rules (DO NOT UPDATE)",
    "Implementation Phase Rules",
    "Update Phase Rules",
)
AI_CONTEXT_SOFT_MAX_LINES = 1000
AI_CONTEXT_SHRINK_MIN_PRIOR_LINES = 80
AI_CONTEXT_SHRINK_RATIO = 0.40


class HandoverVerificationError(RuntimeError):
    """Handover file did not advance meaningfully after the update phase."""


class AiContextVerificationError(RuntimeError):
    """ai-context.md lost protected sections or was destructively rewritten."""


class ImplementationHandoverViolationError(RuntimeError):
    """Implementation agent modified a handover file it must not touch."""


@dataclass(frozen=True)
class HandoverGuardResult:
    """Outcome of the post-implementation handover guard."""

    current_handover_modified: bool
    update_handover_modified: bool
    current_handover_restored: bool
    update_handover_restored: bool


def _handover_content_changed(
    path: Path,
    before: HandoverSnapshot,
) -> bool:
    """Return whether ``path`` differs from the cycle-start snapshot."""
    if before.missing:
        return path.is_file()
    current = snapshot_handover(path, allow_missing=True)
    return current.content_hash != before.content_hash


def _restore_handover_file(path: Path, before: HandoverSnapshot) -> None:
    """Restore ``path`` to the cycle-start handover content."""
    if before.missing:
        if path.is_file():
            path.unlink()
        return
    path.write_text(before.raw, encoding="utf-8")


def guard_handover_files_after_implementation(
    *,
    current_handover_path: Path,
    update_handover_path: Path,
    current_handover_at_start: "TimedHandoverSnapshot",
    update_handover_at_start: "TimedHandoverSnapshot",
    strict: bool,
) -> HandoverGuardResult:
    """
    Detect and undo handover edits performed during the implementation phase.

    Agents must not modify ``current-handover-prompt.md`` or
    ``update-handover-prompt.md`` until the update phase. When ``strict`` is
    True, raise ``ImplementationHandoverViolationError`` instead of restoring.
    """
    before_current = current_handover_at_start.snapshot
    before_update = update_handover_at_start.snapshot

    current_modified = _handover_content_changed(current_handover_path, before_current)
    update_modified = _handover_content_changed(update_handover_path, before_update)

    if strict and (current_modified or update_modified):
        parts: list[str] = []
        if current_modified:
            parts.append(f"{current_handover_path} (current handover)")
        if update_modified:
            parts.append(f"{update_handover_path} (update template)")
        raise ImplementationHandoverViolationError(
            "Implementation agent modified handover file(s) during implementation: "
            + ", ".join(parts)
            + ". Handover files may only change in the update phase."
        )

    current_restored = False
    update_restored = False
    if current_modified:
        _restore_handover_file(current_handover_path, before_current)
        current_restored = True
    if update_modified:
        _restore_handover_file(update_handover_path, before_update)
        update_restored = True

    return HandoverGuardResult(
        current_handover_modified=current_modified,
        update_handover_modified=update_modified,
        current_handover_restored=current_restored,
        update_handover_restored=update_restored,
    )


@dataclass(frozen=True)
class TimedHandoverSnapshot:
    """Pre-update handover state with capture timestamp."""

    snapshot: HandoverSnapshot
    captured_at: datetime


def capture_pre_update_snapshot(
    path: Path,
    *,
    allow_missing: bool = False,
) -> TimedHandoverSnapshot:
    """Snapshot task id, normalized content hash, and timestamp before update."""
    snap = snapshot_handover(path, allow_missing=allow_missing)
    captured_at = datetime.now(timezone.utc)
    return TimedHandoverSnapshot(snapshot=snap, captured_at=captured_at)


def read_post_update_snapshot(path: Path) -> HandoverSnapshot:
    """Read handover after update; map read failures to verification errors."""
    try:
        snap = snapshot_handover(path, allow_missing=False)
    except (PromptError, OSError, UnicodeDecodeError) as exc:
        raise HandoverVerificationError(
            f"Handover file missing or unreadable after update: {path}"
        ) from exc
    return snap


def _secondary_next_suggests_different_task(
    before: HandoverSnapshot,
    *,
    project_root: Path,
    tag: str | None,
    get_next_task_fn: GetNextTaskFn,
) -> bool:
    """Return True when backend next points at a different parent task."""
    try:
        lookup = get_next_task_fn(project_root, tag=tag)
    except (TasksCliError, TaskSelectionError) as exc:
        raise HandoverVerificationError(
            f"Secondary backend next check failed: {exc}"
        ) from exc

    if not lookup.found:
        return True

    next_task = lookup.task
    assert next_task is not None

    if before.task_id is None:
        return True

    return next_task.numeric_id != before.task_id


@dataclass(frozen=True)
class AiContextSnapshot:
    """Pre/post update state for ``ai-context.md`` integrity checks."""

    path: Path
    missing: bool
    raw: str
    line_count: int
    captured_at: datetime


@dataclass(frozen=True)
class AiContextVerifyResult:
    """Outcome of post-update ai-context checks (warnings only; failures raise)."""

    warnings: tuple[str, ...] = ()


def capture_ai_context_snapshot(
    path: Path,
    *,
    allow_missing: bool = True,
) -> AiContextSnapshot:
    """Read ai-context content and line count before the update phase."""
    captured_at = datetime.now(timezone.utc)
    if not path.is_file():
        if allow_missing:
            return AiContextSnapshot(
                path=path,
                missing=True,
                raw="",
                line_count=0,
                captured_at=captured_at,
            )
        raise AiContextVerificationError(f"ai-context not found (required): {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AiContextVerificationError(
            f"ai-context missing or unreadable: {path}"
        ) from exc
    return AiContextSnapshot(
        path=path,
        missing=False,
        raw=raw,
        line_count=_ai_context_line_count(raw),
        captured_at=captured_at,
    )


def _ai_context_line_count(raw: str) -> int:
    if not raw:
        return 0
    return len(raw.splitlines())


def _missing_protected_markers(raw: str) -> list[str]:
    return [marker for marker in AI_CONTEXT_PROTECTED_MARKERS if marker not in raw]


def verify_ai_context_after_update(
    before: AiContextSnapshot,
    *,
    require_ai_context: bool = False,
) -> AiContextVerifyResult:
    """
    Fail when update-phase edits gut whole-project ai-context memory.

    Raises ``AiContextVerificationError`` when the file disappears (if it
    existed before or is required), protected rule sections are missing, or
    the file shrinks by ≥40% from a substantial prior size (≥80 lines).

    Returns soft warnings (e.g. over ~1000 lines) without failing.
    """
    path = before.path
    warnings: list[str] = []

    if not path.is_file():
        if before.missing and not require_ai_context:
            return AiContextVerifyResult()
        raise AiContextVerificationError(
            f"ai-context missing or unreadable after update: {path}"
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AiContextVerificationError(
            f"ai-context missing or unreadable after update: {path}"
        ) from exc

    after_lines = _ai_context_line_count(raw)
    missing = _missing_protected_markers(raw)
    if missing:
        raise AiContextVerificationError(
            "ai-context lost protected section(s) after update: "
            + ", ".join(missing)
            + f" ({path})"
        )

    if (
        not before.missing
        and before.line_count >= AI_CONTEXT_SHRINK_MIN_PRIOR_LINES
        and after_lines < before.line_count * (1.0 - AI_CONTEXT_SHRINK_RATIO)
    ):
        raise AiContextVerificationError(
            "ai-context shrank destructively after update "
            f"(before={before.line_count} lines, after={after_lines} lines, "
            f"path={path}). Whole-project memory must not be rewritten for "
            "the current phase alone."
        )

    if after_lines > AI_CONTEXT_SOFT_MAX_LINES:
        warnings.append(
            f"ai-context exceeds soft max of {AI_CONTEXT_SOFT_MAX_LINES} lines "
            f"(now {after_lines}): prune duplicates/redundant bullets, keep "
            f"durable whole-project facts ({path})"
        )

    return AiContextVerifyResult(warnings=tuple(warnings))


def verify_handover_advanced(
    before: TimedHandoverSnapshot,
    after: HandoverSnapshot,
    *,
    project_root: Path | None = None,
    tag: str | None = None,
    get_next_task_fn: GetNextTaskFn | None = None,
) -> None:
    """
    Compare pre/post snapshots and fail when the handover did not advance.

    Pass when the task id changed, or when content hash changed substantively
    and a secondary backend next check suggests a different active task.
    """
    before_snap = before.snapshot

    if after.missing:
        raise HandoverVerificationError(
            f"Handover file missing or unreadable after update: {after.path}"
        )

    if after.task_id is None:
        raise HandoverVerificationError(
            "Handover after update is missing required Task ID marker"
        )

    if before_snap.missing:
        return

    if after.task_id != before_snap.task_id:
        return

    if after.content_hash == before_snap.content_hash:
        raise HandoverVerificationError(
            "Handover unchanged after update "
            f"(task_id={before_snap.task_id!r}, hash={before_snap.content_hash[:12]}...)"
        )

    if project_root is None or get_next_task_fn is None:
        raise HandoverVerificationError(
            "Handover content changed but task id unchanged; "
            "secondary backend next check is required"
        )

    if _secondary_next_suggests_different_task(
        before_snap,
        project_root=project_root,
        tag=tag,
        get_next_task_fn=get_next_task_fn,
    ):
        return

    raise HandoverVerificationError(
        "Handover content changed but still stuck on the same task "
        f"(task_id={before_snap.task_id!r}, hash={before_snap.content_hash[:12]}...)"
    )
