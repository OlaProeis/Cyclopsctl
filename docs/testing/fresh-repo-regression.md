# Fresh-repo regression testing

Automated and manual coverage for the two-command adoption path (`cyclopsctl init` → `cyclopsctl launch`) on a directory outside the dev repo.

## Manual runbook

[`docs/guides/testing-guide.md`](../guides/testing-guide.md) Part A is the canonical checklist for human validation: install, create an empty project, add `.env` + `prd.md`, run `init`, then `launch`. Live Cursor API runs are optional.

## Pytest fixture

| Path | Role |
|------|------|
| `tests/fixtures/minimal-smoke-prd.md` | Minimal 3-task PRD copied into temp repos during integration tests |

The fixture includes tech stack, numbered requirements (`test-run/output-*.txt`), and a `python -m pytest` verification hint for workflow generation.

## Integration tests

`tests/test_fresh_repo_integration.py` exercises the init → launch chain without live API access:

1. Copy `minimal-smoke-prd.md` into a temp directory with a stub `.env` (`CURSOR_API_KEY` only)
2. Mock native parse-prd and analyze-complexity (writes `.cyclopsctl/tasks/tasks.json` and complexity report)
3. Run `cyclopsctl init --project-root <temp>`
4. Run `cyclopsctl launch --plain --yes --action run --cycles 1` with `_run_command` mocked
5. Assert scaffold files, handover Task ID, native storage paths, and dispatched run argv

Related coverage: `tests/test_init_integration.py`, `tests/test_project_setup.py`, `tests/test_bootstrap.py`.

## CI guidance

Keep CI pytest-only unless live secrets (`CURSOR_API_KEY`) are configured. The fixture-based integration test is sufficient for regression gating on the two-command native flow.
