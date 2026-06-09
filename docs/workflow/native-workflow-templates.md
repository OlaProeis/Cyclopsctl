# Native workflow templates and Cursor rules

Generated and bundled workflow files use **`cyclopsctl tasks`** commands for queue operations. Cyclopsctl-owned Cursor rules reinforce the implementation vs update phase split for agents.

## Generated workflow files

`workflow_gen.py` renders project-aware Markdown from PRD/repo heuristics:

| Output | Template source | Native CLI references |
|--------|-----------------|------------------------|
| `ai-context.md` | `workflow-ai-context.md` | Update phase: `cyclopsctl tasks list pending`, `show`, `set-status` with `--project-root` |
| `update-handover-prompt.md` | `workflow-update-handover-prompt.md` | Step commands use `cyclopsctl tasks set-status`, `list pending`, `show` |

For queue inspection (humans and debugging), bare **`cyclopsctl tasks list`** shows all tasks in a table (ID, status, complexity, dependencies). Filters: `pending`, `done`, or an exact status. See `docs/tasks/tasks-cli.md`.
| `docs/index.md` | `docs-index.md` | No task CLI (index stub only) |

Generic stubs (`templates/ai-context.md`, `templates/update-handover-prompt.md`) ship for init/bootstrap copy paths and are replaced when workflow generation detects unchanged stub content.

Placeholders: `{{PROJECT_NAME}}`, `{{PROJECT_ROOT}}`, `{{TECH_STACK}}`, `{{TEST_CMD}}`.

## Cyclopsctl Cursor rules

Bundled rules live in `src/cyclopsctl/templates/cursor-rules/cyclopsctl/`. `install_cyclopsctl_cursor_rules()` copies them to `.cursor/rules/cyclopsctl/` (non-destructive by default; `--force-workflow` overwrites).

`agent-workflow.mdc` documents:

- **Implementation phase** — work from `current-handover-prompt.md`; no task status changes or handover edits.
- **Update phase** — follow `update-handover-prompt.md`; use `cyclopsctl tasks` CLI with `--project-root`.
- Example commands for `set-status`, `list pending`, and `show`.

`cyclopsctl_cursor_rules_present()` checks whether rules are already installed.

## Cyclopsctl Cursor skill

Bundled skill lives in `src/cyclopsctl/templates/skills/cyclopsctl/SKILL.md` (packaged in the wheel). `install_cyclopsctl_skill()` copies it to `.cursor/skills/cyclopsctl/SKILL.md` (non-destructive by default; `--force-workflow` overwrites).

Cursor discovers **project skills only** under `.cursor/skills/<name>/SKILL.md` — not at the repo root. Commit `.cursor/skills/cyclopsctl/` in cyclopsctl-managed projects so IDE agents know `cyclopsctl tasks` and related CLI outside orchestrated runs.

`cyclopsctl_skill_present()` checks whether the skill is already installed.

## Init rules and skill install

`NativeTaskBackend.init_project()` calls `install_cyclopsctl_cursor_rules()` and `install_cyclopsctl_skill()`. `project_setup.py` records `"cyclopsctl rules"` and `"cyclopsctl skill"` in repairs when those files are added.

## Brownfield compatibility

Workflow generation remains non-destructive: customized `ai-context.md` / `update-handover-prompt.md` are skipped unless forced. Handover sync (`bootstrap`, `project_setup`) uses `render_synced_handover()` in `prompt.py` with the same native CLI references as generated workflows.

The init/bootstrap placeholder (`templates/current-handover-prompt.md`) directs humans to `cyclopsctl bootstrap` instead of manual task creation. Bundled regression covers all workflow stubs, the bootstrap handover template, and cyclopsctl Cursor rules.

Stale brownfield copies with outdated CLI references are addressed via workflow refresh / doctor staleness checks — see `workflow-refresh.md`.

## Tests

- Golden files: `tests/fixtures/workflow_gen/expected/`
- Regression: `tests/test_workflow_gen.py` (native-path artifacts, customization skip logic, cursor rule install idempotency)
- Full setup: `tests/test_project_setup.py::test_fresh_repo_runs_full_setup_pipeline`
