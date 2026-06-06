# Empty queue completion

When the native queue has no pending parent tasks, `TaskBackend.get_next()` returns `found: false`. The cyclopsctl treats that as **normal completion**, not a failure.

## API

`get_next_task` returns `NextTaskLookup`:

| Field | Meaning |
|-------|---------|
| `found` | `True` when a parent task is available |
| `task` | `NextTaskResult` when `found` is `True`; otherwise `None` |
| `tag` | Active tag from native store |

Store or backend errors are reserved for corrupt JSON, missing required files, or invalid task objects when `found` is `True`.

## Run loop

`run_cycles` checks `lookup.found` at the start of each cycle:

- **Empty queue** — log a warning with completed/requested cycle counts, set `RunLoopResult.empty_queue = True`, return immediately (no agent session started).
- **Partial run** — if `--cycles` exceeds remaining work, stop when the queue is exhausted without error.

`cyclopsctl run` exits **0** and prints how many verified cycles completed in the run, e.g. `Task queue empty (tag='master'). Completed 2 verified cycle(s) in this run at ...`

## Verification

During handover verification, a secondary queue `next` that returns an empty queue is treated as advancement (all work complete).

## Files

- `src/cyclopsctl/tasks/types.py` — `NextTaskLookup`, shared dataclasses
- `src/cyclopsctl/tasks/native_backend.py` — native `get_next`
- `src/cyclopsctl/loop.py` — early stop on empty queue
- `src/cyclopsctl/cli.py` — success message and exit 0
- `src/cyclopsctl/verify.py` — secondary empty-queue handling

Tests: `tests/test_tasks_cli.py`, `tests/test_loop.py`, `tests/test_verify.py`.
