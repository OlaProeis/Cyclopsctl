# Handover alignment

`src/cyclopsctl/alignment.py` compares the `# Task ID:` in `current-handover-prompt.md` against the **expected task for this cycle** before agent creation, so mismatches are caught before spending agent time.

## Expected task by mode

| `task_source` | Expected Task ID | Notes |
|---------------|------------------|-------|
| `handover` (default) | Selected handover task (`cyclopsctl tasks show` on `# Task ID:`) | **Not** queue `next` — dependency ordering can skip ahead while lower ids are still pending |
| `sequential` | Lowest numeric pending parent id | Handover should match lowest pending or alignment warns/fails |

When handover mode falls back to `cyclopsctl tasks next` (missing handover, Task ID 0, unknown id, non-pending task), alignment is **skipped** with a logged reason.

## When it runs

After task selection and model routing, and **before** `CycleSession.start_implementation`. See `docs/runtime/cycle-orchestration.md` for the full per-cycle flow.

## Behavior

| Condition | Default (`strict_handover = false`) | `--strict-handover` |
|-----------|-------------------------------------|---------------------|
| Handover Task ID matches expected | Continue silently | Continue |
| Both IDs exist but differ | Log warning; continue | Raise `HandoverAlignmentError` |
| Handover missing on cycle 1 | Skip check; log note | Skip check; log note |
| Handover present but no `# Task ID:` marker | Skip comparison | Skip comparison |
| Handover unusable; deferred to queue next | Skip comparison | Skip comparison |

Strict failures include both task IDs, the expected source label (e.g. `selected handover`, `queue next`), and the handover file path plus project root.

## Why not always compare to queue `next`?

`cyclopsctl tasks next` uses dependency ordering and may return task **22** while task **21** is still pending (and correctly written into the handover by the update phase). In default `handover` mode the handover file is the source of truth for implementation; alignment validates the handover against that selection, not against priority `next`.

The loop still logs a **separate warning** when selected task ≠ queue `next` (informational only unless strict alignment fails on a real handover mismatch).

## Configuration

| Setting | TOML key | Flag | Default |
|---------|----------|------|---------|
| Strict alignment | `strict_handover` | `--strict-handover` | `false` |

See `cyclopsctl.toml.example` and `docs/setup/cli-configuration.md`.

## Public API

| Symbol | Role |
|--------|------|
| `verify_handover_alignment(...)` | Compare handover vs expected task id; warn or raise |
| `HandoverAlignmentResult` | Outcome: checked, aligned, skipped reason, `expected_source` |
| `HandoverAlignmentError` | Strict-mode mismatch; mapped to run failure exit code |
| `format_alignment_mismatch_message(result)` | Shared warning/error text with IDs and paths |

## Integration

`loop.run_cycles` calls `verify_handover_alignment` with `skip_when_handover_missing` on bootstrap cycle 1. Warnings go through `CycleLogger.log_warning`.

`HandoverAlignmentError` is included in `KNOWN_RUN_ERRORS` (`src/cyclopsctl/errors.py`).

Tests: `tests/test_alignment.py`, integration cases in `tests/test_loop.py`.
