# Agent run failure diagnostics

When an implementation or update agent run ends with `AgentRunError`, the cyclopsctl prints an actionable report to stderr and records enough context to investigate after the Rich TUI tears down.

Previously a failed run printed only the agent id and run id; the SDK `result` is frequently empty, so users had nothing to act on.

## Failure report on exit

`cli.py` routes every `AgentRunError` through `_log_and_exit_agent_run`, which logs structured error context (`kind`, `phase`, `agent_id`, `run_id`, `result_detail`) and prints a report built by `failure_report.format_failure_report`:

```
cyclopsctl: error: implementation wait failed for run run-...: ...
Agent run failed during implementation (kind=startup, exit 1)
  agent:      agent-...
  run:        run-...
  detail:     (SDK returned no detail)
  transcript: C:\Users\me\.cursor\projects\g-GAMEDESIGN-test2\agent-transcripts\agent-...\agent-....jsonl
```

- **detail** — `AgentRunError.result_detail` when present, else an explicit `(SDK returned no detail)`.
- **transcript** — the resolved local Cursor JSONL path when it exists on disk, else `(local transcript not found)`. The cyclopsctl does **not** call `Agent.get_run` (unreliable after failure); it points at the local transcript instead.
- **last activity** — when the transcript exists, `summarize_transcript_tail` parses the last assistant tool invocations (and any non-redacted prose) so a `kind=run` failure with an empty SDK `result` still shows what the agent was doing (the SDK export records tool *invocations*, not results, so this is a best-effort action trail). Output is ASCII so the report can never fail to print on legacy Windows consoles.

## Transcript path resolution

`failure_report.resolve_transcript_path(project_root, agent_id)` maps the project root to Cursor's on-disk layout:

```
<home>/.cursor/projects/<slug>/agent-transcripts/<agent_id>/<agent_id>.jsonl
```

`encode_project_slug` derives `<slug>` from the absolute path: drop the drive `:`, replace `\` / `/` with `-`, lowercase the drive letter, preserve the rest. Examples:

| Project root | Slug |
|--------------|------|
| `g:\DEV\CursorOrchestrator` | `g-DEV-CursorOrchestrator` |
| `G:\GAMEDESIGN\test2` | `g-GAMEDESIGN-test2` |

Resolution also scans the projects directory case-insensitively so drive-letter case differences (Windows normalizes the drive) still match.

## State persistence on failure

`loop.py` (`_persist_phase_failure`) records `status=failed` with the failing phase and the exception's `agent_id` / `run_id` before re-raising, so `cyclopsctl status` is useful after a failed run. See `docs/runtime/crash-recovery-state.md`.

## Modules

| Module | Role |
|--------|------|
| `failure_report.py` | `encode_project_slug`, `resolve_transcript_path`, `summarize_transcript_tail`, `format_failure_report` |
| `cli.py` | `_log_and_exit_agent_run` — structured log + stderr report on `AgentRunError` |
| `runner.py` | `AgentRunError.phase` (`startup` / `implementation` / `update`) |
| `loop.py` | `_persist_phase_failure` — `failed` state with agent/run ids |

## Tests

- `tests/test_failure_report.py` — slug mapping, transcript resolution (found / case-insensitive / missing / no agent id), report formatting, `failed` state round-trip and summary note, the CLI handler stderr output, and `_persist_phase_failure`.
- `tests/test_loop.py` — `failed` status on agent failure.
