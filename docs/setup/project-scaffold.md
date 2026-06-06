# Project scaffold (`cyclopsctl init`)

`cyclopsctl init` is the single first-time setup command. It delegates to the assess-and-setup engine (`project_setup.run_project_setup()`) to prepare a repository for `cyclopsctl launch` without a separate bootstrap step.

## Command

```bash
cyclopsctl init --project-root G:/DEV/MyProject
cyclopsctl init --profile solo-default
cyclopsctl init --dry-run
cyclopsctl init --skip-templates
cyclopsctl init --force cyclopsctl.toml
cyclopsctl init --force-all
```

Default project root is the current working directory. `--config` overrides the output path for `cyclopsctl.toml`.

## Prerequisites (fail fast)

| Check | On failure |
|-------|------------|
| `CURSOR_API_KEY` in env or `.env` | Remediation to create `.env` |
| `prd.md` in project root | No partial setup (attach mode when tasks already exist) |

## What init does

On a fresh repo with prerequisites met, init runs the full setup pipeline:

1. Scaffold `cyclopsctl.toml` (profile-aware), `.gitignore` entries, and `current-handover-prompt.md` placeholder when missing
2. Initialize native task storage (`.cyclopsctl/tasks/`) and cyclopsctl Cursor rules
3. Generate PRD-aware workflow files (`ai-context.md`, `update-handover-prompt.md`, `docs/index.md`) — non-destructive for customized files
4. Parse `prd.md` into the `master` tag when no tasks exist; analyze complexity as needed
5. Sync `current-handover-prompt.md` from the native queue
6. Persist `.cyclopsctl/last-parsed-prd.json` after first parse

Re-runs on an already-ready project are idempotent: no overwrites, prints **Already ready.** with pending count and next task, then directs the user to `cyclopsctl launch`.

Init never prompts for cycles or starts a run.

## Power-user flags

| Flag | Behavior |
|------|----------|
| `--profile NAME` | Merge named profile defaults into seeded `cyclopsctl.toml` |
| `--skip-templates` | Write only `cyclopsctl.toml`; skip gitignore and handover template |
| `--force PATH` | Allow overwriting specific scaffold paths (repeatable) |
| `--force-all` | Allow overwriting scaffold targets and force workflow regeneration |
| `--dry-run` | List planned repairs without mutating the filesystem |
| `--refresh-workflow` | Regenerate stale workflow files selectively |

`--no-env` skips loading `.env` in the CLI wrapper; `project_setup` still loads project `.env` when present.

## Success output

```text
Ready. Run `cyclopsctl launch` when you want to start building.
  Tag: master
  Pending tasks: N
  Next task: <id> — <title>
```

When repairs were applied, the CLI also prints `Applied: <repair list>`.

## Modules

| Symbol | Module | Role |
|--------|--------|------|
| `_init_command` | `cli.py` | CLI flags, env load, delegates to `run_project_setup()` |
| `run_project_setup` | `project_setup.py` | Idempotent assess-and-setup engine |
| `run_init_scaffold` | `init_scaffold.py` | Low-level scaffold helpers reused by `project_setup` |
| `format_ready_message` | `project_setup.py` | Post-init ready messaging |

Tests: `tests/test_init_integration.py`, `tests/test_init_scaffold.py`, `tests/test_project_setup.py`.

Related: `docs/setup/project-setup.md`, `docs/setup/config-profiles.md`, `docs/setup/prd-bootstrap.md`.
