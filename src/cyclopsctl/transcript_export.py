"""Transcript sidecar export for run observability (task 16)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cyclopsctl.state import write_atomic


def write_transcript_sidecar(
    export_dir: Path,
    *,
    cycle_number: int,
    task_id: int,
    agent_id: str,
    run_id: str,
    model: str,
    status: str,
) -> Path:
    """Write a compact JSON sidecar for manual transcript lookup."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"cycle-{cycle_number}.json"
    payload: dict[str, Any] = {
        "cycle": cycle_number,
        "task_id": task_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "model": model,
        "status": status,
    }
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path
