# Session Handover — Ad-hoc (not from task queue)

# Task ID: 0

## Environment
- **Project:** Cyclopsctl (`CursorOrchestrator`)
- **Project root:** `G:\DEV\CursorOrchestrator`
- **Tech stack:** Python 3.10+, `cursor-sdk`, Rich TUI, `cyclopsctl` CLI
- **Scope:** **Cyclopsctl only.** Do not modify target-project code (e.g. `G:\GAMEDESIGN\test2`). This is an observability / failure-diagnostics improvement initiative.
- **Branch:** `master`

## Mission

Design and implement better **failure diagnostics** when a cyclopsctl implementation (or update) agent run ends with `AgentRunError` (`kind='run'`, exit code 2). Today users see a single opaque line and lose context when the Rich TUI tears down or the terminal is flaky.

This handover captures a **real production incident** and forensic findings. Use it to propose architecture, then implement the highest-value fixes with tests.

---

## Incident summary (2026-06-09)

User ran:

```powershell
cd G:\GAMEDESIGN\test2
cyclopsctl launch   # 1 cycle, task 20
```

Cyclopsctl exited with:

```
Agent run failed error='implementation run failed: agent=agent-db077b6c-8edc-4a11-b0e6-3963409d387a run=run-a9ee18a0-beb0-42da-b8ec-ca5a0ecea639' error_type='AgentRunError' exit_code=2 kind='run' ...
cyclopsctl: error: implementation run failed: agent=agent-db077b6c-8edc-4a11-b0e6-3963409d387a run=run-a9ee18a0-beb0-42da-b8ec-ca5a0ecea639
```

**What the user expected:** Actionable detail — what the agent was doing, why it failed, where to find the full session.

**What they got:** Agent ID + run ID only. SDK `result` was empty, so cyclopsctl appended no detail.

---

## Forensic findings (verified by reading artifacts)

### 1. Cyclopsctl error semantics (code)

- `src/cyclopsctl/runner.py` — `send_and_wait()` raises `AgentRunError` with `RunFailureKind.RUN` and `RUN_FAILURE_EXIT_CODE = 2` when `result.status == "error"`.
- If `result.result` is non-empty, it is appended to the exception message and stored in `exc.result_detail`. **In the incident, `result` was empty.**
- `src/cyclopsctl/cli.py` — `_log_and_exit()` prints `cyclopsctl: error: {exc}` to stderr and calls `log_error()` with `kind`, `agent_id`, `run_id` — but **not** `result_detail` as a separate field, and nothing about transcript location.

### 2. Crash-recovery state was useless after failure

`G:\GAMEDESIGN\test2\.cyclopsctl\state.json` after the failed run:

```json
{
  "agent_id": null,
  "run_id": null,
  "status": "running",
  "phase": "implementation",
  "last_event": "cycle started",
  "task_id": 20,
  "cycle_number": 1,
  "total_cycles": 1
}
```

**Gap:** `state_tracker.persist()` with `agent_id` / `run_id` happens only **after successful implementation** (`loop.py` ~line 539). On `AgentRunError` during implementation, state stays at `"cycle started"` with null IDs.

### 3. Rich TUI showed `ERROR` then vanished

Terminal capture: `C:\Users\lbh\.cursor\projects\g-GAMEDESIGN-test2\terminals\1.txt`

Last activity lines before teardown:

```
shell · cd "G:\GAMEDESIGN\test2" && npm test 2>&1
shell · cd "G:\GAMEDESIGN\test2" && npm test 2>&1
shell · cd "G:\GAMEDESIGN\test2"; npm test 2>&1
ERROR
```

The `ERROR` line comes from the SDK activity stream (`runner.py` `format_activity_from_event` / status events). The TUI does not persist activity buffer on failure.

### 4. Full agent transcript exists on disk — cyclopsctl never points to it

**Path pattern (local agents):**

```
%USERPROFILE%\.cursor\projects\<encoded-project-path>\agent-transcripts\<agent-id>\<agent-id>.jsonl
```

**This incident:**

