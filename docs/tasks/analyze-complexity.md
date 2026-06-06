# Native complexity analysis via Cursor SDK

Score pending native parent tasks for model routing and handover display using only `CURSOR_API_KEY`.

## Entry points

| Surface | Location |
|---------|----------|
| Backend API | `NativeTaskBackend.analyze_complexity()` → `analyze_complexity_with_cursor()` |
| Core module | `src/cyclopsctl/tasks/analyze.py` |
| Prompt template | `src/cyclopsctl/templates/analyze-complexity-prompt.md` |
| Report path | `.cyclopsctl/reports/complexity-report.json` |
| Tests | `tests/test_analyze.py` |

## Flow

1. Skip when `skip_analyze=True` or when a report already exists (`skip_if_exists=True`, default).
2. Load non-done parent tasks via `list_pending_tasks()` for the active tag.
3. Batch tasks: one agent call when count ≤ 15; larger queues split into 15-task chunks.
4. Render template with `{{TASKS_JSON}}` (id, title, description, details per task).
5. Resolve model: `analyze_model` config or `auto` → Composer standard from `Cursor.models.list()`, fallback `composer-2.5`.
6. Create local Cursor agent and `send_and_wait` with stderr progress.
7. Extract and validate `complexityAnalysis` items (scores 1–10, required metadata fields).
8. On parse failure: one repair prompt retry, then `AnalyzeComplexityError`.
9. Merge chunk results, build report with `meta.generatedAt`, `tasksAnalyzed`, `usedResearch`.
10. Persist report via `TaskStore.save_complexity_report()`.
11. Optionally backfill `complexity` onto task records in `tasks.json` (`backfill_complexity=True` by default).

## Report shape

Compatible with `routing.parse_complexity_payload()` — standard `complexityAnalysis` array shape:

```json
{
  "meta": { "generatedAt": "...", "tasksAnalyzed": N, "usedResearch": false },
  "complexityAnalysis": [
    {
      "taskId": 1,
      "taskTitle": "...",
      "complexityScore": 5,
      "reasoning": "..."
    }
  ]
}
```

## Configuration

`AnalyzeComplexityConfig` supports `analyze_model`, `api_key`, `skip_analyze`, `skip_if_exists`, `backfill_complexity`, `batch_threshold`, and `chunk_size`. TOML defaults live in `[tasks]` (`TasksConfig`); model resolution via `tasks/models.resolve_analyze_model` — see `docs/tasks/task-models.md`.

## Testing

All SDK calls are mocked in CI. Inject `create_agent`, `send_fn`, and `wait_fn` into `analyze_complexity_with_cursor` for unit tests.
