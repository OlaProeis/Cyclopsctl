# Analyze-complexity CLI

`cyclopsctl analyze-complexity` scores **pending** parent tasks on the active tag via the Cursor SDK. It writes `.cyclopsctl/reports/complexity-report.json` and backfills `complexity` on task records — **without** parsing a PRD or running `init` / `bootstrap`.

Use it when:

- A new phase tag was parsed but tasks have no complexity scores (e.g. after an older cyclopsctl skipped analyze because a prior-phase report still existed).
- `cyclopsctl init` refuses with *PRD changed — run `cyclopsctl launch`* and you only need scores, not a re-parse.
- You want to refresh routing scores after editing task titles/descriptions manually.

## Command

```bash
cyclopsctl analyze-complexity
cyclopsctl analyze-complexity --project-root /path/to/project
cyclopsctl analyze-complexity --tag phase-2
cyclopsctl analyze-complexity --analyze-model auto
cyclopsctl analyze-complexity --skip-if-exists
```

Shared flags: `--no-env`, `--project-root`, `--tag`, `--plain` (from path_flags).

| Flag | Effect |
|------|--------|
| `--tag NAME` | Tag to analyze (default: active tag from `.cyclopsctl/tasks/state.json`) |
| `--analyze-model MODEL` | Override model (default: `[tasks].analyze_model` or `auto`) |
| `--skip-if-exists` | Skip when `complexity-report.json` already exists |

**Default behavior:** always runs analysis (replaces an existing report). Pipeline helpers (`bootstrap`, `init` repair) still use `skip_if_exists=True` unless overridden. Launch PRD-change passes `skip_if_exists=False` so new phases always get fresh scores.

## Typical recovery flow

After `cyclopsctl launch --prd prd-phase2.md` when tasks lack complexity:

```bash
cyclopsctl tasks tags
cyclopsctl tasks use-tag phase-2    # if needed
cyclopsctl analyze-complexity
cyclopsctl tasks list
cyclopsctl doctor
```

**Expect:** stderr progress from `cyclopsctl analyze-complexity: analyzing N tasks…`, then `cyclopsctl: complexity analysis complete`. `tasks list` shows complexity values; doctor reports a readable complexity report.

## Requirements

- Native task storage (`.cyclopsctl/tasks/tasks.json`)
- `CURSOR_API_KEY` in project `.env` or environment
- On **Windows**, `managed_sdk_bridge` starts the local Cursor SDK bridge before the first SDK call (same as `run`, `init`, `bootstrap`, `launch`, and `models`)

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `run_analyze_complexity` | `cli.py` | Orchestration, storage check, exit codes |
| `_analyze_complexity_command` | `cli.py` | Env load + `managed_sdk_bridge` wrapper |
| `analyze_complexity_with_cursor` | `tasks/analyze.py` | Cursor agent, report write, backfill |

Tests: `tests/test_cli_analyze_complexity.py`, `tests/test_analyze.py`.

Related: [analyze-complexity.md](../tasks/analyze-complexity.md), [launch-prd-change.md](launch-prd-change.md), [model-routing.md](../runtime/model-routing.md).
