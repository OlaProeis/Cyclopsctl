# Structured cycle logging

`src/cyclopsctl/logging.py` emits per-cycle text logs (and optional JSONL) for manual recovery and debugging.

## CycleLogger

`CycleLogger(jsonl_path=None)` writes INFO-level text lines to the `cyclopsctl.cycle` logger. When `jsonl_path` is set, each event is also appended as a JSON line.

Event helpers (called from `loop.run_cycles`):

| Method | When |
|--------|------|
| `log_cycle_start` | After task selection and routing; includes next task id, handover task id, model |
| `log_run_complete` | After implementation and update `send`/`wait` |
| `log_handover_snapshot` | Before and after update snapshots |
| `log_verification_result` | After successful verification |
| `log_cycle` | Consolidated `CycleLogRecord` at cycle end |

Module-level `log_warning` and `log_error` accept keyword context for actionable messages. `cli.py` calls `log_error` on configuration, agent, and orchestration failures.

## CycleLogRecord

Frozen summary with cycle number, task ids (next + handover before/after), model, agent/run ids, statuses, hashes, verification result, and start/complete timestamps.

## Usage

Default logger is used automatically by `run_cycles`. For JSONL output, pass a custom logger:

```python
from pathlib import Path
from cyclopsctl.logging import CycleLogger

run_cycles(config, cycle_logger=CycleLogger(jsonl_path=Path("cycles.jsonl")))
```

Tests: `tests/test_logging.py`.
