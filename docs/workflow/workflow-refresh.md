# Stale workflow detection and selective refresh

Heuristic detection and safe regeneration for brownfield workflow files that predate native `cyclopsctl tasks` CLI conventions.

## Problem

Older adopted repos may still have `ai-context.md` or `update-handover-prompt.md` missing native task-engine instructions or referencing obsolete external task CLIs. Blind overwrite is wrong; silent staleness confuses update-phase agents.

## Staleness heuristics

`workflow_gen.py` evaluates existing files (no LLM):

| File | Stale when |
|------|------------|
| `ai-context.md` | Missing `cyclopsctl tasks`, `Implementation Phase Rules`, or `Update Phase Rules` |
| `update-handover-prompt.md` | Missing `cyclopsctl tasks` or `cyclopsctl tasks set-status` |
| `docs/index.md` | Missing expected native workflow doc references |
| `current-handover-prompt.md` | Contains obsolete external task CLI references only (never auto-refreshed) |

## Surfaces

| Entry point | Behavior |
|-------------|----------|
| `cyclopsctl doctor` | `[NOTE]` **Stale workflow** check with per-file reasons; remediation → `cyclopsctl init --refresh-workflow` |
| `cyclopsctl launch` | Same check via brownfield readiness; summary line adds `Workflow: upgrade available` when stale |

## Refresh command

```bash
cyclopsctl init --refresh-workflow
cyclopsctl init --refresh-workflow --project-root /path/to/repo
```

| Behavior | Detail |
|----------|--------|
| Selective | Regenerates only files matching staleness heuristics |
| Preserves | Non-stale customized files are never touched |
| Logging | stderr logs `Workflow refresh updated:` and `Workflow refresh skipped (not stale):` |
| Templates | Uses native `workflow-*.md` templates from `src/cyclopsctl/templates/` |
| Handover | Never writes `current-handover-prompt.md` |

`--refresh-workflow` runs as part of the normal idempotent `run_project_setup` path (after default workflow generation). Dry-run plans `workflow refresh: <path>` for each stale target.

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `workflow_file_staleness_reasons` | `workflow_gen.py` | Per-file heuristic reasons |
| `detect_stale_workflow_files` | `workflow_gen.py` | Scan project workflow paths |
| `refresh_stale_workflow_files` | `workflow_gen.py` | Selective regeneration entry point |
| `workflow_upgrade_recommended` | `workflow_gen.py` | True when doctor Stale workflow check reports upgrade |
| `check_stale_workflow_files` | `doctor.py` | Brownfield diagnostic check |
| `run_project_setup` | `project_setup.py` | `--refresh-workflow` repair path and logging |
| `format_launch_summary_line` | `launcher.py` | Appends upgrade hint when stale |

Related: `workflow-generation.md`, `native-workflow-templates.md`, `brownfield-readiness.md`.

## Tests

- `tests/test_workflow_gen.py` — heuristics, selective refresh, customized preserve/overwrite
- `tests/test_doctor.py` — upgrade detail and `--refresh-workflow` remediation
- `tests/test_launcher.py` — `Workflow: upgrade available` in summary line
- `tests/test_project_setup.py` — `init --refresh-workflow` matrix
