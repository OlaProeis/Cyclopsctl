# Crash recovery state file

The cyclopsctl persists minimal run progress to disk during `cyclopsctl run` so operators can inspect where a run stopped after a crash, kill, or unexpected failure.

## Default location

- **Path:** `.cyclopsctl/state.json` under `project_root` (gitignored)
- **Config:** `state_file` in `cyclopsctl.toml`, or `--state-file PATH` / `--no-state` on `cyclopsctl run`

Set `state_file = ""` in TOML or pass `--no-state` to disable persistence entirely.

## State schema

Each write stores:

| Field | Purpose |
|-------|---------|
| `cycle_number` / `total_cycles` | Progress within the configured run |
| `phase` | Current step (`idle`, `resolve`, `implementation`, `update`, `verify`, `complete`, …) |
| `task_id` / `task_title` | Selected parent task when known |
| `agent_id` / `run_id` | Active Cursor agent run when known |
| `last_event` | Human-readable milestone (e.g. `implementation finished`, `implementation failed`) |
| `status` | `running`, `interrupted`, `failed`, or `completed` |
| `updated_at` | UTC ISO timestamp |
| `project_root` | Absolute project path for status display |

Writes use a temp file and atomic rename (`write_atomic`).

## Lifecycle

| Event | State file behavior |
|-------|---------------------|
| During run | Updated after meaningful loop steps (`RunStateTracker.persist`) |
| Successful finish (all cycles or empty queue) | File **cleared** |
| Graceful Ctrl+C | Marked `interrupted` with final context (no auto-resume) |
| Agent run failure (`AgentRunError`) | Marked `failed` with the failing phase and the `agent_id` / `run_id` from the exception |
| Verification / other failure | Left at its last `running` checkpoint for post-crash inspection |

On `AgentRunError` during the implementation or update phase, `loop.py` persists `status=failed` (`last_event="<phase> failed"`) with the agent/run ids before re-raising, so `cyclopsctl status` and the failure report can point at the transcript. There is **no** automatic agent resume; the file is read-only diagnostics.

## `cyclopsctl status`

Read-only inspection command:

```bash
cyclopsctl status --project-root /path/to/project
```

Always exits **0**:

- **Missing file** — reports no state found
- **Corrupt JSON** — reports parse/schema error and path
- **Valid file** — prints a human-readable summary (cycle, phase, task, agent, last event). A `running` status notes the process may still be active or crashed; a `failed` status notes the phase that failed and to inspect the agent transcript

Supports `--config`, `--project-root`, and `--state-file` like other path-aware subcommands.

## Modules

| Module | Role |
|--------|------|
| `state.py` | `RunState`, `RunStateTracker`, atomic I/O, `read_state`, `format_state_summary` |
| `loop.py` | Persists state at cycle checkpoints; marks `failed` on `AgentRunError` (`_persist_phase_failure`); clears on normal completion |
| `cli.py` | `status` subcommand; marks interrupted on SIGINT exit path |
| `config.py` | `state_file` resolution, disable via empty string |

## Tests

`tests/test_state.py` — write/read/clear, corrupt handling, config paths.  
`tests/test_status_cli.py` — CLI with present, absent, corrupt, and disabled state.  
`tests/test_loop.py` — clear on success, `failed` status on agent failure, disabled mode.  
`tests/test_failure_report.py` — `failed` round-trip, summary note, and `_persist_phase_failure`.
