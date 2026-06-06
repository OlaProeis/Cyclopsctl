# Post-run summary table

After `cyclopsctl run` finishes, a compact summary of verified cycles prints to stderr so operators can review outcomes without reading JSONL or scrolling the live dashboard.

## When it prints

| Exit path | Summary shown? |
|-----------|----------------|
| Normal completion (all requested cycles verified) | Yes, if any outcomes |
| Empty queue stop (partial run) | Yes, for completed cycles before stop |
| Graceful interrupt (Ctrl+C) | Yes, for cycles that finished verification before interrupt |
| Preflight / agent / verification failure | No (no verified outcomes to report) |

The summary runs **after** `managed_cycle_display` exits so Rich Live teardown completes first.

## Output modes

| Mode | Condition | Format |
|------|-----------|--------|
| Rich table | stderr is a TTY and not `--plain` | Rich `Table` via `print_run_summary` |
| Plain | `--plain` or non-TTY stderr | ASCII table to stderr |

Empty outcome lists produce no summary output unless `--resume` skipped tasks exist (skipped-only summary still prints).

## Resume skipped tasks

When `cyclopsctl run --resume` skips completed parent tasks, a second table (or plain section) lists skipped task ids and titles. The completion line on stderr also reports the skip count.

| Column | Source |
|--------|--------|
| Task ID | `SkippedTask.task_id` |
| Title | `SkippedTask.task_title` |

## Columns

| Column | Source |
|--------|--------|
| Cycle | `CycleOutcome.cycle_number` |
| Task ID | `CycleOutcome.task_id` |
| Title | `CycleOutcome.task_title` (truncated in Rich mode) |
| Model | `CycleOutcome.model_id` |
| Duration | `CycleOutcome.duration_seconds` via `format_duration` |
| Verification | `CycleOutcome.verification_result` (styled in Rich: passed/failed/interrupted) |

## Data flow

1. `_run_single_cycle` in `loop.py` records `verification_result="passed"` and cycle duration from start/complete timestamps.
2. Verified cycles append a `CycleOutcome` to `RunLoopResult.outcomes`.
3. On interrupt, `run_cycles` attaches the in-progress `RunLoopResult` to `RunInterruptedError.partial_result`.
4. `cli.py` calls `print_run_summary(result.outcomes, skipped_tasks=result.skipped_tasks, plain=config.plain)` after the display context manager and interrupt handler finish.

## Key symbols

| Symbol | Module | Role |
|--------|--------|------|
| `CycleOutcome` | `loop.py` | Per-cycle summary including duration and verification |
| `SkippedTask` | `loop.py` | Resume-skipped parent task id/title |
| `RunLoopResult.outcomes` | `loop.py` | In-memory list for the run |
| `RunLoopResult.skipped_tasks` | `loop.py` | Tasks skipped by `--resume` (no agent work) |
| `RunInterruptedError.partial_result` | `interrupt.py` | Partial outcomes on cooperative shutdown |
| `render_summary_table` | `tui.py` | Rich table builder |
| `format_plain_summary` | `tui.py` | ASCII table for plain mode |
| `print_run_summary` | `tui.py` | TTY/plain dispatch to stderr |

## Tests

- `tests/test_run_summary.py` — unit tests for rendering, duration formatting, and plain/Rich paths
- `tests/test_e2e.py` — CLI integration after successful run
- `tests/test_interrupt.py` — summary on interrupt when partial outcomes exist
