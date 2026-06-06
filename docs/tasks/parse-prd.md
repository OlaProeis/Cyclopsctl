# Native PRD parsing via Cursor SDK

Turn a `prd.md` into native task JSON using only `CURSOR_API_KEY`.

## Entry points

| Surface | Location |
|---------|----------|
| Backend API | `NativeTaskBackend.parse_prd()` → `parse_prd_with_cursor()` |
| Core module | `src/cyclopsctl/tasks/parse_prd.py` |
| Prompt template | `src/cyclopsctl/templates/parse-prd-prompt.md` |
| Tests | `tests/test_parse_prd.py` |

## Decomposition prompt

`parse-prd-prompt.md` instructs the model to split the PRD into many small, flat,
self-contained tasks rather than hitting a target count. Granularity is driven by a
complexity target (size each task to score ~3–6 on the analyzer's 1–10 scale; split
anything that would score 7+) plus explicit split heuristics and a worked example.
There is **no subtask concept** — every task is a flat unit implemented in one
cycle. The prompt also forbids the agent from writing or editing any files (it must
return JSON only); the cyclopsctl owns persistence.

## Flow

1. Read PRD from disk; render template with `{{PRD_CONTENT}}` and optional `{{MAX_TASKS_SUFFIX}}` when `max_tasks` is set.
2. Resolve model via `tasks/models.resolve_parse_model`: `parse_model` or `auto` → Sonnet-class from listings; warns and uses Composer when only Composer-tier models exist; fallback `claude-sonnet-4`.
3. Create local Cursor agent (`cwd` = project root) and `send_and_wait` with stderr progress via `on_activity`.
4. Extract JSON from the response (bare object/array or markdown fence).
5. Validate: sequential ids 1…N, required fields, statuses in `ALLOWED_STATUSES` from `tasks/cli.py`. Tasks are flat — no `subtasks` field is emitted.
6. On validation/parse failure: one repair prompt retry, then `ParsePrdError`.
7. Persist to `.cyclopsctl/tasks/tasks.json` (replace or append with id remapping). The store tolerates an older flat `{"tasks": [...]}` file on load by migrating it under the default tag — see `docs/tasks/task-store.md`.
8. Write `.cyclopsctl/last-parsed-prd.json` only after successful persistence (reuses `project_setup.write_last_parsed_prd`).

## Replace vs append

- **Replace** (`append=False`): overwrites the tag’s task queue.
- **Append** (`append=True`): loads existing tag tasks, offsets new task ids and in-batch dependencies via `remap_tasks_for_append`, then merges.

## Configuration

`ParsePrdConfig` supports `max_tasks` (optional cap), `parse_model`, and `api_key`. TOML defaults live in `[tasks]` (`TasksConfig`); see `docs/tasks/task-models.md`.

## Testing

All SDK calls are mocked in CI. Inject `create_agent`, `send_fn`, and `wait_fn` into `parse_prd_with_cursor` for unit tests.
