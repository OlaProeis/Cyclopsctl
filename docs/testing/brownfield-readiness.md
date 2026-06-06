# Brownfield readiness diagnostics

Warn-by-default preflight checks for in-progress repos: handover drift, stale workflow files, missing reports, and tag mismatches. All queue inspection uses `TaskBackend`.

## When checks run

| Entry point | Brownfield checks |
|-------------|-------------------|
| `cyclopsctl doctor` | When `.cyclopsctl/tasks/tasks.json` exists |
| `cyclopsctl launch` | Same as doctor, plus workspace-dirty (git) and explicit workflow paths for stale detection |

Launch also resolves **queue next** and **pending list** via `TaskBackend`.

## Check catalog

| Check | Condition | Outcome |
|-------|-----------|---------|
| Handover drift | Missing `# Task ID:`, placeholder `0`, or id ≠ `backend.get_next()` | `[NOTE]` warn |
| Handover in queue | Handover id not found via `backend.show()` | `[NOTE]` warn |
| Handover task status | Handover id is `done` while pending tasks remain | `[NOTE]` warn |
| Stale workflow | Missing native `cyclopsctl tasks` / phase-rule markers in workflow files | `[NOTE]` warn; remediation `init --refresh-workflow` |
| Active tag | Config `--tag` set and ≠ `backend.current_tag()` | `[NOTE]` warn |
| Workspace dirty | Uncommitted changes (`git status --porcelain`) | `[NOTE]` warn (launch only) |
| Complexity report | Tasks exist but report file missing | `[NOTE]` warn (native brownfield); invalid JSON still `[FAIL]` |

All brownfield checks use `passed=True` with `informational=True` for warnings — they do not change exit codes or block launch. `--strict-handover` at run time is unchanged.

## Launch summary line

`gather_launch_status()` sets `LaunchStatus.active_tag` from config or `backend.current_tag()`. The overview prints a one-line summary via `format_launch_summary_line()`:

```
Pending: 3 · Handover: task 6 · Next: task 6 · Tag: master · Workflow: upgrade available
```

When workflow files match staleness heuristics, the summary appends `Workflow: upgrade available`. See `docs/workflow/workflow-refresh.md`.

Shown at the top of Rich and plain launch overviews (`tui.py`).

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `run_brownfield_readiness_checks` | `doctor.py` | Orchestrates warn-by-default brownfield checks |
| `check_handover_drift` | `doctor.py` | Handover vs queue alignment |
| `check_stale_workflow_files` | `doctor.py` | Staleness heuristics via `workflow_gen.detect_stale_workflow_files` |
| `check_active_tag_mismatch` | `doctor.py` | Config tag vs `current_tag()` |
| `check_workspace_dirty` | `doctor.py` | Optional git porcelain |
| `compare_handover_to_backend_next` | `alignment.py` | Backend-injected handover vs `get_next()` |
| `format_launch_summary_line` | `launcher.py` | Concise preflight summary string |

`run_diagnostics()` accepts `brownfield_ai_context`, `brownfield_update_handover`, and `include_workspace_dirty` for launch-specific paths. `run_launch_diagnostics()` passes launch config paths and enables workspace-dirty checks.

Remediation hints for brownfield checks point to `cyclopsctl bootstrap --sync-handover-only`, `cyclopsctl tasks show`, and `docs/guides/testing-guide.md`.

## Tests

- `tests/test_doctor.py` — mocked backend warning paths, fixture stale-workflow and tag-mismatch
- `tests/test_launcher.py` — launch summary line and overview integration
- `tests/test_alignment.py` — `compare_handover_to_backend_next`
- `tests/test_brownfield_integration.py` — B1 fixture doctor/launch smoke

Related: `docs/cli/doctor-cli.md`, `docs/guides/testing-guide.md`, `docs/workflow/handover-alignment.md`.
