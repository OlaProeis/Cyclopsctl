# Graceful interrupt (Ctrl+C)

`cyclopsctl run` handles SIGINT cooperatively so operators can stop a long run without corrupting the terminal or mutating handover files.

## Behavior

On Ctrl+C during `cyclopsctl run`:

1. **`RunInterruptController`** sets `stop_requested` and runs registered cleanup callbacks.
2. The active **`CycleSession`** is closed (agent disposed) if one is open.
3. **`run_cycles`** checks `stop_requested` at safe points and raises **`RunInterruptedError`** with `cycle_number`, `phase`, `agent_id`, and `run_id` when known.
4. No further cycles start after interruption.
5. **`managed_cycle_display`** marks the dashboard `interrupted`, stops Rich `Live` idempotently, and restores the terminal.
6. The CLI logs interrupt context and exits with code **130** (`INTERRUPT_EXIT_CODE`).

Handover files and task queue state are not modified by the interrupt path itself; interruption is checked before verification and between phases where possible.

## Modules

| Module | Role |
|--------|------|
| `interrupt.py` | `RunInterruptController`, `RunInterruptedError`, `INTERRUPT_EXIT_CODE` |
| `loop.py` | Interrupt checkpoints in `run_cycles`; tracks active session |
| `cli.py` | Registers/restores SIGINT handler around bridge + display + loop |
| `tui.py` | `RunDashboardState.mark_interrupted()`, Rich `Live.stop()` on interrupt |
| `errors.py` | Maps `RunInterruptedError` to exit code 130 |
| `logging.py` | `CycleLogger.log_interrupt()` structured event |

## Signal handler contract

The SIGINT handler only requests shutdown (`stop_requested`) and invokes lightweight cleanup callbacks. The main loop performs authoritative checks and raises `RunInterruptedError` so cleanup stays predictable in normal Python control flow.

## Tests

`tests/test_interrupt.py` covers session cleanup, handover preservation, no follow-on cycles, CLI exit code 130, and Rich Live teardown. `tests/test_tui.py` verifies interrupted dashboard state.
