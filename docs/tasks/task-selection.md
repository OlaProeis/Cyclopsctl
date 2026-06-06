# Configurable task selection

Each cyclopsctl cycle needs a **parent task** for model routing, logging, and cycle metadata. Selection is configurable via `--task-source` and optional `--task-id`.

## Modes

| Mode | CLI / TOML | Behavior |
|------|------------|----------|
| `handover` | `--task-source handover` (default) | Read `# Task ID:` from `current-handover-prompt.md` and load that task via `cyclopsctl tasks show`. Falls back to `cyclopsctl tasks next` when the handover file is missing, uses queue-complete **Task ID 0**, points at an unknown id, or points at a non-pending task. |
| `sequential` | `--task-source sequential` | `cyclopsctl tasks list pending`; pick the lowest numeric pending parent id. |

**Pin one cycle:** `--task-id N` (TOML: `task_id = N`) validates the task exists and is `pending` via `cyclopsctl tasks show`, applies only to the **next** cycle, then normal `task_source` resumes.

## Loop integration

`loop.run_cycles` calls `task_selection.build_cycle_task_resolver` to obtain the selected task each cycle. The selected task drives:

- `ModelRouter.route(task_id)`
- Cycle log fields (`next_task_id`, title, outcomes)

Handover alignment compares the handover `# Task ID:` to the **expected task for this cycle** (selected handover task in default mode, lowest pending in `sequential`). When the selected id differs from `cyclopsctl tasks next`, the loop logs an **informational** warning — that does not fail the run.

The update phase should pick the next handover task via **`cyclopsctl tasks list pending`** (lowest numeric id), so handovers stay in sequential order when multiple tasks are open. See `update-handover-prompt.md` step 3.

Verification’s secondary `cyclopsctl tasks next` check still uses `get_next_task` from `TaskBackend`.

## Configuration

| Setting | TOML key | Flag | Default |
|---------|----------|------|---------|
| Task source | `task_source` | `--task-source` | `handover` |
| Pinned task | `task_id` | `--task-id` | — |

See `cyclopsctl.toml.example` and `docs/setup/cli-configuration.md`.

## Modules

| Module | Role |
|--------|------|
| `task_selection.py` | `resolve_cycle_task`, `build_cycle_task_resolver`, mismatch message formatting |
| `tasks/native_backend.py` | `list_pending`, `show`, `get_next` via native store |
| `loop.py` | Wires resolver; warns on selected vs next mismatch |
| `config.py` | `CyclopsctlConfig.task_source`, `pinned_task_id` |

## Sequential mode note

In `sequential` mode the cyclopsctl drives cycles by lowest pending id; the update-phase agent should rewrite the handover to the same lowest pending id so alignment stays consistent.

## Tests

- `tests/test_task_selection.py` — mode resolution, pinning, validation
- `tests/test_loop.py` — integration: handover/sequential routing, mismatch warnings
