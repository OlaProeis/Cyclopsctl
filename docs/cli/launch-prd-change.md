# Launch PRD-change detection and new-tag flow

When an existing project updates `prd.md`, `cyclopsctl launch` (RUN action) detects the change and starts a fresh native task queue on a new tag instead of re-parsing into the prior tag.

## When it runs

PRD-change handling runs at the start of the **Run** launch path when:

- `cyclopsctl.toml` and native task storage exist (initialized project)
- `prd.md` is present
- `.cyclopsctl/last-parsed-prd.json` records a prior parse
- The current PRD SHA-256 differs from the stored hash
- Tasks already exist in the tag recorded by `last-parsed-prd.json`

If the project is not initialized, launch exits with *Run `cyclopsctl init` first*. If `prd.md` is missing, PRD-change detection is skipped.

Unchanged PRD: no tag creation; launch continues with the existing queue.

## New-tag pipeline

When a changed PRD is detected on a mature project:

1. Propose a tag name (`phase-N` from PRD title, title slug, or `prd-YYYY-MM-DD` fallback; avoid collisions)
2. `NativeTaskBackend.add_tag` (skipped when `--tag` names an existing tag)
3. `NativeTaskBackend.use_tag`
4. Parse PRD via Cursor SDK for the new tag
5. Analyze complexity for the new tag
6. Refresh workflow files from PRD (`force_workflow=True`)
7. Sync `current-handover-prompt.md` for the first pending task
8. Update `.cyclopsctl/last-parsed-prd.json`

Prior tags are never deleted. The active tag for the session is set on the assembled `cyclopsctl run` argv.

## Tag naming

| Input | Example tag |
|-------|-------------|
| `# PRD: v2 expansion` | `v2-expansion` |
| `# Feature Dashboard` | `feature-dashboard` |
| No heading | `prd-2026-06-05` |
| Collision with existing tag | `feature-dashboard-2` |

Non-interactive launches (`--yes`) auto-accept the proposed tag. Interactive TTY mode can confirm or edit the tag name.

## Guard during init

`cyclopsctl init` (`project_setup.run_project_setup`) refuses to re-parse when tasks exist and the PRD hash changed. It prints *PRD changed — run `cyclopsctl launch` to start a new task list.* Launch owns new-tag creation.

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `handle_launch_prd_change` | `project_setup.py` | PRD hash compare, tag pipeline, state update |
| `propose_tag_name` / `slugify_tag_name` | `project_setup.py` | Tag slug from PRD title or date |
| `load_last_parsed_prd` / `write_last_parsed_prd` | `project_setup.py` | Parse state under `.cyclopsctl/` |
| `_apply_prd_change_at_launch` | `launcher.py` | Run-path integration, status refresh |

Tests: `tests/test_launch_prd_change.py`, `tests/test_project_setup.py` (PRD-change guard).

Related: [launch-cli.md](launch-cli.md), [project-setup.md](../setup/project-setup.md), [prd-bootstrap.md](../setup/prd-bootstrap.md).
