# Brownfield regression testing

Automated and manual coverage for in-progress repository adoption (partial queues, PRD change, resume, attach, continue, and customized workflows) on a directory outside the dev repo.

## Manual runbook

[`docs/guides/testing-guide.md`](../guides/testing-guide.md) is the canonical manual checklist: fresh repo (Part A) and existing repo / brownfield (Part B). Live Cursor API runs are optional.

## Pytest fixtures

| Path | Scenario |
|------|----------|
| `tests/fixtures/brownfield-mid-backlog/` | **B1** — tasks 1–5 done, handover at task 6, complexity report present |
| `tests/fixtures/brownfield-prd-change/` | **B2** — mature queue + `last-parsed-prd.json` hash drift for launch PRD-change flow |
| `tests/fixtures/brownfield-resume/` | **B3** — mid-backlog plus `.cyclopsctl/run-history.json` with completed cycle IDs 1–5 |
| `tests/fixtures/brownfield-no-prd/` | **B4** — native queue + README, no `prd.md` or `cyclopsctl.toml`; attach init (`--attach --yes`) |
| `tests/fixtures/brownfield-prd-only/` | **B5** — `prd.md` + `src/`, empty queue; continue init (`init --yes`, mocked parse) |
| `tests/fixtures/brownfield-customized-workflow/` | **B6** — custom `ai-context.md` and `docs/index.md`; repeat init must not overwrite |

B4 omits `prd.md` by design. B5 omits `.cyclopsctl/tasks/` until continue init runs. Copy a tree into a temp directory with `shutil.copytree` (see integration tests).

## Integration tests

`tests/test_brownfield_integration.py` loads each fixture without live API access via `SCENARIO_FIXTURES`:

1. Assert runbook scenarios (B1–B6) appear in `docs/guides/testing-guide.md`
2. **B1** — pending queue, handover Task ID 6, native doctor checks, launch preflight output
3. **B2** — `prd_hash_changed()` and mocked `handle_launch_prd_change()` new-tag flow
4. **B3** — run history alignment and `_resolve_runnable_task()` resume skip of completed IDs
5. **B4** — `init --attach --yes` repairs scaffold + handover, no parse/`last-parsed-prd`, launch doctor passes
6. **B5** — `init --yes` continue mode (mocked parse/analyze), handover at task 1, launch doctor passes
7. **B6** — `run_project_setup()` preserves customized `ai-context.md`, `docs/index.md`, and `update-handover-prompt.md`

Use `get_task_backend(TaskBackendConfig(task_backend="native"))` in tests.

Related coverage: `tests/test_project_setup.py` (attach/continue matrix), `tests/test_launch_readiness.py`, `tests/test_launch_prd_change.py`, `tests/test_task_resume.py`, `tests/test_fresh_repo_integration.py`, `tests/test_readme_docs.py` (README brownfield links).

## CI guidance

Keep CI pytest-only unless live secrets are configured. The six fixture trees plus `test_brownfield_integration.py` gate brownfield regression without SDK calls.
