# Transient retry on SDK failures

Optional retry for short-lived Cursor SDK send/wait failures during `cyclopsctl run`. Disabled by default for backward compatibility.

## Configuration

| Setting | Default | CLI |
|---------|---------|-----|
| `retry_on` | `off` | `--retry-on transient` |
| `retry_max_attempts` | `3` | `--retry-max-attempts N` |
| `retry_backoff_seconds` | `5, 15` | `--retry-backoff-seconds 5,15` |

TOML example (`cyclopsctl.toml.example`):

```toml
retry_on = "transient"
retry_max_attempts = 3
retry_backoff_seconds = [5, 15]
```

## Behavior

When `retry_on = "transient"`, each cycle retries up to `retry_max_attempts` times on **transient** `AgentRunError` with `RunFailureKind.STARTUP` (SDK create/send/wait `CursorAgentError`). The full implement → update → verify cycle is retried; the selected task is resolved once per cycle.

**Retried:** `CursorAgentError` where `is_retryable` is true, or the message/status suggests connectivity or startup instability (network, timeout, rpc, 502/503/504, etc.).

**Not retried:** agent logical failures (`result.status == "error"`), handover verification errors, task queue errors, config errors, auth failures, or other non-transient startup errors.

Each retry logs a warning with `attempt`, `max_attempts`, `retry_delay_seconds`, and error context.

## Modules

| Module | Role |
|--------|------|
| `runner.py` | `is_transient_cursor_agent_error`, `is_transient_agent_failure`, backoff helpers |
| `loop.py` | `_run_cycle_with_retry` wraps single-cycle execution |
| `config.py` | `retry_on`, `retry_max_attempts`, `retry_backoff_seconds` on `CyclopsctlConfig` |
| `cli.py` | `--retry-on`, `--retry-max-attempts`, `--retry-backoff-seconds` |

## Tests

- `tests/test_runner.py` — classification and backoff unit tests
- `tests/test_retry.py` — loop integration (retry success, non-retryable paths, budget exhaustion)
- `tests/test_config.py` — config defaults and validation
