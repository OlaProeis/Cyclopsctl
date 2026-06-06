# Backend wiring for init, launch, and bootstrap

`cyclopsctl init`, `cyclopsctl launch`, and `cyclopsctl bootstrap` resolve task operations through the `TaskBackend` protocol (`NativeTaskBackend`).

## Default path (native)

Greenfield `cyclopsctl init` with default `task_backend=native`:

1. Checks `CURSOR_API_KEY`.
2. Verifies `prd.md`.
3. Scaffolds `.cyclopsctl/tasks/` via `NativeTaskBackend.init_project()`.
4. Parses PRD and analyzes complexity via Cursor SDK when the queue is empty.
5. Syncs handover and generates workflow files when missing.

Launch and doctor use the same backend: queue next/list checks run through `get_task_backend()`.

## Entry points

| Module | Role |
|--------|------|
| `project_setup.py` | `resolve_setup_backend()`, `run_project_setup()`, `handle_launch_prd_change()` |
| `bootstrap.py` | `resolve_bootstrap_backend()`, `run_bootstrap()`, `sync_current_handover()` |
| `launcher.py` | `_resolve_launch_backend()`, handover repair, PRD-change dispatch |
| `doctor.py` | Native pending/next checks |
| `config.py` | `resolve_default_complexity_report_path()` → `.cyclopsctl/reports/complexity-report.json` |

## Testing

Inject `backend=` in tests to avoid live SDK calls.

## Native backend operations

`NativeTaskBackend` implements `init_project`, `add_tag`, and `use_tag` on top of `tasks/store.py`. Store imports in `native_backend.py` are lazy-loaded to avoid circular imports with `tasks/backend.py`.
