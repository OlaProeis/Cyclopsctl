# Doctor diagnostics CLI

`cyclopsctl doctor` (alias `cyclopsctl check`) runs preflight-only diagnostics before a long cyclopsctl run. It validates toolchain and configuration without starting agents or mutating the task queue.

## Commands

```bash
cyclopsctl doctor --project-root /path/to/project
cyclopsctl check --project-root /path/to/project --plain
cyclopsctl doctor --config cyclopsctl.toml --project-root /path/to/project
cyclopsctl doctor --project-root /path/to/project --fix
```

Shared flags: `--config`, `--project-root`, `--current-handover`, `--complexity-report`, `--tag`, `--plain`, `--no-env`, `--fix`.

## Checks

| Check | When | Pass criteria |
|-------|------|---------------|
| `CURSOR_API_KEY` | Always | Non-empty after optional `.env` load |
| Cursor SDK bridge | Always | Env configured, Windows bootstrap succeeds, or not required |
| `Native tasks` | Always | Readable `tasks.json`, or greenfield NOTE when no queue yet |
| Complexity report | When `tasks.json` exists | File exists; valid JSON object |
| Handover Task ID | Launch only (`run_launch_diagnostics`) | File exists; `# Task ID: <n>` parses |
| Native queue next | Launch only | `TaskBackend.get_next()` succeeds; task found or queue empty (reported as NOTE) |
| Native pending list | Launch only | Pending queue summary via `TaskBackend` |

When native `tasks.json` exists, doctor and launch add **brownfield readiness** warn-by-default checks (handover drift, stale workflow, tag mismatch, etc.) via `TaskBackend` — see `docs/testing/brownfield-readiness.md`. Launch runs extended preflight (handover, `ai-context`, `next`, pending list) and prints a one-line summary (`Pending · Handover · Next · Tag`).

An empty queue is **not** a failure — it is reported clearly as `[NOTE]` and still exits **0** when all other checks pass.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed |
| 1 | Startup/tooling failure (API key, bridge) |
| 2 | Configuration/content failure (handover, report, next-task error) |

Configuration errors (`ConfigError`, `EnvLoadError`) use the same CLI error path as `cyclopsctl run`.

## Output

- **TTY stderr:** Rich summary table via `render_diagnostics_rich`
- **`--plain` or non-TTY:** Plain-text lines with `[PASS]`, `[FAIL]`, or `[NOTE]`

Failed checks and empty-queue NOTE rows include a **Remediation** line with copy-pasteable next steps. Plain and Rich modes use the same `format_check_detail()` text so hints stay aligned.

Remediation hints are **context-aware**: mature projects with an existing queue get `cyclopsctl bootstrap --sync-handover-only`, `cyclopsctl tasks show`, or `cyclopsctl bootstrap --append`; greenfield projects get `cyclopsctl init` / `cyclopsctl bootstrap` guidance.

## Safe auto-fix (`--fix`)

`--fix` applies limited filesystem-only repairs, then re-runs diagnostics:

| Condition | Action |
|-----------|--------|
| `CURSOR_API_KEY` missing and no `.env` file | Create stub `.env` with `CURSOR_API_KEY=` placeholder and Cursor settings comment only |

`--fix` never invokes parse-prd, bootstrap pipelines, or agent runs. It does not overwrite an existing `.env`.

## Configuration

`DoctorConfig` in `config.py` requires `--project-root` only. Handover and complexity report paths default under the project root (`current-handover-prompt.md`, `.cyclopsctl/reports/complexity-report.json`). Values from `cyclopsctl.toml` merge with CLI flags (CLI wins).

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `remediation_for_check` | `doctor.py` | Context-aware hint text per check/failure mode |
| `enrich_checks_with_remediation` | `doctor.py` | Attaches remediation to diagnostic results |
| `apply_safe_fixes` | `doctor.py` | `--fix` filesystem actions (stub `.env` only) |
| `check_native_tasks_directory` | `doctor.py` | Native tasks readability / greenfield NOTE |
| `run_diagnostics` | `doctor.py` | Runs doctor checks; returns enriched `DiagnosticCheck` list |
| `run_launch_diagnostics` | `doctor.py` | Doctor checks plus launch-only file and queue summary |
| `run_doctor` | `doctor.py` | Optional fix pass, prints results, returns exit code |
| `load_doctor_config` | `config.py` | Path merge and validation for doctor (`fix` flag) |
| `_doctor_command` | `cli.py` | Loads `.env`, then calls `run_doctor` |

Tests: `tests/test_doctor.py`.

Related: `docs/setup/env-loading.md`, `docs/workflow/prompt-handover.md`, `docs/tasks/tasks-cli.md`.
