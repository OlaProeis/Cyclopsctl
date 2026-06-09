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

**Not retried:** run errors that carry an SDK detail (e.g. billing/credits — see the Opus → Composer fallback in `loop.py`), handover verification errors, task queue errors, config errors, auth failures, or other non-transient startup errors.

Each retry logs a warning with `attempt`, `max_attempts`, `retry_delay_seconds`, and error context.

## Diagnostics for errored runs with no SDK detail

When a run errors with an empty SDK result, `runner.extract_run_error_detail` fetches the run conversation (`run.conversation_json()`) best-effort and attaches the most recent error-keyed string (or the agent's last prose) as `AgentRunError.diagnostic_detail`. This is display-only — retry and billing classification key off `result_detail` (the SDK-reported reason) — and is shown in the failure report as a `context:` line.

## Modules

| Module | Role |
|--------|------|
| `runner.py` | `is_transient_cursor_agent_error`, `is_transient_agent_failure`, `extract_run_error_detail`, backoff helpers |
| `loop.py` | `_run_cycle_with_retry` wraps single-cycle execution |
| `config.py` | `retry_on`, `retry_max_attempts`, `retry_backoff_seconds` on `CyclopsctlConfig` |
| `cli.py` | `--retry-on`, `--retry-max-attempts`, `--retry-backoff-seconds` |

## Tests

- `tests/test_runner.py` — classification and backoff unit tests
- `tests/test_retry.py` — loop integration (retry success, non-retryable paths, budget exhaustion)
- `tests/test_config.py` — config defaults and validation