```
C:\Users\lbh\.cursor\projects\g-GAMEDESIGN-test2\agent-transcripts\agent-db077b6c-8edc-4a11-b0e6-3963409d387a\agent-db077b6c-8edc-4a11-b0e6-3963409d387a.jsonl
```

Transcript contents (5 JSONL lines, ~17 KB):

| Line | Content |
|------|---------|
| 1 | User prompt (cyclopsctl prepended ai-context + task 20 handover) |
| 2 | Agent explored project (Read, Glob, Grep) |
| 3 | Agent read phase2 tests, PRD, package.json, prior Playwright error-context |
| 4 | Agent read playwright.config, acceptance.spec, searched terminals folder |
| 5 | Agent invoked Shell: `npm test 2>&1` with `block_until_ms: 900000` — **no further lines** |

**Important:** JSONL records tool *invocations* but not tool *results* in this export. No shell stdout/stderr in transcript. Session ended while/long before shell completed.

### 5. SDK resume / get_run is unreliable after failure

Using `cyclopsctl.sdk_bridge.managed_sdk_bridge` + `Agent.resume` / `Agent.list`:

- Target agent `agent-db077b6c-...` → **`Agent not found`** (pruned from bridge list; 50-agent cap).
- `Agent.get_run('run-a9ee18a0-...')` → **Run not found**.
- A different recent `error` agent (`agent-630000bc-...`) resumes but: `result: ''`, `list_messages(): 0`.

**Conclusion:** Post-mortem must not depend solely on SDK message APIs. Local JSONL + cyclopsctl-captured context are the reliable sources.

### 6. Existing observability features don't help on failure

From `docs/runtime/run-observability.md`:

- `--export-transcript-dir` writes sidecars only on **verified successful** cycles (`loop.py` ~691). Failed cycles write nothing.
- JSONL cycle logging (`CycleLogger(jsonl_path=...)`) exists but has **no CLI flag** wired in `cli.py` / `launcher.py` for normal runs.
- `log_error()` writes to Python logger `cyclopsctl.cycle` — no `basicConfig`; users don't see structured error context unless logging is configured externally.

---

## Known code touchpoints

| Area | File | Notes |
|------|------|-------|
| Agent error raise | `src/cyclopsctl/runner.py` | `AgentRunError`, `send_and_wait`, activity stream |
| Run loop / state | `src/cyclopsctl/loop.py` | `_run_implementation_phase`, `state_tracker.persist`, transcript sidecar on success only |
| CLI exit | `src/cyclopsctl/cli.py` | `_log_and_exit`, `AgentRunError` handler ~725 |
| Error mapping | `src/cyclopsctl/errors.py` | `exit_code_for` |
| State file | `src/cyclopsctl/state.py` | `RunStateTracker`, `format_state_summary` |
| Transcript sidecar | `src/cyclopsctl/transcript_export.py` | Minimal JSON (ids only, no path to Cursor JSONL) |
| TUI activity | `src/cyclopsctl/tui.py` | `RichCycleLogger`, activity buffer (last 8 lines), teardown on error |
| Windows bridge | `src/cyclopsctl/sdk_bridge.py` | Required for SDK on Windows |
| Docs | `docs/runtime/run-observability.md`, `structured-logging.md`, `crash-recovery-state.md`, `agent-session.md` | Update in update phase if behavior changes |

---

## Proposed improvement areas (prioritize — do not boil the ocean)

Think through trade-offs, then implement in small PR-sized slices.

### A. Failure exit message (high value, low risk)

On `AgentRunError` in `cli.py` (or a dedicated `failure_report.py`):

- Print `result_detail` when present (even if empty, say so explicitly).
- Print `agent_id`, `run_id`, `kind`, `exit_code`, `phase` if available.
- **Resolve and print local transcript path** when it exists:
  - Derive encoded project slug from `project_root` (investigate how Cursor maps `G:\GAMEDESIGN\test2` → `g-GAMEDESIGN-test2`).
  - Check `%USERPROFILE%\.cursor\projects\<slug>\agent-transcripts\<agent_id>\<agent_id>.jsonl`.
- Optionally print last N lines of terminal capture if present under `.cursor/projects/<slug>/terminals/*.txt` (newest matching cwd).

### B. Persist IDs on failure (high value)

