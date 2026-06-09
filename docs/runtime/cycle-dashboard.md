# Rich live cycle dashboard

`src/cyclopsctl/tui.py` provides a Rich-based live terminal dashboard for `cyclopsctl run`, showing cycle progress, current phase, and task metadata during interactive runs.

## Components

| Symbol | Role |
|--------|------|
| `RunDashboardState` | Mutable state: cycle number, task id/title, model, phase, agent/run ids, per-step status, queue strip, agent plan, recent activity lines |
| `RichCycleLogger` | Subclass of `CycleLogger`; updates state on log hooks; suppresses plain text when TUI is active |
| `activity_callback_for_logger` | Returns `RichCycleLogger.append_activity` in Rich mode, else `None` |
| `plan_callback_for_logger` | Returns `RichCycleLogger.apply_plan_update` in Rich mode, else `None` |
| `render_dashboard(state, console_width=None, now=None)` | Builds a Rich `Panel` with progress bar, metadata table, step checklist, and a "still running" heartbeat |
| `render_heartbeat(state, now=None)` | Returns a "still running" line when an active run phase has gone quiet, else `None` |
| `format_elapsed_short(seconds)` | Compact duration formatter (`45s`, `3m 20s`, `1h 5m`) |
| `managed_cycle_display(plain=False, jsonl_path=None)` | Context manager yielding a `CycleLogger` and managing `rich.live.Live` |
| `use_rich_display(plain=False)` | Returns whether Rich mode should activate (not plain and stderr is a TTY) |

## Step checklist

Steps advance as the run loop emits structured log events:

1. Resolve next task — done at `log_cycle_start`
2. Implementation — running during implementation phase
3. Snapshot handover — after implementation completes
4. Update phase — after before-snapshot
5. Verify handover — after update and after-snapshot

## Logging behavior

`RichCycleLogger` overrides `_emit` to skip INFO text lines when `suppress_plain_logs=True` (default). Optional JSONL output via `jsonl_path` is unchanged — every event still appends structured JSON when configured.

Plain mode and non-TTY stderr yield a standard `CycleLogger` with no Live display.

## Live agent activity

During implementation and update phases, `send_and_wait` (via `CycleSession.on_activity`) streams cursor-sdk run events into the dashboard when Rich mode is active.

| Behavior | Detail |
|----------|--------|
| Source | `run.stream()` / `messages()` / `observe()` / `events()` — first supported API wins |
| Tool line format | `tool · file · summary` (paths/summaries truncated at ingest) |
| Prose line format | Summary-only assistant/status/thinking text; no ingest truncation |
| Prose coalescing | Consecutive prose fragments merge into one logical buffer entry via `merge_prose_activity`; tool rows start a new entry |
| Prose display | Truncate at 240 chars, then soft-wrap to live console width minus panel padding (`format_activity_for_display`) |
| Buffer | Last 8 **logical** entries in `RunDashboardState.activity_lines` (not raw stream fragments) |
| Refresh | Throttled to ~4 Hz (`ACTIVITY_REFRESH_INTERVAL_SECONDS = 0.25`) |
| Fallback | Silent milestone-only UI when streaming is absent or errors |
| Plain mode | No activity callback wired (`activity_callback_for_logger` returns `None`) |

`render_dashboard` shows a **Recent activity** section when the buffer is non-empty. Activity clears at each `log_cycle_start`. The live display passes `console.size.width` so prose wraps to the terminal rather than a fixed narrow column.

Parsing and stream consumption live in `runner.py` (`format_activity_from_event`, `is_prose_activity_line`, `merge_prose_activity`, `consume_run_activity`). Coalescing and width-aware rendering live in `tui.py` (`RunDashboardState.append_activity`, `format_activity_for_display`, `activity_panel_content_width`). `loop.py` passes the Rich logger callback into the default `CycleSession` factory.

## Stall heartbeat

A long but legitimate agent step (e.g. a slow test suite) can produce no stream
events for minutes, making the dashboard look hung. To reassure the user that
`cyclopsctl` is still alive, `render_heartbeat` adds a yellow **still running**
line whenever the run is in an active SDK phase (`implementation` or `update`)
and no new activity has arrived for at least `HEARTBEAT_IDLE_THRESHOLD_SECONDS`
(15s):

```
⏳ still running - no new activity for 45s (3m 20s elapsed)
```

| Aspect | Detail |
|--------|--------|
| Active phases | `ACTIVE_RUN_PHASES = {implementation, update}` |
| Idle/elapsed tracking | `RunDashboardState.last_activity_monotonic` / `phase_started_monotonic`, set by `RichCycleLogger` on phase start and each activity/plan event |
| Display threshold | `HEARTBEAT_IDLE_THRESHOLD_SECONDS = 15` |
| Live updates without events | `managed_cycle_display` runs `Live` with `auto_refresh=True` at `HEARTBEAT_REFRESH_PER_SECOND` so the elapsed counter ticks even while the stream is silent |
| Reset | Cleared via `end_active_phase()` when a run phase completes |

## Task queue strip

At each `log_cycle_start`, `loop.py` captures pending parent tasks via the backend `list_pending` hook and renders a compact strip (completed ✓, current ●, upcoming ids). Plain mode logs the same one-line summary. See `docs/runtime/tui-queue-strip.md`.

## Agent plan panel

Composer `TodoWrite` tool calls are parsed from the same activity stream and rendered as an **Agent plan** checklist above **Recent activity**. See `docs/runtime/agent-plan-panel.md` for parser shapes, merge/replace semantics, lifecycle, and tests.

## Usage

```python
from cyclopsctl.tui import managed_cycle_display

with managed_cycle_display(plain=False, jsonl_path=None) as cycle_logger:
    run_cycles(config, cycle_logger=cycle_logger)
```

CLI wiring (`--plain` flag and default TTY selection) is described in `docs/runtime/cycle-orchestration.md` once integrated.

Tests: `tests/test_tui.py`.
