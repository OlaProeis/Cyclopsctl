# Launch attach readiness

`cyclopsctl launch` can start cycles on brownfield attach repos without requiring `prd.md`, `last-parsed-prd.json`, or a prior greenfield parse — only a runnable task queue and minimal scaffold.

## Readiness criteria

`is_project_initialized_for_launch()` in `project_setup.py` returns true when all of the following hold:

| Requirement | Check |
|-------------|--------|
| Config present | `cyclopsctl.toml` exists |
| Non-empty queue | `project_has_non_empty_tasks()` via `TaskBackend` (native `.cyclopsctl/tasks/`) |
| Handover ready | Workflow files exist **or** `handover_launch_ready()` — pending tasks and `get_next()` so launch can sync |

PRD parse state (`last-parsed-prd.json`) is **not** part of launch readiness.

## Attach context (no PRD)

`is_brownfield_attach_context()` is true when tasks exist and `prd.md` is absent.

At launch:

1. `prepare_launch_workspace()` runs **before** diagnostics — generates missing `ai-context.md` / `update-handover-prompt.md` from README/repo context and syncs handover when needed.
2. PRD-change detection is skipped; stderr prints `ATTACH_LAUNCH_INFO_MESSAGE`.
3. When `prd.md` appears later on a mature queue, the existing PRD-change flow (`handle_launch_prd_change`) still applies.

## Remediation messages

| Situation | Message |
|-----------|---------|
| Never initialized (no toml, no tasks) | `INIT_REQUIRED_MESSAGE` → `cyclopsctl init` |
| Tasks exist, missing `cyclopsctl.toml` | `INIT_MISSING_TOML_MESSAGE` → `cyclopsctl init --attach --yes` |

`format_init_required_message()` picks the appropriate text. Doctor `remediation_for_check()` uses `_suggests_attach_init()` to recommend `cyclopsctl init --attach --yes` instead of bootstrap/parse-prd when attach context applies.

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `is_project_initialized_for_launch` | `project_setup.py` | Launch gate: toml + tasks + handover |
| `project_has_non_empty_tasks` | `project_setup.py` | Backend-backed non-empty store check |
| `handover_launch_ready` | `project_setup.py` | Files present or syncable from queue |
| `is_brownfield_attach_context` | `project_setup.py` | Tasks without PRD |
| `format_init_required_message` | `project_setup.py` | Uninitialized vs missing-toml remediation |
| `prepare_launch_workspace` | `launcher.py` | Pre-diagnostics scaffold repair for attach |
| `_apply_prd_change_at_launch` | `launcher.py` | Attach info line; skip PRD handler when no PRD |
| `_suggests_attach_init` | `doctor.py` | Attach-aware remediation hints |

## Tests

- `tests/test_launch_readiness.py` — native readiness, handover repair, launch integration, doctor hints, greenfield regression

Related: `docs/setup/brownfield-attach-init.md`, `docs/testing/brownfield-readiness.md`, `docs/cli/launch-cli.md`, `docs/cli/launch-prd-change.md`.
