# Agent session and run execution

`src/cyclopsctl/runner.py` and `src/cyclopsctl/session.py` wrap the Cursor SDK for per-cycle agent lifecycle.

## Runner (`runner.py`)

- **`create_local_agent`** — `Agent.create` with `local.cwd=project_root` and optional `api_key`; raises **`AgentRunError`** on `CursorAgentError` (startup, exit code 1).
- **`send_and_wait`** — sends prompt unchanged; optionally consumes SDK activity streams via `on_activity` before `run.wait()`; returns **`SendRunResult`** (`agent_id`, `run_id`, `status`, `result`).
- **`AgentRunError`** — `RunFailureKind.STARTUP` (exit 1) vs `RunFailureKind.RUN` when `status == "error"` (exit 2).
- Injectable `create_agent`, `send_fn`, `wait_fn` for tests.

## Session (`session.py`)

- **`CycleSession`** — one cyclopsctl cycle:
  - **`start_implementation(prompt)`** — fresh `Agent.create`, then `send_and_wait` (new agent every call).
  - **`run_update(prompt)`** — same agent handle, second `send_and_wait`.
  - **`on_activity`** — optional callback forwarded to `send_and_wait` for Rich live activity (see `docs/runtime/cycle-dashboard.md`).
  - **`close()`** / context manager — disposes the agent.
- **`SessionError`** — raised if update runs before implementation.

Create a new `CycleSession` per cycle so each implementation phase gets a new agent.

Tests: `tests/test_runner.py`, `tests/test_session.py`.
