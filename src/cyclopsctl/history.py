"""Cross-invocation run history and resume-aware startup (task 13)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cyclopsctl.prompt import HandoverSnapshot, snapshot_handover
from cyclopsctl.state import write_atomic

DEFAULT_HISTORY_FILE = Path(".cyclopsctl/run-history.json")
HISTORY_VERSION = 1


class HistoryMismatchError(RuntimeError):
    """Current handover Task ID disagrees with persisted run history."""


@dataclass(frozen=True)
class RunHistory:
    """Persisted cyclopsctl history across CLI invocations."""

    completed_cycle_task_ids: list[int]
    last_handover_task_id: int | None
    last_handover_content_hash: str | None
    last_run_at: str
    bootstrap_complete: bool
    project_root: str
    version: int = HISTORY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunHistory:
        raw_ids = data.get("completed_cycle_task_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError("completed_cycle_task_ids must be a list")
        completed_cycle_task_ids = [int(item) for item in raw_ids]
        return cls(
            completed_cycle_task_ids=completed_cycle_task_ids,
            last_handover_task_id=_optional_int(data.get("last_handover_task_id")),
            last_handover_content_hash=_optional_str(data.get("last_handover_content_hash")),
            last_run_at=str(data.get("last_run_at", "")),
            bootstrap_complete=bool(data.get("bootstrap_complete", False)),
            project_root=str(data.get("project_root", "")),
            version=int(data.get("version", HISTORY_VERSION)),
        )


@dataclass(frozen=True)
class HistoryReadResult:
    """Outcome of reading a run history file."""

    kind: str
    path: Path
    history: RunHistory | None = None
    message: str | None = None


@dataclass(frozen=True)
class HistoryValidationResult:
    """Outcome of comparing current handover to persisted history."""

    checked: bool
    aligned: bool | None
    handover_task_id: int | None
    expected_handover_task_id: int | None
    handover_path: Path
    history_path: Path
    skipped_reason: str | None = None


@dataclass(frozen=True)
class StartupResolution:
    """Whether cycle 1 should use first-prompt or current handover."""

    use_first_prompt_for_cycle_one: bool
    resumed: bool
    history: RunHistory | None
    history_path: Path | None
    validation: HistoryValidationResult | None = None


def default_history_path(project_root: Path) -> Path:
    """Default history file location under ``project_root``."""
    return (project_root / DEFAULT_HISTORY_FILE).resolve()


def resolve_history_path(value: Path | None, project_root: Path) -> Path | None:
    """Resolve configured history path; ``None`` disables persistence."""
    if value is None:
        return default_history_path(project_root)
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def serialize_history(history: RunHistory) -> str:
    return json.dumps(history.to_dict(), indent=2, sort_keys=True)


def write_history(path: Path, history: RunHistory) -> None:
    write_atomic(path, serialize_history(history))


def clear_history(path: Path) -> None:
    """Remove the history file if present."""
    if path.is_file():
        path.unlink()


def read_history(path: Path) -> HistoryReadResult:
    """Read run history from disk, reporting missing or corrupt files."""
    if not path.is_file():
        return HistoryReadResult(
            kind="missing",
            path=path,
            message="No cyclopsctl run history file found.",
        )
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return HistoryReadResult(
            kind="corrupt",
            path=path,
            message=f"History file contains invalid JSON: {exc}",
        )
    except OSError as exc:
        return HistoryReadResult(
            kind="corrupt",
            path=path,
            message=f"Could not read history file: {exc}",
        )
    if not isinstance(data, dict):
        return HistoryReadResult(
            kind="corrupt",
            path=path,
            message="History file must contain a JSON object.",
        )
    try:
        history = RunHistory.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        return HistoryReadResult(
            kind="corrupt",
            path=path,
            message=f"History file has invalid schema: {exc}",
        )
    return HistoryReadResult(kind="ok", path=path, history=history)


def format_history_mismatch_message(result: HistoryValidationResult) -> str:
    """Build a warning message for history vs handover mismatch."""
    return (
        f"Handover was updated since the last run (handover Task ID "
        f"{result.handover_task_id}, history recorded "
        f"{result.expected_handover_task_id}). Proceeding with the handover "
        f"(handover: {result.handover_path}; history: {result.history_path})"
    )


def validate_handover_against_history(
    handover_path: Path,
    history: RunHistory,
    *,
    history_path: Path,
    strict: bool = False,
) -> HistoryValidationResult:
    """
    Compare current handover Task ID to the last recorded handover task id.

    When the handover file is missing or lacks a Task ID marker, the check is
    skipped (startup should not treat the run as resumed in this case).

    Mismatches are always non-fatal: the handover file is the operator's source
    of truth (including manual or agent updates between runs). ``strict`` is
    accepted for API compatibility but does not raise here — use per-cycle
    handover alignment (``verify_handover_alignment``) for fail-fast behavior.
    """
    del strict  # history mismatch never fails the run; handover wins
    snap = snapshot_handover(handover_path, allow_missing=False)

    if snap.missing:
        return HistoryValidationResult(
            checked=False,
            aligned=None,
            handover_task_id=None,
            expected_handover_task_id=history.last_handover_task_id,
            handover_path=handover_path,
            history_path=history_path,
            skipped_reason="handover file missing",
        )

    handover_task_id = snap.task_id
    if handover_task_id is None:
        return HistoryValidationResult(
            checked=False,
            aligned=None,
            handover_task_id=None,
            expected_handover_task_id=history.last_handover_task_id,
            handover_path=handover_path,
            history_path=history_path,
            skipped_reason="handover missing Task ID marker",
        )

    expected = history.last_handover_task_id
    if expected is None:
        return HistoryValidationResult(
            checked=False,
            aligned=None,
            handover_task_id=handover_task_id,
            expected_handover_task_id=None,
            handover_path=handover_path,
            history_path=history_path,
            skipped_reason="history has no recorded handover task id",
        )

    if handover_task_id == expected:
        return HistoryValidationResult(
            checked=True,
            aligned=True,
            handover_task_id=handover_task_id,
            expected_handover_task_id=expected,
            handover_path=handover_path,
            history_path=history_path,
        )

    return HistoryValidationResult(
        checked=True,
        aligned=False,
        handover_task_id=handover_task_id,
        expected_handover_task_id=expected,
        handover_path=handover_path,
        history_path=history_path,
    )


def handover_ready_for_implementation(handover_path: Path) -> bool:
    """Return True when ``current-handover-prompt.md`` can drive cycle 1."""
    snap = snapshot_handover(handover_path, allow_missing=True)
    return not snap.missing and snap.task_id is not None and snap.task_id > 0


def resolve_startup(
    *,
    project_root: Path,
    handover_path: Path,
    history_path: Path | None,
    fresh: bool = False,
    strict: bool = False,
) -> StartupResolution:
    """
    Decide whether cycle 1 uses ``first-prompt`` or ``current-handover``.

    ``first-prompt`` (e.g. ``prompts/setup-ai-workflow.md``) is for explicit
    greenfield bootstrap only. When a usable ``current-handover-prompt.md``
    exists, cycle 1 always uses it unless ``--fresh`` forces bootstrap.

    Run history affects resume warnings only; it no longer selects the prompt.
    """
    history: RunHistory | None = None
    validation: HistoryValidationResult | None = None

    if history_path is not None:
        read_result = read_history(history_path)
        if read_result.kind == "ok" and read_result.history is not None:
            history = read_result.history
            validation = validate_handover_against_history(
                handover_path,
                history,
                history_path=history_path,
                strict=strict,
            )

    if fresh:
        return StartupResolution(
            use_first_prompt_for_cycle_one=True,
            resumed=False,
            history=history,
            history_path=history_path,
            validation=validation,
        )

    if handover_ready_for_implementation(handover_path):
        resumed = (
            history is not None
            and history.bootstrap_complete
            and validation is not None
            and validation.checked
            and validation.aligned is True
        )
        return StartupResolution(
            use_first_prompt_for_cycle_one=False,
            resumed=resumed,
            history=history,
            history_path=history_path,
            validation=validation,
        )

    return StartupResolution(
        use_first_prompt_for_cycle_one=True,
        resumed=False,
        history=history,
        history_path=history_path,
        validation=validation,
    )


def load_completed_task_ids(history_path: Path | None) -> frozenset[int]:
    """Return verified completed parent task ids from run history, if any."""
    if history_path is None:
        return frozenset()
    read_result = read_history(history_path)
    if read_result.kind != "ok" or read_result.history is None:
        return frozenset()
    return frozenset(read_result.history.completed_cycle_task_ids)


def merge_completed_task_ids(
    prior_ids: list[int] | frozenset[int],
    new_ids: list[int],
) -> list[int]:
    """Union prior and newly verified task ids for resume persistence."""
    return sorted(set(prior_ids) | set(new_ids))


def build_history_from_run(
    *,
    project_root: Path,
    completed_task_ids: list[int],
    final_handover: HandoverSnapshot,
) -> RunHistory:
    """Create a history record after a successful cyclopsctl run."""
    return RunHistory(
        completed_cycle_task_ids=list(completed_task_ids),
        last_handover_task_id=final_handover.task_id,
        last_handover_content_hash=final_handover.content_hash,
        last_run_at=_utc_now_iso(),
        bootstrap_complete=True,
        project_root=str(project_root.resolve()),
    )


def format_history_summary(result: HistoryReadResult) -> str:
    """Human-readable summary for ``cyclopsctl status``."""
    if result.kind == "missing":
        return result.message or "No cyclopsctl run history file found."
    if result.kind == "corrupt":
        lines = [
            result.message or "History file is corrupt.",
            f"Path: {result.path}",
        ]
        return "\n".join(lines)

    assert result.history is not None
    history = result.history
    lines = [
        "Cyclopsctl run history",
        f"Path: {result.path}",
        f"Bootstrap complete: {history.bootstrap_complete}",
        f"Project: {history.project_root or '—'}",
    ]
    if history.completed_cycle_task_ids:
        ids = ", ".join(str(task_id) for task_id in history.completed_cycle_task_ids)
        lines.append(f"Last completed task IDs: {ids}")
    if history.last_handover_task_id is not None:
        lines.append(f"Last handover task ID: {history.last_handover_task_id}")
    if history.last_handover_content_hash:
        lines.append(f"Last handover hash: {history.last_handover_content_hash[:12]}…")
    if history.last_run_at:
        lines.append(f"Last run: {history.last_run_at}")
    return "\n".join(lines)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "DEFAULT_HISTORY_FILE",
    "HistoryMismatchError",
    "HistoryReadResult",
    "HistoryValidationResult",
    "RunHistory",
    "StartupResolution",
    "build_history_from_run",
    "clear_history",
    "default_history_path",
    "format_history_mismatch_message",
    "format_history_summary",
    "load_completed_task_ids",
    "merge_completed_task_ids",
    "read_history",
    "resolve_history_path",
    "resolve_startup",
    "serialize_history",
    "validate_handover_against_history",
    "write_history",
]
