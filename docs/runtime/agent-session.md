# Agent session and run execution

`src/cyclopsctl/runner.py` and `src/cyclopsctl/session.py` wrap the Cursor SDK for per-cycle agent lifecycle.

## Runner (`runner.py`)

- **`create_local_agent`** — `Agent.create` with `local.cwd=project_root` and optional `api_key`; raises **`AgentRunError`** on `CursorAgentError` (startup, exit code 1).
- **`send_and_wait`** — sends prompt unchanged; optionally consumes SDK activity streams via `on_activity` before `run.wait()`; returns **`SendRunResult`** (`agent_id`, `run_id`, `status`, `result`).
- **`AgentRunError`** — `RunFailureKind.STARTUP` (exit 1) vs `RunFailureKind.RUN` when `status == "error"` (exit 2). Carries `agent_id`, `run_id`, `result_detail`, optional `diagnostic_detail` (display-only conversation context), and `phase` (`"startup"` / `"implementation"` / `"update"`) for failure diagnostics (see `docs/runtime/failure-diagnostics.md`). Empty `result_detail` on a run error is treated as transient when `retry_on = "transient"` (see `docs/runtime/transient-retry.md`).
- Injectable `create_agent`, `send_fn`, `wait_fn` for tests.

## Session (`session.py`)

- **`CycleSession`** — one cyclopsctl cycle:
  - **`start_implementation(prompt)`** — fresh `Agent.create`, then `send_and_wait` (new agent every call).
  - **`run_update(prompt)`** — same agent handle, second `send_and_wait`.
  - **`on_activity`** — optional callback forwarded to `send_and_wait` for Rich live activity (see `docs/runtime/cycle-dashboard.md`).
  - **`close()`** / context manager — disposes the agent.
- **`SessionError`** — raised if update runs before implementation.

Create a new `CycleSession` per cycle so each implementation phase gets a new agent.

## Windows SDK bridge (`sdk_bridge.py`)

The Cursor SDK auto-launches a local bridge subprocess on first use. On Windows its discovery reader fails (`WinError 10038` while polling pipe stderr via `selectors`), so the cyclopsctl launches the bridge itself via `managed_sdk_bridge(project_root)` (entered in `cli.py` around `run_cycles`).

- **`launch_bridge_for_windows`** — starts `cursor-sdk-bridge`, reads the discovery line with a blocking `readline` (Windows-safe), and exports `CURSOR_SDK_BRIDGE_URL` / `CURSOR_SDK_BRIDGE_TOKEN` so the SDK attaches to it.
- **`needs_windows_bridge_bootstrap`** — only bootstraps on `win32` when those env vars are not already set; non-Windows platforms let the SDK launch its own bridge.

### Env-fallback default client (required for `run.wait()`)

When a bridge is supplied through env vars, the SDK treats it as *caller-supplied* and builds its default client with `allow_api_key_env_fallback=False`. Run-scoped RPCs (`WaitLiveRun` / `ObserveRun` / `CancelRun`) are then classified as cloud-routed and rejected with `missing_api_key`, because those RPCs never carry an explicit `apiKey`. This made `run.wait()` fail on Windows even though `Agent.create` / `send` succeeded.

`install_env_fallback_default_client(url, auth_token)` builds a `Client(..., allow_api_key_env_fallback=True)` and installs it as the SDK default — the same trust model the SDK uses for a bridge it launched itself (a single-user local CLI with the user's own `CURSOR_API_KEY`). `launch_bridge_for_windows` calls it after discovery and stores the client on `ManagedBridge.client`; `ManagedBridge.close()` resets it via `close_default_client()`. The installer is injectable for tests.

### Long-run timeout (`WaitLiveRun` / activity stream)

The cursor-sdk client defaults are 60s unary / 600s stream. `run.wait()` issues a blocking `WaitLiveRun` unary RPC, so a long agent run (soak test, full acceptance suite) fails with `Bridge request timed out: ReadTimeout`. The installed client uses `resolve_bridge_client_timeout()` to apply a generous timeout (default **1 hour**) to both unary and stream RPCs. Override with `CYCLOPSCTL_BRIDGE_TIMEOUT_SECONDS` (a number of seconds; `0` / `none` disables the timeout entirely).

### Bridge cleanup (no orphaned processes)

The bridge spawns child node/npm/test processes (the agent's shell commands run under it), so cleanup must kill the whole **process tree**:

- `ManagedBridge.close()` is idempotent; on Windows `_terminate_process` uses `taskkill /F /T` to terminate the bridge subprocess **and its descendants** (otherwise a background `npm test` / Playwright soak orphans when the run ends). Non-Windows falls back to `terminate()` / `kill()`.
- Launched bridges are tracked in a module registry; an `atexit` handler (`_close_active_bridges_atexit`) closes any still open at interpreter exit, as a safety net for exit paths that skip the `managed_sdk_bridge` context manager's `finally`.

Tests: `tests/test_runner.py`, `tests/test_session.py`, `tests/test_sdk_bridge.py`.
