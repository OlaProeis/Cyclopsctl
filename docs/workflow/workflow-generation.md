# Project-aware workflow generation

`cyclopsctl bootstrap --with-workflow` generates project-specific workflow Markdown from `prd.md` and repository signals instead of copying generic stubs.

## Command

```bash
cyclopsctl bootstrap --with-workflow
cyclopsctl bootstrap --sync-handover-only --with-workflow
cyclopsctl bootstrap --with-workflow --force-workflow
cyclopsctl bootstrap --with-workflow --force ai-context.md
cyclopsctl bootstrap --with-workflow --tech-stack "Python 3.11, FastAPI" --test-cmd "python -m pytest"
```

| Flag | Effect |
|------|--------|
| `--with-workflow` | Generate `ai-context.md`, `update-handover-prompt.md`, and `docs/index.md` when appropriate |
| `--force-workflow` | Overwrite workflow files even when customized |
| `--force PATH` | Force overwrite specific workflow file (repeatable) |
| `--tech-stack TEXT` | Override tech stack hint in generated files |
| `--test-cmd CMD` | Override test command in generated files |

## Outputs

| File | Behavior |
|------|----------|
| `ai-context.md` | Project name, tech stack, test command, workflow rules (Context7 MCP, `cyclopsctl tasks` CLI-only, doc conventions), Where Things Live |
| `update-handover-prompt.md` | Project root paths, native task commands (`cyclopsctl tasks set-status` / `list` / `show`), test command in final checks |
| `.cursor/rules/cyclopsctl/*.mdc` | Cyclopsctl-owned Cursor rules (implementation vs update phase, native task CLI) — installed alongside workflow files |
| `docs/index.md` | Minimal index stub — **created only when absent** unless forced |
| `current-handover-prompt.md` | **Never** written by workflow generation (owned by bootstrap handover sync) |

## Non-destructive rules

Generation runs when a target is **missing** or still matches the bundled generic stub (`ai-context.md`, `update-handover-prompt.md` from `src/cyclopsctl/templates/`). Customized files are skipped unless `--force-workflow` or `--force` applies.

Generic stubs copied by `cyclopsctl init` are detected and replaced on the next `--with-workflow` run.

## Context resolution

`workflow_gen.py` resolves inputs deterministically (no LLM). Preference order: **PRD → README → repository metadata**. See `workflow-readme-fallback.md` for attach-init and no-PRD paths.

### PRD heuristics

- **Project name** — first `#` heading in `prd.md` (strips `PRD:` prefix) or repository directory name
- **Tech stack** — `Tech Stack` / `Technology Stack` / `Requirements` sections, bullet lists, or inline hints (Python, FastAPI, Node.js, etc.)
- **Test command** — PRD fenced blocks / testing sections, then repo signals, default `python -m pytest`

### README / repo fallback (no PRD)

- **Project name** — first `#` in `README.md`, else folder name
- **Tech stack** — README sections and fenced blocks, then `pyproject.toml` / `package.json` / etc.; `See prd.md` only when PRD was the source but stack is empty
- **Test command** — README Testing / Development / Quick start sections, then repo signals, default `python -m pytest`

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `generate_workflow_files` | `workflow_gen.py` | Main generation entry point |
| `resolve_workflow_inputs` | `workflow_gen.py` | PRD/repo heuristic resolution |
| `is_generic_workflow_file` | `workflow_gen.py` | Detect bundled stub copies |
| `generate_workflow_from_bootstrap` | `workflow_gen.py` | Bootstrap pipeline wrapper |
| `install_cyclopsctl_cursor_rules` | `workflow_gen.py` | Copy bundled rules to `.cursor/rules/cyclopsctl/` |
| `run_bootstrap` | `bootstrap.py` | Calls workflow gen when `--with-workflow` is set |

Templates live in `src/cyclopsctl/templates/workflow-*.md`, `docs-index.md`, and `cursor-rules/cyclopsctl/` (with `{{PROJECT_NAME}}`, `{{PROJECT_ROOT}}`, `{{TECH_STACK}}`, `{{TEST_CMD}}` placeholders). See `native-workflow-templates.md` for native CLI conventions and init behavior.

Golden-file tests: `tests/test_workflow_gen.py`, fixtures under `tests/fixtures/workflow_gen/`.
