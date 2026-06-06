# Cycle orchestration

`src/cyclopsctl/loop.py` implements the main **implement → update → verify** run loop wired from `cyclopsctl run`.

## Flow

For each configured cycle (`CyclopsctlConfig.cycles`):

1. **Task selection** — `task_selection.build_cycle_task_resolver` picks the cycle task (`handover` or `sequential`; optional `--task-id` pin). Empty queue stops successfully (see `docs/tasks/empty-queue-completion.md`). Details: `docs/tasks/task-selection.md`.
2. **`ModelRouter.route(task_id)`** — select Composer or Opus from the complexity report using the **selected** task id.
3. **Selected vs next warning** — when selected id ≠ `cyclopsctl tasks next`, log a warning.
4. **Handover alignment** — `alignment.verify_handover_alignment` compares handover `# Task ID:` to the **expected task for this cycle** before agent work (warn by default; `--strict-handover` fails fast). Skipped when the handover file is missing on cycle 1. See `docs/workflow/handover-alignment.md`.
5. **`CycleSession`** — new agent for implementation; same agent for update.
6. **Implementation prompt** — cycle 1 uses `current_handover` when the handover file is ready; `first_prompt` only for `--fresh` or missing handover. Later cycles send `current_handover` unchanged. Implementation prompts include `ai-context.md`; update sends `update_handover` only (same session).
7. **Pre-update snapshot** — `verify.capture_pre_update_snapshot` (allows missing file on cycle 1).
8. **Update prompt** — send `update_handover` on the same agent (no ai-context re-attach).
9. **Post-update read + verify** — `verify.read_post_update_snapshot` and `verify.verify_handover_advanced` (secondary `cyclopsctl tasks next` when hash changes but task id does not).
10. **Structured logging** — `logging.CycleLogger` records cycle, run, snapshot, and verification events.

The loop stops on the first error (`AgentRunError`, store, prompt, session, or verification failure) without starting the next cycle.

## Public API

| Symbol | Role |
|--------|------|
| `run_cycles(config, ...)` | Execute N verified cycles; returns `RunLoopResult` |
| `CycleOutcome` / `RunLoopResult` | Per-cycle and run summaries; `empty_queue` when native queue has no next task |

Verification API lives in `verify.py`; see `docs/workflow/handover-verification.md`. Logging API in `docs/runtime/structured-logging.md`.

## Test hooks

`run_cycles` accepts injectable `get_next_task_fn`, `list_pending_tasks_fn`, `get_task_by_id_fn`, `router`, and `session_factory` for integration tests without live subprocess or SDK calls.

## CLI wiring

`src/cyclopsctl/cli.py` loads config, calls `run_cycles`, maps `AgentRunError.exit_code` (1 startup / 2 run failure) and returns 2 for other failures.

Tests: `tests/test_loop.py`.
