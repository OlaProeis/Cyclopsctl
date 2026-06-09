"""Crash-recovery run state persistence and status inspection (task 9)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = Path(".cyclopsctl/state.json")
STATE_VERSION = 1


class RunStateStatus(str, Enum):
    """Lifecycle status stored in the run state file."""

    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True)
class RunState:
    """Minimal cyclopsctl run state for post-crash inspection."""

    cycle_number: int
    total_cycles: int
    phase: str
    task_id: int | None
    task_title: str | None
    agent_id: str | None
    run_id: str | None
    last_event: str
    status: RunStateStatus
    updated_at: str
    project_root: str
    version: int = STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        status_raw = data.get("status", RunStateStatus.RUNNING.value)
        try:
            status = RunStateStatus(str(status_raw))
        except ValueError as exc:
            raise ValueError(f"invalid status: {status_raw!r}") from exc
        return cls(
            cycle_number=int(data["cycle_number"]),
            total_cycles=int(data["total_cycles"]),
            phase=str(data.get("phase", "idle")),
            task_id=_optional_int(data.get("task_id")),
            task_title=_optional_str(data.get("task_title")),
            agent_id=_optional_str(data.get("agent_id")),
            run_id=_optional_str(data.get("run_id")),
            last_event=str(data.get("last_event", "")),
            status=status,
            updated_at=str(data.get("updated_at", "")),
            project_root=str(data.get("project_root", "")),
            version=int(data.get("version", STATE_VERSION)),
        )


@dataclass(frozen=True)
class StateReadResult:
    """Outcome of reading a run state file."""

    kind: str
    path: Path
    state: RunState | None = None
    message: str | None = None


def default_state_path(project_root: Path) -> Path:
    """Default state file location under ``project_root``."""
    return (project_root / DEFAULT_STATE_FILE).resolve()


def resolve_state_path(value: Path | None, project_root: Path) -> Path | None:
    """Resolve configured state path; ``None`` disables persistence."""
    if value is None:
        return default_state_path(project_root)
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    if path.exists():
        path.unlink()
    tmp_path.replace(path)


def serialize_state(state: RunState) -> str:
    return json.dumps(state.to_dict(), indent=2, sort_keys=True)


def write_state(path: Path, state: RunState) -> None:
    write_atomic(path, serialize_state(state))


def clear_state(path: Path) -> None:
    """Remove the state file if present."""
    if path.is_file():
        path.unlink()


def read_state(path: Path) -> StateReadResult:
    """Read run state from disk, reporting missing or corrupt files."""
    if not path.is_file():
        return StateReadResult(
            kind="missing",
            path=path,
            message="No cyclopsctl state file found.",
        )
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return StateReadResult(
            kind="corrupt",
            path=path,
            message=f"State file contains invalid JSON: {exc}",
        )
    except OSError as exc:
        return StateReadResult(
            kind="corrupt",
            path=path,
            message=f"Could not read state file: {exc}",
        )
    if not isinstance(data, dict):
        return StateReadResult(
            kind="corrupt",
            path=path,
            message="State file must contain a JSON object.",
        )
    try:
        state = RunState.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        return StateReadResult(
            kind="corrupt",
            path=path,
            message=f"State file has invalid schema: {exc}",
        )
    return StateReadResult(kind="ok", path=path, state=state)


def format_state_summary(result: StateReadResult) -> str:
    """Human-readable summary for ``cyclopsctl status``."""
    if result.kind == "missing":
        return result.message or "No cyclopsctl state file found."
    if result.kind == "corrupt":
        lines = [
            result.message or "State file is corrupt.",
            f"Path: {result.path}",
        ]
        return "\n".join(lines)

    assert result.state is not None
    state = result.state
    lines = [
        "Cyclopsctl run state",
        f"Path: {result.path}",
        f"Status: {state.status.value}",
        f"Project: {state.project_root or '—'}",
        f"Cycle: {state.cycle_number}/{state.total_cycles}",
        f"Phase: {state.phase}",
    ]
    if state.task_id is not None:
        title = f" — {state.task_title}" if state.task_title else ""
        lines.append(f"Task: {state.task_id}{title}")
    if state.agent_id:
        lines.append(f"Agent: {state.agent_id}")
    if state.run_id:
        lines.append(f"Run: {state.run_id}")
    if state.last_event:
        lines.append(f"Last event: {state.last_event}")
    if state.updated_at:
        lines.append(f"Updated: {state.updated_at}")
    if state.status is RunStateStatus.RUNNING:
        lines.append(
            "Note: status is 'running'; the process may still be active or may have crashed."
        )
    elif state.status is RunStateStatus.FAILED:
        phase = state.phase or "the run"
        lines.append(
            f"Note: the last run failed during {phase}; "
            "inspect the agent transcript for details."
        )
    return "\n".join(lines)


class RunStateTracker:
    """Persist cyclopsctl progress to disk during a run."""

    def __init__(self, path: Path | None, *, project_root: Path) -> None:
        self.path = path
        self.project_root = project_root.resolve()
        self._current: RunState | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def persist(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        phase: str,
        last_event: str,
        task_id: int | None = None,
        task_title: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        status: RunStateStatus = RunStateStatus.RUNNING,
    ) -> None:
        if not self.enabled or self.path is None:
            return
        state = RunState(
            cycle_number=cycle_number,
            total_cycles=total_cycles,
            phase=phase,
            task_id=task_id,
            task_title=task_title,
            agent_id=agent_id,
            run_id=run_id,
            last_event=last_event,
            status=status,
            updated_at=_utc_now_iso(),
            project_root=str(self.project_root),
        )
        write_state(self.path, state)
        self._current = state

    def mark_completed(self) -> None:
        if not self.enabled or self.path is None:
            return
        clear_state(self.path)
        self._current = None

    def mark_interrupted(
        self,
        *,
        cycle_number: int,
        phase: str,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if not self.enabled or self.path is None:
            return
        base = self._current
        self.persist(
            cycle_number=cycle_number,
            total_cycles=base.total_cycles if base is not None else cycle_number,
            phase=phase,
            last_event="run interrupted by user",
            task_id=base.task_id if base is not None else None,
            task_title=base.task_title if base is not None else None,
            agent_id=agent_id or (base.agent_id if base is not None else None),
            run_id=run_id or (base.run_id if base is not None else None),
            status=RunStateStatus.INTERRUPTED,
        )


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
    "DEFAULT_STATE_FILE",
    "RunState",
    "RunStateStatus",
    "RunStateTracker",
    "StateReadResult",
    "clear_state",
    "default_state_path",
    "format_state_summary",
    "read_state",
    "resolve_state_path",
    "serialize_state",
    "write_atomic",
    "write_state",
]