In `loop.py`, when `_run_implementation_phase` catches or propagates `AgentRunError`:

- Call `state_tracker.persist(..., agent_id=exc.agent_id, run_id=exc.run_id, last_event="implementation failed", status="failed")` (may need new status value — check `state.py` consumers).
- Ensure `cyclopsctl status` shows failed runs usefully.

### C. Failure transcript sidecar (medium value)

Extend `transcript_export.py` or add `failure_export.py`:

- On `AgentRunError`, write `.cyclopsctl/failures/cycle-<n>-<timestamp>.json` under **project_root** with: cycle, task_id, agent_id, run_id, result_detail, cursor_jsonl_path, last_activity_lines (if TUI can expose buffer), impl_status=error.
- Consider writing sidecar on failure even when `--export-transcript-dir` is unset (always write minimal failure record to `.cyclopsctl/`).

### D. Capture activity buffer on failure (medium value)

`RichCycleLogger` / `RunDashboardState` holds last 8 activity lines. On `AgentRunError`, flush buffer to failure sidecar or stderr before Live teardown.

### E. `cyclopsctl inspect` subcommand (optional, larger)

```
cyclopsctl inspect agent <agent_id> [--project-root PATH] [--tail N]
cyclopsctl inspect run <run_id> [--agent-id ...]
```

Uses `managed_sdk_bridge` when needed; falls back to local JSONL path; prints run status + transcript tail.

### F. Wire JSONL cycle logging to CLI (optional)

`--cycle-log PATH` or `[run] cycle_log = ".cyclopsctl/cycles.jsonl"` so `log_error` / cycle events are durable without Rich.

---

## Non-goals

- Fixing why the **target project's** agent or tests failed (out of scope).
- Depending on SDK `list_messages()` for post-mortem (returns empty in incident).
- Requiring cloud agent APIs.

---

## Test strategy

- Unit tests with mocked `AgentRunError` (agent_id, run_id, result_detail present/absent).
- Test transcript path resolution for Windows paths (`G:\...` → slug).
- Test `state.json` persists IDs on implementation failure.
- Test failure sidecar write (tmp_path).
- Test `_log_and_exit` / stderr output includes transcript path when file exists (caplog or redirect stderr).
- Run `python -m pytest` — all tests must pass.

---

## Verification

```bash
cd G:\DEV\CursorOrchestrator
python -m pytest
```

Manual smoke (optional): simulate or document how to verify stderr output on a failed run without needing a real long agent session.

---

## Questions for Opus to resolve in design

1. **Encoded path algorithm:** What is the canonical mapping from `project_root` to `.cursor/projects/<slug>`? Is it documented by Cursor or empirically derived? Handle drive letters, case, forward vs back slashes.
2. **State `status` values:** Should we add `"failed"` vs leaving `"running"` stale? How does `cyclopsctl status` message change?
3. **Update-phase failures:** Same treatment for update `AgentRunError`?
4. **Security:** Should failure sidecars redact API keys or absolute paths?
5. **Minimal vs comprehensive:** Which of A–F ships first for maximum user value per line of code?
6. **Empty SDK result:** When `result` is empty, should cyclopsctl attempt `Agent.get_run` best-effort before exit (knowing it often fails)?

---

## Implementation phase rules

- Work in `G:\DEV\CursorOrchestrator` only.
- Run `python -m pytest` before finishing.
- Follow existing module patterns; prefer focused new helpers over sprawling `cli.py` changes.
- Do not edit `ai-context.md`, `current-handover-prompt.md`, or `docs/index.md` during implementation (update phase only if docs change).

## Update phase (after implementation)

- Update relevant docs under `docs/runtime/` if CLI or state behavior changes.
- Update `docs/index.md` with one-line entries for any new docs.
- Add concise bullets to `ai-context.md` project memory if new modules or flags exist.

---

## Reference: exception the user saw

```
error_type='AgentRunError'
exit_code=2
kind='run'
agent_id='agent-db077b6c-8edc-4a11-b0e6-3963409d387a'
run_id='run-a9ee18a0-beb0-42da-b8ec-ca5a0ecea639'
message='implementation run failed: agent=... run=...'  # no trailing detail
```
