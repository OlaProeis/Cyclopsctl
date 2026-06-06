# Project setup assessment

`project_setup.py` centralizes idempotent first-time and repair flows for repository readiness. `cyclopsctl init` delegates to `run_project_setup()` for the full assess-and-setup pipeline (see `docs/setup/project-scaffold.md`).

## Module

`src/cyclopsctl/project_setup.py` — `run_project_setup()`, `resolve_project_setup_config()`, `format_ready_message()`.

## Prerequisites (fail fast)

| Check | On failure |
|-------|------------|
| `CURSOR_API_KEY` in env or `.env` | `ProjectSetupError`; optional `fix_env=True` writes stub `.env` |
| `prd.md` in project root | `ProjectSetupError` when **no tasks** exist; brownfield **attach mode** when tasks exist (see `docs/setup/brownfield-attach-init.md`) |

Loads project `.env` via `env.load_project_env()` before the API key check.

## Init modes: greenfield vs attach vs continue

`run_project_setup()` picks one path from repository signals. See `docs/guides/testing-guide.md` scenarios B4–B6 for manual validation.

| Mode | When detected | PRD required | Parse PRD | Key flags | Result flag |
|------|---------------|--------------|-----------|-----------|-------------|
| **Greenfield** | No tasks for active tag; PRD present; no existing-repo heuristic (`src/`, `README.md`, `.git`) | Yes | Yes (SDK) | `init` (default) | — |
| **Attach** | Tasks exist; expected PRD path missing | No | **Skipped** | `init --attach --yes` | `attach_mode` |
| **Continue** | No tasks; PRD present; `repo_has_existing_content()` true | Yes | Yes (SDK, confirm) | `init --yes`, optional `--from-prd` | `continue_mode` |
| **Fail fast** | No tasks and no PRD | — | — | Remediation: `--from-prd` or `--attach --yes` if queue may exist elsewhere | — |

**Attach** uses README/repo context for workflow generation (`prd_path=None`). **Continue** and **greenfield** share the parse pipeline; continue adds TTY cost warning. All modes preserve customized `ai-context.md`, `update-handover-prompt.md`, and `docs/index.md` unless `--force` or `--refresh-workflow`.

## Repair sequence (non-destructive)

1. **Scaffold** — missing `cyclopsctl.toml`, `.gitignore` entries, `current-handover-prompt.md` template (reuses `init_scaffold` helpers).
2. **Native tasks** — `NativeTaskBackend.init_project()` scaffolds `.cyclopsctl/tasks/` and installs cyclopsctl Cursor rules.
3. **Workflow files** — PRD-aware generation via `workflow_gen.generate_workflow_files()` (skips customized targets).
4. **Parse / analyze** — native parse-prd only when no tasks exist for the tag; analyze-complexity when tasks exist but the report is missing.
5. **Handover sync** — when handover is missing, `# Task ID: 0`, or stale vs `cyclopsctl tasks next`.
6. **Parse state** — writes `.cyclopsctl/last-parsed-prd.json` only after a successful first parse.

## Changed PRD guard

When tasks already exist and `prd.md` SHA-256 differs from `.cyclopsctl/last-parsed-prd.json`, setup refuses re-parse and directs the user to `cyclopsctl launch` (new-tag flow).

## Parse state file

`.cyclopsctl/last-parsed-prd.json`:

```json
{
  "path": "prd.md",
  "sha256": "<hex>",
  "tag": "master",
  "parsed_at": "<ISO-8601 UTC>"
}
```

## Result and messaging

`ProjectSetupResult` includes `already_ready`, `repairs`, `parsed_prd`, pending count, and next task metadata. `format_ready_message()` prints:

```text
Ready. Run `cyclopsctl launch` when you want to start building.
  Tag: master
  Pending tasks: N
  Next task: <id> — <title>
```

Re-runs on a healthy project set `already_ready=True` with an empty `repairs` tuple.

## Tests

`tests/test_project_setup.py` — matrix coverage for fresh, partial, healthy, missing PRD/env, changed PRD with existing tasks, and last-parsed state persistence.

`tests/test_init_integration.py` — CLI integration for `cyclopsctl init` delegating to this engine.
