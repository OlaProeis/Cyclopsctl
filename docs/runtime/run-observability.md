# Run observability (dry-run, git summary, transcript export)

Optional `cyclopsctl run` features for brownfield safety and post-run review without new external services.

## CLI flags and config

| Setting | CLI | TOML (`[run]` section) | Default |
|---------|-----|------------------------|---------|
| Dry-run plan | `--dry-run` | `dry_run = true` | off |
| Git diff summary | `--git-summary` | `git_summary = true` | off |
| Transcript sidecars | `--export-transcript-dir PATH` | `export_transcript_dir = "..."` | none |

CLI flags override TOML. Relative paths for `export_transcript_dir` resolve under `project_root`.

Example:

```toml
[run]
git_summary = true
export_transcript_dir = ".cyclopsctl/transcripts"
```

## Dry-run

`cyclopsctl run --dry-run` executes the normal task-resolution path (startup, resume skip, model routing, handover alignment, selection warnings) and logs a cycle plan **without** calling `Agent.create` / `send`.

| Behavior | Detail |
|----------|--------|
| SDK bridge | Skipped (`managed_sdk_bridge` not entered) |
| Preflight | Still runs (API key, backend checks) |
| Alignment | Same warn/strict rules as live run |
| History | Does not persist completed tasks to run history |
| Outcome | `CycleOutcome.verification_result = "dry-run"`; empty agent/run ids |
| JSONL | `dry_run_plan` event, then `cycle_complete` with `impl_status=update_status=dry-run` |

Exit 0 when the plan is valid; strict handover misalignment still raises before any agent work (same as live run).

## Git diff summary

When `--git-summary` is enabled (or `[run] git_summary = true`):

1. At cycle start, capture `git rev-parse HEAD` via `git_summary.capture_git_head()`.
2. After verify (or at dry-run completion), run `git diff --stat <start_head>` via `capture_cycle_git_diff_summary()`.

| Fallback | Behavior |
|----------|----------|
| Not a git repo / git missing | Summary omitted; run continues |
| No changes | Summary omitted |

Summary text is stored on `CycleOutcome.git_diff_summary`, `CycleLogRecord.git_diff_summary`, and included in post-run output when present.

## Transcript sidecar export

When `--export-transcript-dir` is set, each **verified live cycle** writes:

```
<dir>/cycle-<n>.json
```

Payload:

```json
{
  "agent_id": "...",
  "cycle": 1,
  "model": "composer-2.5",
  "run_id": "...",
  "status": "passed",
  "task_id": 8
}
```

`run_id` is the implementation-phase run id. Dry-run cycles do not write sidecars.

## Post-run summary extensions

When outcomes include agent ids or git summaries, `print_run_summary` adds columns dynamically:

| Column | When shown |
|--------|------------|
| Agent ID | Any outcome has non-empty `agent_id` |
| Run ID | Same (uses `impl_run_id`) |
| Git changes | Any outcome has `git_diff_summary` |

Dry-run outcomes omit agent/run columns (empty ids). Verification shows `dry-run` styling in Rich mode.

## Key modules

| Module | Role |
|--------|------|
| `loop.py` | Dry-run branch after `log_cycle_start`; git capture; sidecar hook; `RunLoopResult.dry_run` |
| `cli.py` | Flags, `[run]` config wiring, skip SDK bridge on dry-run |
| `config.py` | `CyclopsctlConfig.dry_run`, `git_summary`, `export_transcript_dir`; `_merge_run_section()` |
| `git_summary.py` | Injectable git subprocess helpers |
| `transcript_export.py` | Atomic JSON sidecar writer |
| `logging.py` | `log_dry_run_plan()`, `CycleLogRecord.git_diff_summary` |
| `tui.py` | Dynamic summary columns, `dry-run` verification style |

## Tests

- `tests/test_run_observability.py` — dry-run (no agent), git mocks, transcript sidecars, non-git fallback, TOML `[run]` parsing
- `tests/test_run_summary.py` — summary column rendering (extended outcomes)
