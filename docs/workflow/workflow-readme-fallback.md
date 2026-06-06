# README and repo-metadata workflow fallback

When `prd.md` is absent, `workflow_gen.resolve_workflow_inputs()` derives workflow placeholders from `README.md` and repository signals instead of emitting generic stubs or `See prd.md` tech-stack placeholders.

## Context-source preference

Resolution order (generation-time only — never read during cyclopsctl cycles):

1. **PRD** — explicit `prd_path` or auto-detected `prd.md` in `generate_workflow_files()`
2. **README** — `README.md` in project root
3. **Repository metadata** — dependency files (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Makefile`)

`WorkflowInputs.context_source` is `"prd"`, `"readme"`, or `"repo"`. Human labels via `context_source_label`: `prd.md`, `README.md`, or `repository metadata`.

## README heuristics (v1)

| Field | Source |
|-------|--------|
| Project name | First markdown `#` heading |
| Tech stack | `Tech Stack` / `Technology Stack` section, fenced blocks, inline hints, then repo dependency files |
| Test command | Sections matching Testing / Development / Quick start / Getting started, then fenced blocks |

Fallbacks when README lacks data: folder name for project name; `python -m pytest` for test command only after PRD, README, and repo detection all miss.

`See prd.md` appears in **Tech Stack** only when context source is PRD and no stack could be extracted. README and repo paths never emit it.

## Attach init integration

Brownfield attach (`project_setup.run_project_setup()` with `prd_path=None`) calls `generate_workflow_files()` without a PRD. After attach confirmation, stderr logs:

```text
cyclopsctl init: Workflow context source: README.md
```

(or `repository metadata` when `README.md` is missing).

## Module map

| Symbol | Module | Role |
|--------|--------|------|
| `resolve_workflow_inputs` | `workflow_gen.py` | Context-source resolution and placeholder values |
| `WorkflowInputs` | `workflow_gen.py` | `context_source`, `context_source_label` |
| Attach context log | `project_setup.py` | Prints context source after attach-mode confirmation |

## Tests

- `tests/test_workflow_gen.py` — README variants (Python, Node, minimal), missing-README repo fallback, golden `ai-context.md` without PRD
- `tests/test_project_setup.py` — attach init uses README context and logs source label
- Fixtures: `tests/fixtures/workflow_gen/{python-readme,node-readme,minimal-readme}.md`

Related: `docs/workflow/workflow-generation.md`, `docs/setup/brownfield-attach-init.md`.
