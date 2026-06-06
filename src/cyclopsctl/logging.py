"""Structured cycle logs (text and optional JSONL) (task 10)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cyclopsctl.tui import TaskQueueSnapshot

logger = logging.getLogger("cyclopsctl.cycle")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


@dataclass(frozen=True)
class CycleLogRecord:
    """Structured summary emitted once per verified cycle."""

    cycle_number: int
    total_cycles: int
    next_task_id: int
    next_task_title: str
    model_id: str
    agent_id: str
    impl_run_id: str
    impl_status: str
    update_run_id: str
    update_status: str
    handover_before_task_id: int | None
    handover_after_task_id: int | None
    handover_before_hash: str
    handover_after_hash: str
    verification_result: str
    started_at: datetime
    completed_at: datetime
    git_diff_summary: str | None = None


@dataclass
class CycleLogger:
    """Emit per-cycle text logs and optional JSONL records."""

    jsonl_path: Path | None = None
    _logger: logging.Logger = field(default_factory=lambda: logger, repr=False)

    def _emit(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        self._logger.info(message)
        if self.jsonl_path is not None and payload is not None:
            self._append_jsonl(payload)

    def _append_jsonl(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, default=str, sort_keys=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def log_cycle_start(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        next_task_id: int,
        next_task_title: str,
        model_id: str,
        handover_task_id: int | None,
        timestamp: datetime | None = None,
        queue_snapshot: TaskQueueSnapshot | None = None,
    ) -> datetime:
        ts = timestamp or _utc_now()
        queue_line = ""
        if queue_snapshot is not None:
            from cyclopsctl.tui import TaskQueueStripState, format_queue_strip_line

            strip = TaskQueueStripState()
            strip.apply_snapshot(queue_snapshot)
            queue_line = format_queue_strip_line(strip)
        message = (
            f"[cycle {cycle_number}/{total_cycles}] start "
            f"next_task_id={next_task_id} "
            f"handover_task_id={handover_task_id} "
            f"model={model_id} "
            f"task_title={next_task_title!r} "
            f"timestamp={_iso_timestamp(ts)}"
        )
        if queue_line:
            message = f"{message}\n{queue_line}"
        payload: dict[str, Any] = {
            "event": "cycle_start",
            "timestamp": _iso_timestamp(ts),
            "cycle_number": cycle_number,
            "total_cycles": total_cycles,
            "next_task_id": next_task_id,
            "next_task_title": next_task_title,
            "handover_task_id": handover_task_id,
            "model_id": model_id,
        }
        if queue_line:
            payload["queue_summary"] = queue_line
        self._emit(message, payload=payload)
        return ts

    def log_run_complete(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        phase: str,
        agent_id: str,
        run_id: str,
        status: str,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or _utc_now()
        self._emit(
            (
                f"[cycle {cycle_number}/{total_cycles}] {phase} "
                f"agent_id={agent_id} run_id={run_id} status={status} "
                f"timestamp={_iso_timestamp(ts)}"
            ),
            payload={
                "event": f"{phase}_complete",
                "timestamp": _iso_timestamp(ts),
                "cycle_number": cycle_number,
                "total_cycles": total_cycles,
                "phase": phase,
                "agent_id": agent_id,
                "run_id": run_id,
                "status": status,
            },
        )

    def log_handover_snapshot(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        label: str,
        path: Path,
        task_id: int | None,
        content_hash: str,
        missing: bool,
        captured_at: datetime | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or _utc_now()
        self._emit(
            (
                f"[cycle {cycle_number}/{total_cycles}] snapshot {label} "
                f"path={path} task_id={task_id} hash={content_hash[:12]} "
                f"missing={missing} captured_at={_iso_timestamp(captured_at) if captured_at else None} "
                f"timestamp={_iso_timestamp(ts)}"
            ),
            payload={
                "event": "handover_snapshot",
                "timestamp": _iso_timestamp(ts),
                "cycle_number": cycle_number,
                "total_cycles": total_cycles,
                "label": label,
                "path": str(path),
                "task_id": task_id,
                "content_hash": content_hash,
                "missing": missing,
                "captured_at": _iso_timestamp(captured_at) if captured_at else None,
            },
        )

    def log_verification_result(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        result: str,
        before_task_id: int | None,
        after_task_id: int | None,
        before_hash: str,
        after_hash: str,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or _utc_now()
        self._emit(
            (
                f"[cycle {cycle_number}/{total_cycles}] verification "
                f"result={result} before_task_id={before_task_id} "
                f"after_task_id={after_task_id} before_hash={before_hash[:12]} "
                f"after_hash={after_hash[:12]} timestamp={_iso_timestamp(ts)}"
            ),
            payload={
                "event": "verification",
                "timestamp": _iso_timestamp(ts),
                "cycle_number": cycle_number,
                "total_cycles": total_cycles,
                "result": result,
                "before_task_id": before_task_id,
                "after_task_id": after_task_id,
                "before_hash": before_hash,
                "after_hash": after_hash,
            },
        )

    def log_cycle(self, record: CycleLogRecord) -> None:
        """Emit the consolidated per-cycle record after successful verification."""
        payload = {
            "event": "cycle_complete",
            "timestamp": _iso_timestamp(record.completed_at),
            **asdict(record),
        }
        message = (
            f"[cycle {record.cycle_number}/{record.total_cycles}] complete "
            f"next_task_id={record.next_task_id} "
            f"handover_before_task_id={record.handover_before_task_id} "
            f"handover_after_task_id={record.handover_after_task_id} "
            f"model={record.model_id} agent_id={record.agent_id} "
            f"impl_run_id={record.impl_run_id} impl_status={record.impl_status} "
            f"update_run_id={record.update_run_id} update_status={record.update_status} "
            f"verification_result={record.verification_result} "
            f"started_at={_iso_timestamp(record.started_at)} "
            f"completed_at={_iso_timestamp(record.completed_at)}"
        )
        if record.git_diff_summary:
            summary_line = record.git_diff_summary.splitlines()[0]
            message = f"{message} git_diff_summary={summary_line!r}"
        self._emit(message, payload=payload)

    def log_dry_run_plan(
        self,
        *,
        cycle_number: int,
        total_cycles: int,
        next_task_id: int,
        next_task_title: str,
        model_id: str,
        handover_task_id: int | None,
        aligned: bool | None,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or _utc_now()
        self._emit(
            (
                f"[cycle {cycle_number}/{total_cycles}] dry-run plan "
                f"next_task_id={next_task_id} handover_task_id={handover_task_id} "
                f"model={model_id} aligned={aligned} "
                f"task_title={next_task_title!r} timestamp={_iso_timestamp(ts)}"
            ),
            payload={
                "event": "dry_run_plan",
                "timestamp": _iso_timestamp(ts),
                "cycle_number": cycle_number,
                "total_cycles": total_cycles,
                "next_task_id": next_task_id,
                "next_task_title": next_task_title,
                "handover_task_id": handover_task_id,
                "model_id": model_id,
                "aligned": aligned,
            },
        )

    def log_warning(self, message: str, **context: Any) -> None:
        context_text = " ".join(f"{key}={value!r}" for key, value in context.items())
        detail = f"{message} {context_text}".strip()
        self._logger.warning(detail)
        if self.jsonl_path is not None:
            self._append_jsonl(
                {
                    "event": "warning",
                    "timestamp": _iso_timestamp(),
                    "message": message,
                    **context,
                }
            )

    def log_error(self, message: str, **context: Any) -> None:
        context_text = " ".join(f"{key}={value!r}" for key, value in context.items())
        detail = f"{message} {context_text}".strip()
        self._logger.error(detail)
        if self.jsonl_path is not None:
            self._append_jsonl(
                {
                    "event": "error",
                    "timestamp": _iso_timestamp(),
                    "message": message,
                    **context,
                }
            )

    def log_interrupt(
        self,
        *,
        cycle_number: int,
        phase: str,
        agent_id: str | None = None,
        run_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or _utc_now()
        self._emit(
            (
                f"[interrupt] cycle_number={cycle_number} phase={phase} "
                f"agent_id={agent_id or ''} run_id={run_id or ''} "
                f"timestamp={_iso_timestamp(ts)}"
            ),
            payload={
                "event": "interrupt",
                "timestamp": _iso_timestamp(ts),
                "cycle_number": cycle_number,
                "phase": phase,
                "agent_id": agent_id,
                "run_id": run_id,
            },
        )


DEFAULT_CYCLE_LOGGER = CycleLogger()


def log_cycle(record: CycleLogRecord, *, cycle_logger: CycleLogger | None = None) -> None:
    (cycle_logger or DEFAULT_CYCLE_LOGGER).log_cycle(record)


def log_warning(message: str, *, cycle_logger: CycleLogger | None = None, **context: Any) -> None:
    (cycle_logger or DEFAULT_CYCLE_LOGGER).log_warning(message, **context)


def log_error(message: str, *, cycle_logger: CycleLogger | None = None, **context: Any) -> None:
    (cycle_logger or DEFAULT_CYCLE_LOGGER).log_error(message, **context)
