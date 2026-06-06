# Task backend abstraction

The cyclopsctl uses a **`TaskBackend` protocol** so init, launch, bootstrap, doctor, and the run loop share one interface over native JSON storage (`.cyclopsctl/tasks/`).

## Protocol

`TaskBackend` is a `Protocol` in `src/cyclopsctl/tasks/backend.py` with these methods:

| Method | Purpose |
|--------|---------|
| `init_project` | Scaffold native task storage and cyclopsctl Cursor rules |
| `parse_prd` | Turn a PRD into task JSON via Cursor SDK |
| `analyze_complexity` | Generate complexity report via Cursor SDK |
| `list_pending` | Pending parent tasks |
| `get_next` | Next eligible task |
| `show` | Full task record for handover sync |
| `set_status` | Update task status |
| `add_tag` / `use_tag` | Tag management |
| `current_tag` | Active tag name |
| `complexity_report_path` | Report path for routing |
| `tasks_exist` | Whether a tag has tasks |

Shared dataclasses (`NextTaskResult`, `TaskShowDetail`, `NextTaskLookup`) live in `src/cyclopsctl/tasks/types.py`.

## Factory and config

`get_task_backend(TaskBackendConfig)` returns `NativeTaskBackend` (the only supported backend in v1.0).

Resolution order (`resolve_task_backend`):

1. CLI `task_backend`
2. TOML `[tasks].backend`
3. Top-level TOML `task_backend`
4. When `project_root` is provided: infer from on-disk storage (native `tasks.json` → `native`)
5. Default: `native`

## Modules

| Module | Role |
|--------|------|
| `tasks/backend.py` | Protocol, factory, config resolution |
| `tasks/types.py` | Shared dataclasses |
| `tasks/native_backend.py` | Native store, CRUD, parse, and analyze |
| `tasks/store.py` | `.cyclopsctl/` JSON persistence |
| `tasks/cli.py` | `cyclopsctl tasks` subcommands |
| `tasks/parse_prd.py` | PRD → tasks via Cursor SDK |
| `tasks/analyze.py` | Complexity report via Cursor SDK |

## NativeTaskBackend

`NativeTaskBackend` implements the full protocol:

- `init_project` scaffolds `.cyclopsctl/tasks/` and installs cyclopsctl Cursor rules
- `parse_prd` / `analyze_complexity` call Cursor SDK helpers (`CURSOR_API_KEY` required)
- CRUD delegates to `tasks/cli.py` with lazy imports to avoid circular dependencies
- `complexity_report_path()` returns `.cyclopsctl/reports/complexity-report.json`
- `current_tag()` reads `.cyclopsctl/tasks/state.json` (`currentTag`), default `master`

## Doctor integration

`DoctorConfig.task_backend` and `LaunchConfig.task_backend` resolve through `resolve_task_backend()`.

Native doctor checks:

- Readable `tasks.json` (or greenfield NOTE when no queue yet)
- Complexity report when tasks exist
- Queue `next` / `list pending` via `TaskBackend` (no external CLIs)

## Tests

- `tests/test_task_backend.py` — selection precedence, protocol contract, type import paths
- `tests/test_tasks_cli.py` — native CRUD and CLI integration
- `tests/test_task_store.py` — persistence and path resolution
