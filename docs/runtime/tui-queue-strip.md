# TUI task queue strip

The Rich live dashboard and plain cycle logs show **macro** parent-task queue progress during multi-cycle runs — completed ids, the current cycle task, and upcoming pending ids.

## Data flow

```
loop._capture_queue_snapshot → list_pending_tasks_fn (TaskBackend hook)
    → build_task_queue_snapshot → log_cycle_start(queue_snapshot=...)
    → RichCycleLogger / CycleLogger → TaskQueueStripState
    → render_queue_strip_section / format_queue_strip_line
```

| Layer | Role |
|-------|------|
| `loop.py` | `_capture_queue_snapshot()` calls injectable `list_pending_tasks_fn` at cycle start; tracks `session_completed_ids` across cycles |
| `tui.TaskQueueSnapshot` | Immutable snapshot: completed ids, current id, pending ids, total, upcoming cap |
| `tui.TaskQueueStripState` | Session-local strip; `apply_snapshot()` at cycle start, `mark_task_completed()` on verify success |
| `tui.format_queue_strip_line()` | Plain one-liner, e.g. `Queue: [✓4] [✓5] [●6] [7] [8] [9] … (12 pending)` |
| `tui.render_queue_strip_section()` | Rich strip with green ✓ (done), yellow ● (current), dim upcoming ids |
| `logging.CycleLogger.log_cycle_start` | Appends queue summary to plain INFO log and JSONL `queue_summary` field |

## Layout

Dashboard section order: progress bar → **queue strip** → task title → metadata → steps → agent plan → recent activity.

## Configuration

`DEFAULT_QUEUE_STRIP_UPCOMING_CAP = 5` in `tui.py` caps upcoming pending ids shown after the current task. Overflow uses `…` plus `(N pending)` total count.

## Backend

Queue data always comes from the task backend `list_pending` hook (`resolve_run_task_hooks()` / `run_cycles(..., list_pending_tasks_fn=)`). The TUI layer reads the native queue only.

## Plain mode

Non-Rich loggers emit the queue summary as a second line on cycle start. Rich mode suppresses duplicate plain text but still writes JSONL when configured.

## Tests

- `tests/test_tui.py` — snapshot building, formatting, Rich rendering, verify-time completion updates, plain logger output
- `tests/test_loop.py` — `list_pending` injection at cycle start and session completed-id progression across cycles
