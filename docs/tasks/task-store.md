# Native task storage

Native backend persistence lives under `.cyclopsctl/` with tag-keyed JSON.

## Layout

```text
.cyclopsctl/
  tasks/
    tasks.json          # tag-keyed queue (e.g. master.tasks[])
    state.json          # { "currentTag", "lastSwitched" }
  reports/
    complexity-report.json
```

## Module

`src/cyclopsctl/tasks/store.py` provides:

| API | Purpose |
|-----|---------|
| `resolve_tasks_json_path` / `resolve_tag_state_path` / `resolve_complexity_report_path` | Path resolution; optional report override |
| `load_tasks_document` / `save_tasks_document` | Full tag-keyed document read/write |
| `load_tag_tasks` / `save_tag_tasks` | Per-tag queue helpers with merge support |
| `load_tag_state` / `save_tag_state` / `set_current_tag` | Active tag pointer (defaults to `master` when missing) |
| `load_complexity_report` / `save_complexity_report` | Complexity report JSON |
| `validate_no_circular_dependencies` | Rejects cycles before persist |
| `TaskStore` | Dataclass wrapper binding `project_root` + backend |

Atomic writes reuse `write_atomic` from `state.py` (temp file + rename) for tasks, tag state, and reports.

## Errors

Controlled exceptions (not silent fallbacks on required reads):

| Exception | When |
|-----------|------|
| `TaskStoreNotFoundError` | Missing `tasks.json` or unknown tag |
| `TaskStoreCorruptError` | Invalid JSON or schema |
| `TaskStoreValidationError` | Invalid task shape before write |
| `CircularDependencyError` | Dependency cycle detected on save |

## Schema

`tasks.json` top level is tag-keyed. Each tag entry has a `tasks` array. Task objects use these fields: `id`, `title`, `description`, `details`, `testStrategy`, `priority`, `dependencies`, `status`, `complexity`, and optional extras such as `updatedAt`. Tasks are flat — there is no `subtasks` field or nesting.

**Older flat shape migration:** `load_tasks_document` accepts an older flat
`{"tasks": [...]}` document (written by pre-tag versions or by an agent that saved
the file itself) and migrates it under the default `master` tag on load. A genuine
tag-keyed document never has a list under `tasks`, so the detection is unambiguous.

## Tests

`tests/test_task_store.py` — round-trips, atomic writes, path resolution, circular dependency rejection, corruption/missing-file handling.

## Consumers

`tasks/cli.py` and `NativeTaskBackend` CRUD delegate to this module via lazy imports.
