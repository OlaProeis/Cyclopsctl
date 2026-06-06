# Native tasks CLI

Pure-Python CRUD for the native task backend, exposed as `cyclopsctl tasks` for update-phase agents and debugging.

## Commands

```bash
cyclopsctl tasks list --project-root .
cyclopsctl tasks list pending --project-root .
cyclopsctl tasks list done --format json --project-root .
cyclopsctl tasks show <id> --format json --project-root .
cyclopsctl tasks next --format json --project-root .
cyclopsctl tasks set-status --id=<id> --status=done --project-root .
cyclopsctl tasks tags --project-root .
cyclopsctl tasks use-tag <name> --project-root .
```

Shared flags: `--project-root` (default cwd), `--tag` (default from `.cyclopsctl/tasks/state.json`), `--format json|plain`.

`list`-only and `tags`-only flags: `--plain-table` (fixed-width text instead of Rich; plain format only).

## Behavior

| Command | Logic |
|---------|--------|
| `list` | All parent tasks for the tag in a Rich table (ID, title, status, complexity, dependencies), sorted by numeric id |
| `list pending` | Non-`done` tasks (same table) |
| `list done` | Only `done` tasks (same table) |
| `list <status>` | Exact status match (`in-progress`, `review`, `cancelled`, `blocked`, `deferred`) |
| `list all` | Same as bare `list` |
| `next` | Lowest `pending` task whose dependencies are all `done` |
| `show` | Full task record JSON (`found` + `task`) for handover sync |
| `set-status` | Validates allowed statuses, updates `updatedAt` (UTC ISO), persists via `TaskStore` |
| `tags` | Every tag (phase) with total / done / pending counts; marks the active tag |
| `use-tag` | Switch the active tag context to an existing tag (validates it exists) |

JSON `list` output includes `tasks`, `tag`, and `filter`. Each task summary may include `id`, `title`, `status`, `priority`, `complexity`, `dependencies`, and `description`.

`tags` JSON output includes `activeTag` and a `tags` array (`name`, `total`, `done`, `pending`, `active`). `use-tag` JSON output is `{"activeTag": "<name>"}`. Tags map one-to-one to PRDs/phases: each `launch --prd <file>` (or in-place `prd.md` change) starts a new tag and old phases stay browsable here. See [launch-prd-change.md](../cli/launch-prd-change.md).

JSON shapes use shared parsers in `tasks/types.py` (`parse_list_payload`, `parse_next_payload`, `parse_show_detail_payload`) for consistent CLI and backend output.

## Module

`src/cyclopsctl/tasks/cli.py` — CRUD helpers, JSON/plain formatters, argparse wiring via `add_tasks_subparser()`.

`NativeTaskBackend` delegates CRUD to this module with **lazy imports** inside each method to avoid a circular import chain (`backend` → `native_backend` → `cli` → `store` → `backend`).

## Tests

`tests/test_tasks_cli.py` — CLI integration, parser compatibility, dependency-aware `next`, exit codes, tag selection.
