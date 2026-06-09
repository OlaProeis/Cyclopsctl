# Run history and resume-aware startup

The cyclopsctl persists cross-invocation run history so a new `cyclopsctl run` can warn when the handover disagrees with the last successful run. **Cycle-1 prompt selection is separate:** when `current-handover-prompt.md` is ready, cycle 1 always uses it — not `first_prompt` / `setup-ai-workflow.md`.

Complements crash-recovery state (task 9) and handover alignment (task 6); it does not replace them.

## Default location

- **Path:** `.cyclopsctl/run-history.json` under `project_root` (gitignored with `.cyclopsctl/`)
- **Config:** `history_file` in `cyclopsctl.toml`, or `--history-file PATH` / `--no-history` on `cyclopsctl run`

Set `history_file = ""` in TOML or pass `--no-history` to disable persistence.

## History schema

| Field | Purpose |
|-------|---------|
| `completed_cycle_task_ids` | Task IDs from verified cycles in the last successful run |
| `last_handover_task_id` | `# Task ID:` in handover after that run |
| `last_handover_content_hash` | Content hash of handover after verification |
| `last_run_at` | UTC ISO timestamp of last successful run |
| `bootstrap_complete` | `true` after at least one verified cycle has completed |
| `project_root` | Absolute project path |

Writes use atomic rename via `state.write_atomic`.

## Startup resolution

At the start of `run_cycles`, `history.resolve_startup` decides cycle-1 prompt source:

| Condition | Cycle 1 implementation prompt |
|-----------|-------------------------------|
| Usable `current-handover-prompt.md` (file exists with `# Task ID:`) | `current_handover` |
| Handover missing or no Task ID marker | `first_prompt` (greenfield bootstrap only) |
| `--fresh` | `first_prompt` (history ignored; use only when intentionally re-bootstrapping) |

Run history affects **resume warnings** only—it no longer selects the cycle-1 prompt. If a handover file is ready, cycle 1 always uses it unless `--fresh` forces bootstrap.

When resuming (history aligned with handover), the handover Task ID must match `last_handover_task_id` in history. If the handover was updated between runs (manual edit, another agent, or new phase work), a mismatch **logs a warning and proceeds** with the current handover — the handover file is authoritative. `--strict-handover` applies only to **per-cycle** handover vs selected-task alignment, not to stale run history.

Bootstrap cycle 1 (first-prompt path) skips handover alignment and allows a missing handover file. Cycle 1 with an existing handover runs full alignment and requires the handover file.

## Lifecycle

| Event | History file behavior |
|-------|----------------------|
| Successful run with ≥1 verified cycle | Updated with final handover snapshot and completed task IDs |
| Empty queue after partial completion | Updated same as success |
| Mid-run failure after ≥1 verified cycle | Best-effort update with completed task IDs from verified cycles (`loop._persist_run_history_after_failure`); original error still propagates |
| Interrupt (SIGINT) after ≥1 verified cycle | Same best-effort partial update as mid-run failure |
| Failure before any verified cycle | Unchanged |
| `--fresh` | Ignored for startup only; successful runs still update history unless `--no-history` |

## CLI and status

- **`cyclopsctl run --fresh`** — force first-prompt bootstrap for cycle 1
- **`cyclopsctl run --resume`** — skip parent tasks already in `completed_cycle_task_ids` before each cycle (no implement/update/verify for skipped ids); requires history enabled
- **`cyclopsctl status`** — prints crash state and run history summaries when enabled

`--resume` is mutually exclusive with `--fresh` and `--no-history`. Skipped tasks are logged at cycle start and listed in the post-run summary when present.

## Task-level resume (`--resume`)

Distinct from cycle-1 startup resolution (handover vs first-prompt). With `--resume`, the loop loads completed ids from run history and, before each requested cycle:

1. Resolves the next task via the configured `task_source` (with `exclude_task_ids` when advancing past skips).
2. If the resolved task id is in the completed set, logs a skip and resolves again without consuming a cycle slot.
3. Runs implement → update → verify only for non-skipped tasks.

Skipped tasks never enter the agent session or verification. `--cycles` counts **executed** verified cycles only.

On successful runs with `--resume`, persisted `completed_cycle_task_ids` merges prior history ids with newly verified ids (union, sorted).

| Flag interaction | Result |
|------------------|--------|
| `--resume` + `--no-history` | `ConfigError` at startup |
| `--resume` + `--fresh` | `ConfigError` at startup |
| `--resume` without `--no-history` | Default or custom history file required |

## Key modules

| Module | Role |
|--------|------|
| `history.py` | Schema, read/write, `load_completed_task_ids`, `merge_completed_task_ids`, `resolve_startup`, handover validation |
| `loop.py` | Startup resolution, resume skip loop, `_implementation_prompt`, post-run persistence, `_persist_run_history_after_failure` on abort |
| `task_selection.py` | `exclude_task_ids` support when advancing past skipped tasks |
| `config.py` | `history_file`, `fresh`, `resume` settings and validation |
| `cli.py` | `--fresh`, `--resume`, `--history-file`, `--no-history`; status output |
| `tui.py` | Post-run skipped-task summary table |

## Tests

`tests/test_history.py` (schema and resolution), `tests/test_history_loop.py` (fresh vs resumed multi-invocation flows), and `tests/test_task_resume.py` (`--resume` skip behavior, flag validation, history merge).
