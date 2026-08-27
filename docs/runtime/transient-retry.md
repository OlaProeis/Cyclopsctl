# Transient retry on agent failures

Per-cycle retry for short-lived agent failures during `cyclopsctl run`. **Enabled by default** (`retry_on = "transient"`); set `retry_on = "off"` / `--retry-on off` to stop on the first failure.

## Configuration

| Setting | Default | CLI |
|---------|---------|-----|
| `retry_on` | `transient` | `--retry-on transient` / `--retry-on off` |
| `retry_max_attempts` | `3` | `--retry-max-attempts N` |
| `retry_backoff_seconds` | `5, 15` | `--retry-backoff-seconds 5,15` |

TOML example (`cyclopsctl.toml.example`):

```toml
retry_on = "transient"
retry_max_attempts = 3
retry_backoff_seconds = [5, 15]
```

## Behavior

When `retry_on = "transient"`, each cycle retries up to `retry_max_attempts` times on a transient `AgentRunError`. The full implement → update → verify cycle is retried with a **fresh agent**; the selected task is resolved once per cycle.

**Retried:**

- `RunFailureKind.STARTUP` — SDK create/send/wait `CursorAgentError` where `is_retryable` is true, or the message/status suggests connectivity or startup instability (network, timeout, rpc, 502/503/504, etc.).
- `RunFailureKind.RUN` with **empty SDK detail** — the run completed with `status == "error"` but the SDK returned no result text. In practice this is an upstream infrastructure blip (model/server/connection inside the agent host), not an agent logical failure; retrying the cycle with a fresh agent is the same recovery as a manual relaunch.

**Same-agent continue first:** `CycleSession` sends one continue nudge on the live agent before the loop considers a fresh-agent retry (see `docs/runtime/agent-session.md`). That recovers the common case where `wait()` returned `error` with no detail but the handle is still usable.

**Not retried:** run errors that carry an SDK detail (e.g. billing/credits — see the Opus → Composer fallback in `loop.py`), handover verification errors, task queue errors, config errors, auth failures, other non-transient startup errors, or empty-detail run errors **after** a same-agent continue when the diagnostic shows the agent was already mid-work (`last agent output:`, `cargo test`, `full test suite`, `npm test`, Playwright/soak). Fresh-agent retry in that case re-runs the whole implementation (often another full suite) and frequently re-triggers OOM or host drops. Work is already on disk; relaunch the queue.

Each retry logs a warning with `attempt`, `max_attempts`, `retry_delay_seconds`, and error context.

## Diagnostics for errored runs with no SDK detail

When a run errors with an empty SDK result, `runner.extract_run_error_detail` fetches the run conversation (`run.conversation_json()`) best-effort and attaches the most recent error-keyed string (or the agent's last prose) as `AgentRunError.diagnostic_detail`. Billing fallback still keys off `result_detail` only. After a same-agent continue, `looks_like_productive_run_drop` uses the diagnostic (and exception text) to skip a fresh-agent retry. The failure report shows the diagnostic as a `context:` line and notes when continue already ran.

## Modules

| Module | Role |
|--------|------|
| `runner.py` | `is_transient_cursor_agent_error`, `is_transient_agent_failure`, `should_continue_same_agent`, `looks_like_productive_run_drop`, `extract_run_error_detail`, backoff helpers |
| `session.py` | Same-agent continue after empty-detail run errors |
| `loop.py` | `_run_cycle_with_retry` wraps single-cycle execution |
| `config.py` | `retry_on`, `retry_max_attempts`, `retry_backoff_seconds` on `CyclopsctlConfig` |
| `cli.py` | `--retry-on`, `--retry-max-attempts`, `--retry-backoff-seconds` |

## Tests

- `tests/test_runner.py` — classification and backoff unit tests (including productive-drop skip)
- `tests/test_session.py` — same-agent continue after empty-detail run errors
- `tests/test_retry.py` — loop integration (retry success, same-agent continue, mid-work drop does not fresh-retry, non-retryable paths, budget exhaustion)
- `tests/test_config.py` — config defaults and validation
