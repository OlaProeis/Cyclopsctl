# PRD continue init

`cyclopsctl init` can bootstrap a task queue from a PRD on **existing repositories** that have code but no task JSON yet — without treating them as empty greenfield projects.

## When continue mode applies

Continue mode is **auto-detected** when:

1. The expected PRD path exists (`prd.md` in project root, or `init --from-prd`),
2. `TaskBackend.tasks_exist()` is false for the active tag, and
3. `repo_has_existing_content()` is true — any of `src/`, `README.md`, or `.git` is present.

**Greenfield unchanged:** PRD present but no existing-content heuristic → same parse pipeline as today, no confirmation prompt.

**Attach unchanged:** tasks exist without PRD → attach mode (see `docs/setup/brownfield-attach-init.md`).

**No PRD, no tasks:** fails fast; existing repos get remediation pointing to `--from-prd` or `init --attach --yes` when a task queue may exist elsewhere.

## Confirmation UX

| Flag | Behavior |
|------|----------|
| (default, TTY) | Warn about Cursor SDK time/cost; prompt `Parse PRD into tasks? [y/N]` |
| `--yes` / `-y` | Non-interactive yes (CI/non-TTY) |

Non-TTY without `--yes` raises `ProjectSetupError` with `PRD_CONTINUE_CONFIRM_MESSAGE` remediation.

Stderr logs `PRD continue mode: parsing PRD into tasks for existing repository` after confirmation.

## Repair sequence (continue only)

Same parse pipeline as greenfield after confirmation:

1. **Scaffold** — `cyclopsctl.toml`, `.gitignore`, native task storage init when missing.
2. **Workflow files** — `generate_workflow_files()` non-destructive (customized `ai-context.md`, `update-handover-prompt.md`, `docs/index.md` preserved unless `--force` / `--refresh-workflow`).
3. **Parse + analyze** — `backend.parse_prd()` then `analyze_complexity()` (mocked in unit tests).
4. **last-parsed-prd** — `write_last_parsed_prd()` on successful parse.
5. **Handover sync** — `sync_current_handover()` from lowest pending parent via `TaskBackend.get_next()`.

**Guard:** existing tasks for the tag → `prd_changed_with_existing_tasks()` refuses re-parse (launch handles PRD change via new tag).

## Module map

| Piece | Location |
|-------|----------|
| Existing-repo heuristic | `project_setup.repo_has_existing_content()` |
| Continue detection and execution | `project_setup.run_project_setup()`, `_confirm_prd_continue()` |
| Missing-PRD remediation | `project_setup._format_missing_prd_error()` |
| CLI flags | `cli.py` — `init --from-prd`, `init --yes` |
| Result flag | `ProjectSetupResult.continue_mode` |

## Tests

Matrix coverage in `tests/test_project_setup.py`:

- (A) PRD + `src/` + empty queue, confirm yes → parse + handover; customized `ai-context.md` preserved
- (B) non-TTY without `--yes` → fail with confirm remediation
- (C) existing tasks + PRD → no re-parse (unchanged guard)
- (D) no PRD + `src/` → fail with attach remediation
- (E) `init --from-prd` custom path with `--yes`

Related: `docs/setup/project-setup.md`, `docs/setup/brownfield-attach-init.md`, `docs/testing/brownfield-regression.md` (B6 scenario).
