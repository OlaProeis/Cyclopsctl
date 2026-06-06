# Brownfield attach init

`cyclopsctl init` can attach to in-progress repositories that already have a native task queue but no `prd.md`, without re-parsing or overwriting customized workflow files.

## When attach mode applies

Attach mode is **auto-detected** when:

1. `TaskBackend.tasks_exist()` is true for the active tag, and
2. The expected PRD path is missing (`prd.md` in project root, or `--from-prd` when set).

**Greenfield unchanged:** no tasks and no PRD still fails fast with `PRD file not found`.

## Confirmation UX

| Flag | Behavior |
|------|----------|
| (default, TTY) | Warn with expected PRD path; prompt `Continue attaching without PRD? [y/N]` |
| `--attach` | Skip confirmation prompt (proceed) |
| `--yes` / `-y` | Non-interactive yes (use with `--attach` on CI/non-TTY) |

Non-TTY without `--attach`/`--yes` raises `ProjectSetupError` with remediation to re-run `cyclopsctl init --attach --yes`.

Stderr logs `Brownfield attach mode: continuing with existing task queue (no PRD)` and `Workflow context source: <README.md|repository metadata>` so launch/doctor can distinguish attach from greenfield init. Workflow placeholders come from `resolve_workflow_inputs()` with `prd_path=None` — see `docs/workflow/workflow-readme-fallback.md`.

## Repair sequence (attach only)

Non-destructive repairs only:

1. **Scaffold** — `cyclopsctl.toml`, `.gitignore`, missing handover template paths via `_ensure_scaffold()`.
2. **Workflow files** — `generate_workflow_files()` with `prd_path=None` (absent or generic targets only; customized `ai-context.md`, `update-handover-prompt.md`, `docs/index.md` preserved unless `--force` or `--refresh-workflow`).
3. **Analyze** — `analyze_complexity` only when the complexity report file is missing.
4. **Handover sync** — `sync_current_handover()` from lowest pending parent task via `TaskBackend.get_next()`.

**Explicitly skipped:** `parse_prd`, `.cyclopsctl/last-parsed-prd.json` writes, PRD-change guard.

## Module map

| Piece | Location |
|-------|----------|
| Attach detection and execution | `project_setup.run_project_setup()`, `_expected_prd_path()`, `_confirm_attach_continue()` |
| CLI flags | `cli.py` — `init --attach`, `init --yes` |
| Handover sync without PRD | `bootstrap.sync_current_handover()` (project name from folder / existing handover tech stack) |
| Result flag | `ProjectSetupResult.attach_mode` |

## Tests

Matrix coverage in `tests/test_project_setup.py`:

- (A) tasks + no PRD, confirm yes → scaffold + handover sync, no parse
- (B) customized `ai-context.md` preserved
- (C) no tasks + no PRD → fail fast
- (D) `--attach --yes` non-TTY
- (E) tasks + PRD present → normal init, no attach prompt

Related: `docs/setup/project-setup.md`, `docs/setup/project-scaffold.md`, `docs/testing/brownfield-regression.md` (B5 scenario).
