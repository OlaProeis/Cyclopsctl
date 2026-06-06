# Handover verification

`src/cyclopsctl/verify.py` snapshots `current-handover-prompt.md` before the update phase and fails the run when the file did not advance meaningfully afterward.

## Snapshot capture

- `capture_pre_update_snapshot(path, allow_missing=False)` — returns `TimedHandoverSnapshot` (task id, normalized hash, UTC `captured_at`). Cycle 1 may use `allow_missing=True` when the handover file does not exist yet.
- `read_post_update_snapshot(path)` — strict post-update read; maps missing/unreadable files to `HandoverVerificationError`.

## Verification rules

`verify_handover_advanced(before, after, ...)` passes when:

1. **Task ID changed** — `# Task ID:` differs between before and after snapshots, or
2. **Content changed with secondary check** — hash changed but task id unchanged, and `cyclopsctl tasks next` points at a different parent task than the before snapshot.

Fails on: missing/unreadable after file, missing `# Task ID:` marker, unchanged hash + same task id, or hash changed but still stuck on the same task (secondary check agrees).

Cycle 1: when the before snapshot is `missing=True`, verification passes once the after file includes a valid Task ID marker.

## Integration

`loop.run_cycles` captures before update (after implementation), runs update, reads after snapshot, then calls verify with `project_root`, `tag`, and `get_next_task_fn` for the secondary check.

## Prompt discipline (cyclopsctl + ai-context)

- **Implementation:** `current-handover-prompt.md` + prepended `ai-context.md` (see `docs/workflow/ai-context-injection.md`). Cycle 1 uses the handover when ready — **not** `prompts/setup-ai-workflow.md` (bootstrap only).
- **Update:** `update-handover-prompt.md` only (same agent session; no ai-context re-attach).

If an implementation agent edits docs or handover files instead of `src/`/`tests/`, the wrong prompt was likely sent at startup — see `docs/runtime/run-history.md` and **Cyclopsctl prompt selection** in `ai-context.md`.

## Implementation-phase guard

Before the pre-update snapshot, the loop compares **`current-handover-prompt.md`** and **`update-handover-prompt.md`** against snapshots taken at cycle start (before agent creation).

| Mode | Agent edited handover during implementation |
|------|---------------------------------------------|
| Default | Restore file(s) from cycle-start content; log warning; continue to update phase |
| `--strict-handover` | Raise `ImplementationHandoverViolationError` before update |

This prevents implementation agents from pre-writing the handover (which caused false `Handover unchanged after update` failures when the update phase had nothing left to change).

## Errors

`HandoverVerificationError` — raised on any verification failure; stops the run without starting the next cycle.

`ImplementationHandoverViolationError` — strict mode only; implementation agent modified a handover file.

Tests: `tests/test_verify.py`, integration cases in `tests/test_loop.py`.
