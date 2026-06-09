---
name: cyclopsctl
description: >-
  Manage Cyclopsctl native task queues and project setup outside orchestrated
  runs. Use when the user asks to view, update, or work on tasks, mark task
  status, sync handover, switch tags or phases, inspect the queue, or run
  cyclopsctl CLI commands (cyclopsctl tasks, bootstrap, doctor, status, init,
  launch).
---

# Cyclopsctl (ad-hoc operator)

Help a **human in the IDE** manage a Cyclopsctl project **outside** an active `cyclopsctl run`. Run CLI commands; do not guess task state from files alone.

## Project root

Pass `--project-root` on every command (default: cwd). Use the repo root that contains `.cyclopsctl/`.

```bash
cyclopsctl tasks list --project-root {{PROJECT_ROOT}}
```

## Task queue (`cyclopsctl tasks`)

```bash
cyclopsctl tasks list --project-root {{PROJECT_ROOT}}
cyclopsctl tasks list pending --project-root {{PROJECT_ROOT}}
cyclopsctl tasks list done --format json --project-root {{PROJECT_ROOT}}
cyclopsctl tasks show <id> --format json --project-root {{PROJECT_ROOT}}
cyclopsctl tasks next --format json --project-root {{PROJECT_ROOT}}
cyclopsctl tasks set-status --id=<id> --status=done --project-root {{PROJECT_ROOT}}
cyclopsctl tasks tags --project-root {{PROJECT_ROOT}}
cyclopsctl tasks use-tag <name> --project-root {{PROJECT_ROOT}}
```

Shared flags: `--tag <name>`, `--format json|plain`.

**Statuses:** `pending`, `in-progress`, `done`, `review`, `cancelled`, `blocked`, `deferred`.

### Picking the "next" task

| User intent | Command |
|-------------|---------|
| Show pending work | `tasks list pending` |
| Mark task done | `tasks set-status --id=N --status=done` |
| Task details | `tasks show N --format json` |
| Handover / "next task to implement" | `list pending --format json`, then **lowest numeric parent id** |
| Cyclopsctl run selection | `tasks next` (dependency-aware; may differ from lowest id) |

Storage: `.cyclopsctl/tasks/tasks.json`, active tag in `.cyclopsctl/tasks/state.json`.

## Handover sync (no agent run)

When the queue exists but `current-handover-prompt.md` is missing or stale:

```bash
cyclopsctl bootstrap --sync-handover-only --project-root {{PROJECT_ROOT}}
```

Synced handover must include `# Task ID: <n>`.

## Diagnostics

```bash
cyclopsctl doctor --project-root {{PROJECT_ROOT}}
cyclopsctl status --project-root {{PROJECT_ROOT}}
```

`status` reports the last run's outcome (running / completed / interrupted / failed) from `.cyclopsctl/state.json` — use it to check whether a previous run crashed before touching the queue.

## Setup and queue bootstrap

Requires `CURSOR_API_KEY` (from project `.env` or environment).

```bash
cyclopsctl init --project-root {{PROJECT_ROOT}}
cyclopsctl bootstrap --project-root {{PROJECT_ROOT}}
cyclopsctl bootstrap --from-prd prd.md --project-root {{PROJECT_ROOT}}
cyclopsctl analyze-complexity --project-root {{PROJECT_ROOT}}
```

Bootstrap may refuse destructive re-parse — use `cyclopsctl launch --prd <file>` for a new phase tag.

## Starting runs

Only when the user explicitly wants to **start** agent cycles:

```bash
cyclopsctl
cyclopsctl launch --project-root {{PROJECT_ROOT}}
cyclopsctl run --cycles <n> --project-root {{PROJECT_ROOT}}
```

## During orchestrated runs

Implementation agents must **not** mutate the queue or handover files — the cyclopsctl owns that. This skill applies to **interactive IDE** requests outside runs.

## Reference

Every command supports `--help` (e.g. `cyclopsctl tasks --help`, `cyclopsctl run --help`). Full documentation lives in the cyclopsctl source repository under `docs/` (e.g. `docs/tasks/tasks-cli.md`, `docs/index.md`) and may not be present in this project.
