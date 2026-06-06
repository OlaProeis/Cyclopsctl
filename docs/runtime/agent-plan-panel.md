# TUI agent plan panel (Composer TodoWrite)

The Rich live dashboard shows Composer’s self-generated micro-plan above **Recent activity** during `cyclopsctl run`. The cyclopsctl does not create or edit these todos — it only parses and displays `TodoWrite` tool calls from the cursor-sdk activity stream.

## Data flow

```
SDK run stream → runner.parse_todo_write_from_event → on_plan_update callback
    → RichCycleLogger.apply_plan_update → RunDashboardState.agent_plan
    → render_agent_plan_section (above Recent activity)
```

| Layer | Role |
|-------|------|
| `runner.py` | `parse_todo_write_from_event`, `_tool_call_mapping_from_event`; detects `TodoWrite` / `todo_write` from flat `tool_call` and `interaction_update` events |
| `runner.consume_run_activity` | Optional `on_plan_update(todos, merge)` alongside activity lines |
| `tui.AgentPlanState` | `apply_todo_write` with replace (`merge=False`) or merge-by-id (`merge=True`; updated ids move to end) |
| `tui.RichCycleLogger` | `apply_plan_update`, `plan_callback_for_logger`; clears plan at `log_cycle_start` |
| `session.CycleSession` | Forwards `on_plan_update` through implementation and update phases in the same cycle |
| `loop.run_cycles` | Wires `plan_callback_for_logger(log)` into the default session factory |

## State and semantics

- **`AgentPlanItem`**: `id`, `content`, `status` (`pending`, `in_progress`, `completed`, `cancelled`).
- **Replace**: `merge=False` replaces the full plan list.
- **Merge**: `merge=True` updates existing ids in place and appends new ids; re-updated ids move to the end (last-update ordering).
- **Lifecycle**: Cleared at each `log_cycle_start`; persists through implementation and update within one cycle.
- **Visibility**: Section hidden when no items; capped at 12 visible rows with `+N more` overflow.

## Rendering

`render_agent_plan_section` builds a compact checklist with status icons:

| Status | Icon |
|--------|------|
| completed | green ✓ |
| in_progress | yellow ● |
| pending | dim ○ |
| cancelled | dim strikethrough ✗ |

`render_dashboard` inserts **Agent plan** above **Recent activity** only when `agent_plan.items` is non-empty. When the stream never emits `TodoWrite`, the dashboard layout is unchanged (milestone-only UI).

Plain mode does not wire `plan_callback_for_logger` — no agent plan in non-Rich output. Queue strip plain summary is emitted separately at cycle start; see `docs/runtime/tui-queue-strip.md`.

## Tests

- `tests/test_activity_stream.py` — parser variants, merge/replace, stream consumption, cycle-start clear
- `tests/test_tui.py` — section visibility, icon rendering, ordering above Recent activity, overflow cap, persistence through update phase
