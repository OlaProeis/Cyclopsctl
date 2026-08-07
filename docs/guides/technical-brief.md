# Cyclopsctl — Technical Brief (for AI agents)

## What this is

**Cyclopsctl** (`cyclopsctl` v1.0) is a **Python 3.10+ CLI harness** that automates a repeatable Cursor-agent development loop for repositories using a **native task queue** (`.cyclopsctl/`) and **file-based handover prompts**.

It is **not** an autonomous coding platform, task planner, or documentation system. It is a **sequencer + verifier**:

1. Resolves which parent task to work on
2. Selects a Cursor model from complexity data
3. Runs an **implementation** agent pass
4. Runs an **update** agent pass on the **same** agent session
5. Verifies the handover file advanced
6. Repeats for N cycles or stops on empty queue / failure

All workflow intelligence (what to implement, how to update docs, how to mark tasks done, what the next handover says) lives in **Markdown prompt files** and in **agent behavior**—not in cyclopsctl code.

---

## Adoption flow

The recommended user path is **install → init → bootstrap → launch**:

```
┌──────────────────────────────────────────────────────────────────┐
│ INSTALL                                                          │
│   install.ps1 / install.sh  OR  pip / pipx / git one-liner      │
│   + CURSOR_API_KEY in .env                                       │
├──────────────────────────────────────────────────────────────────┤
│ INIT (once per repo)                                             │
│   cyclopsctl init [--profile NAME] [--yes]                     │
│   → cyclopsctl.toml, workflow stubs, native tasks, handover    │
├──────────────────────────────────────────────────────────────────┤
│ PLAN (human)                                                     │
│   prd.md + .env (CURSOR_API_KEY)                                 │
├──────────────────────────────────────────────────────────────────┤
│ BOOTSTRAP (when init did not parse PRD)                          │
│   cyclopsctl bootstrap --from-prd prd.md [--with-workflow]     │
│   → parse-prd, analyze-complexity, sync # Task ID in handover    │
├──────────────────────────────────────────────────────────────────┤
│ ANALYZE ONLY (new phase tag missing scores)                      │
│   cyclopsctl analyze-complexity [--tag phase-N]                │
│   → complexity report + task backfill (no PRD parse)             │
├──────────────────────────────────────────────────────────────────┤
│ LAUNCH / RUN                                                     │
│   cyclopsctl launch  (default `cyclopsctl` command)          │
│   OR cyclopsctl run --config cyclopsctl.toml --cycles N      │
│   → doctor preflight, implement → update → verify cycles         │
└──────────────────────────────────────────────────────────────────┘
```

**Existing mature repo:** skip init when files exist; use `bootstrap` after PRD changes or `launch` PRD-change detection. **Power users:** `cyclopsctl run` with flags, profiles, routing TOML, `--task-source`, `--resume`.

End-to-end smoke validation on a fresh directory: [`docs/guides/testing-guide.md`](testing-guide.md).

---

## Problem it solves

Manual workflow without cyclopsctl:

```
new agent → current-handover-prompt.md → wait
         → update-handover-prompt.md (same session) → wait
         → manually start next agent with rewritten handover
```

Failure mode: update pass fails silently → same task repeats with no obvious signal.

Cyclopsctl automates sequencing and **fails the run** when `current-handover-prompt.md` does not advance after the update phase.

---

## Architectural boundaries

### Cyclopsctl responsibilities

| Does | Does not |
|------|----------|
| **`init`** scaffold via `project_setup` / `init_scaffold` | Plan tasks during cycles |
| **`bootstrap`** PRD pipeline via Cursor SDK (`tasks/parse_prd.py`, `tasks/analyze.py`) | Rewrite handover templates |
| Native queue via `cyclopsctl tasks` / `TaskBackend` (`list`, `next`, `show`) | Choose next task content in handovers |
| Read complexity report JSON (read-only) | Split tasks or manage task hierarchies |
| `Agent.create` / `send` / `wait` via `cursor-sdk` | Summarize or own project knowledge |
| Prepend `ai-context.md` to every implementation prompt | Auto-retry failed runs (unless `--retry-on transient`) |
| Snapshot + verify handover advancement | |
| Route models by complexity score + optional `[routing]` TOML | |
| Interactive **`launch`**, **`doctor`**, Rich TUI | |

### Agent responsibilities (via prompt files)

**Implementation phase** (`current-handover-prompt.md` or first-run prompt):

- Implement and test the current parent task only
- Do **not** mark tasks done via `cyclopsctl tasks set-status`
- Do **not** rewrite handover, docs, or `ai-context.md`

**Update phase** (`update-handover-prompt.md`, same agent session):

- Mark tasks done with `cyclopsctl tasks set-status`
- Update `docs/` and `ai-context.md` project memory every cycle (update handover step 2)
- Rewrite `current-handover-prompt.md` for the **next** parent task

---

## Runtime stack

| Component | Role |
|-----------|------|
| **Python 3.10+** | Cyclopsctl language |
| **`cursor-sdk`** (`cursor_sdk`) | `Agent.create`, `send`, `wait`; `local.cwd` = target `project_root` |
| **Native task store** | `.cyclopsctl/tasks/` JSON + `cyclopsctl tasks` CLI |
| **`python-dotenv`** | Load `CURSOR_API_KEY` from project-root `.env` |
| **`rich`** | Live cycle dashboard (`RunDashboardState`, `RichCycleLogger`) |
| **`tomli`** | Parse `cyclopsctl.toml` on Python < 3.11 |

**Required env:** `CURSOR_API_KEY`

---

## Core execution model: one cycle

One cycle = one task. Tasks are flat, self-contained units (integer ids only; there is no subtask hierarchy).

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. RESOLVE TASK          task_selection / --task-id           │
│ 2. ROUTE MODEL           routing.py + models.py                 │
│ 3. PRE-AGENT ALIGNMENT   alignment.py (warn / strict)           │
│ 4. IMPLEMENTATION        session.py + runner.py (new Agent)     │
│ 5. SNAPSHOT handover     prompt.py + verify.py                  │
│ 6. UPDATE                same agent, update_handover only         │
│ 7. VERIFY advancement    verify.py                              │
│ 8. INCREMENT cycle       loop.py                                │
└─────────────────────────────────────────────────────────────────┘
```

### Session rule

- **Implementation:** fresh `Agent.create()` every cycle
- **Update:** second `send()` on the **same** agent handle

Implemented in `session.py` (`CycleSession`).

---

## Python package structure

```
src/cyclopsctl/
├── cli.py              # Entrypoint: launch, run, init, bootstrap, doctor, status, models, tasks
├── config.py           # CyclopsctlConfig, TOML + CLI merge, path validation
├── profiles.py         # [profile.*] / [routing_profile.*] merge precedence
├── init_scaffold.py    # Low-level scaffold helpers and bundled templates
├── project_setup.py    # Idempotent assess-and-setup engine (init, launch PRD change)
├── env.py              # .env loading, --no-env
├── tasks/              # Native store, backend, parse/analyze, CLI
│   ├── store.py        # .cyclopsctl/ JSON persistence
│   ├── backend.py      # TaskBackend protocol + factory
│   ├── native_backend.py
│   ├── cli.py          # cyclopsctl tasks subcommands
│   ├── parse_prd.py    # PRD → tasks via Cursor SDK
│   ├── analyze.py      # Complexity report via Cursor SDK
│   └── types.py        # NextTaskResult, TaskShowDetail, NextTaskLookup
├── bootstrap.py        # PRD bootstrap pipeline, handover sync
├── workflow_gen.py     # PRD-aware ai-context, update-handover, docs/index generation
├── task_selection.py   # handover | sequential | --task-id
├── routing.py          # ComplexityReport, ModelRouter, [routing] TOML rules
├── models.py           # Cursor.models.list(), Opus/Composer presets
├── prompt.py           # Handover read, Task ID parse, hash, ai-context compose
├── alignment.py        # Pre-agent handover vs expected task
├── verify.py           # Post-update snapshot compare
├── preflight.py        # API key checks
├── doctor.py           # cyclopsctl doctor diagnostics table
├── launcher.py         # Interactive pre-run setup, LaunchDispatch argv assembly
├── loop.py             # Main cycle loop (run_cycles)
├── session.py          # CycleSession: new agent impl, reuse for update
├── runner.py           # SDK create/send/wait, activity stream, transient retry
├── interrupt.py        # SIGINT, RunInterruptedError, exit 130
├── state.py            # Crash-recovery state file, cyclopsctl status
├── history.py          # Cross-run resume history, --fresh, --resume skip
├── logging.py          # CycleLogger, CycleLogRecord, JSONL support
├── tui.py              # Rich live dashboard, launch overview, post-run summary
├── sdk_bridge.py       # Windows cursor-sdk bridge bootstrap
├── installer.py        # Global install helpers, PATH remediation, --verify-only
├── version.py          # get_package_version() via importlib.metadata
└── errors.py           # KNOWN_RUN_ERRORS, exit_code_for()
```

Entry point: `cyclopsctl = cyclopsctl.cli:main` (`pyproject.toml`).

| Module | Role |
|--------|------|
| `config` | CLI flags, paths, project root, tag, cycle count, ai-context path, profile merge |
| `profiles` | Named TOML presets; CLI > profile > base TOML |
| `init_scaffold` | Template copy, gitignore, config seed; used by `project_setup` |
| `project_setup` | Init/launch prerequisites, repair, PRD-change guard, `last-parsed-prd.json` |
| `bootstrap` | `cyclopsctl bootstrap`; handover sync from native `tasks show` |
| `workflow_gen` | `--with-workflow` PRD-aware file generation |
| `task_selection` | Per-cycle task source resolution |
| `launcher` | Default `cyclopsctl` / `launch` interactive flow |
| `loop` | Cycle orchestration; empty queue stop; verify after update |
| `history` | `.cyclopsctl/run-history.json`; cycle-1 handover vs bootstrap selection |

---

## CLI surface

```bash
cyclopsctl                    # default: launch
cyclopsctl launch             # preflight + menu → run
cyclopsctl init               # scaffold config + workflow stubs
cyclopsctl bootstrap          # PRD → tasks → complexity → handover sync
cyclopsctl analyze-complexity # score pending tasks on active tag (no parse)
cyclopsctl run                # N implement → update cycles
cyclopsctl doctor | check     # preflight diagnostics
cyclopsctl status             # crash-recovery + history summary
cyclopsctl models             # model inventory + routing presets
cyclopsctl tasks list         # all tasks (Rich table)
cyclopsctl tasks list pending # non-done tasks
cyclopsctl tasks list done    # completed tasks
cyclopsctl tasks show <id>    # full record
cyclopsctl tasks next         # dependency-aware next pending
cyclopsctl tasks set-status --id=<id> --status=done
```

`list` supports optional filters (`pending`, `done`, exact status), `--format json`, and `--plain-table`. See `docs/tasks/tasks-cli.md`.

Config: `cyclopsctl.toml` (see `cyclopsctl.toml.example`). CLI flags override TOML. Profiles via `--profile` on `init` / `run`. Tags via `--tag` or TOML `tag`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success; or empty queue stop |
| 1 | Startup/config/preflight |
| 2 | Agent run failure or handover verification failure |
| 130 | Ctrl+C interrupt |

---

## Task selection (`task_selection.py`)

Default: **`handover`** — read `# Task ID: N` from `current-handover-prompt.md`, resolve via `cyclopsctl tasks show N`.

| `task_source` | Behavior |
|---------------|----------|
| `handover` (default) | Task id from handover file; fallback to `cyclopsctl tasks next` if handover missing (cycle 1) |
| `sequential` | Lowest numeric id among pending tasks |

**`--task-id N`:** pin one task for the first unresolved cycle only, then follow `task_source`.

When selected task ≠ `cyclopsctl tasks next`, cyclopsctl logs a **warning** (not fatal unless alignment strict).

---

## Model routing (`routing.py`, `models.py`)

Reads `.cyclopsctl/reports/complexity-report.json` (read-only). Maps parent `taskId` → `complexityScore` (1–10).

| Score | Model |
|-------|-------|
| 1–5 | Composer (`composer_tier`: standard/fast) |
| 6–8 | Grok (`grok_tier`: standard/fast; default standard) |
| 9–10 | Fable 5 **high thinking** (not Max Mode) |

Optional **`[routing]`** TOML: score bands, `composer_tier`, `grok_tier`, `fable_enabled`, `opus_enabled`, `rules_file`, fallbacks. See [`docs/runtime/model-routing.md`](../runtime/model-routing.md).

Fallback to `default_model` / Composer when report missing, task absent, or Grok/Fable unavailable.

---

## Handover contracts (`prompt.py`, `verify.py`, `alignment.py`)

### `current-handover-prompt.md`

- Must contain: `# Task ID: <integer>` (parent only)
- **Only the update-phase agent may rewrite this file**

### `update-handover-prompt.md`

- Fixed user template; cyclopsctl passes through verbatim

### `ai-context.md`

- Prepended to implementation prompts with `# AI Context` and `---` separator
- Update phase sends `update_handover` only (no ai-context re-attach)

### Verification

**Pass if:** Task ID changed, OR hash changed and `cyclopsctl tasks next` advanced.  
**Fail if:** stuck on same task after update.

---

## Testing approach

### Unit and integration tests

```bash
python -m pytest
```

~40 test modules under `tests/` covering: config, profiles, init, bootstrap, project_setup, workflow_gen, env, prompt, verify, alignment, routing, models, session, runner, loop, tasks, doctor, launcher, TUI, interrupt, SDK bridge, installer, distribution, history, e2e, CLI.

Tests use injectable hooks (`get_next_task_fn`, `session_factory`, `router`, `cycle_logger`) for deterministic cycle testing without live Cursor API calls.

### User-facing doc checks

`tests/test_readme_docs.py` asserts README scenario sections (install → init → launch), advanced CLI coverage, fresh-repo-testing link, resolved license, and technical-brief module map completeness.

### Fresh-repo manual smoke

[`docs/guides/testing-guide.md`](testing-guide.md) — checklist for install → init → bootstrap → doctor → optional live `run` in a **new empty directory** outside the dev repo.

---

## Workflow file ecosystem (target repo)

| File | Owner | Purpose |
|------|-------|---------|
| `prd.md` | Human | Product requirements |
| `cyclopsctl.toml` | `init` / human | Run defaults, profiles, routing |
| `ai-context.md` | Update agent / `workflow_gen` | Architecture + phase rules |
| `current-handover-prompt.md` | Update agent / bootstrap | Next implementation task |
| `update-handover-prompt.md` | Human/template / `workflow_gen` | Fixed update-phase rules |
| `docs/index.md` | Update agent / `workflow_gen` | Doc map only |
| `.cyclopsctl/tasks/tasks.json` | Native store | Task state |
| `.cyclopsctl/reports/complexity-report.json` | Native analyze | Model routing input (read-only) |
| `prompts/setup-ai-workflow.md` | Bootstrap only | Greenfield when no handover |

---

## Design invariants (do not violate when extending)

1. **Native task store only** — queue operations via `TaskBackend` / `cyclopsctl tasks`.
2. **Handover passthrough**—cyclopsctl never rewrites `update-handover-prompt.md`.
3. **Parent tasks only** for cycle scope and `# Task ID:` marker.
4. **Two-phase discipline** enforced by prompt files, not cyclopsctl code.
5. **Fail closed** on stuck handovers after update.
6. **New agent per implementation**, same agent for update.
7. **Empty queue is success**, not error.
8. **Modularity:** one concern per file; thin `cli.py`.

---

## What "done" looks like (v1.0)

The built system can:

- **Install** globally via git `pip install` or shell scripts (`installer.py`)
- **`init`** scaffold and **`bootstrap`** PRD pipeline with optional workflow generation
- **`launch`** interactively or **`run`** headlessly with profiles, tags, routing
- Run N verified implement→update cycles unattended
- Route models from complexity report with Composer/Opus fallback
- Detect stuck handovers and stop instead of blind looping
- Resume with `--resume` skipping completed parent tasks
- Preflight via `cyclopsctl doctor`; Rich dashboard + plain log mode
- Graceful Ctrl+C with exit 130; Windows SDK bridge bootstrap

Full product spec: `prd.md`. Module-level docs: `docs/index.md`. User-facing overview: `README.md`.
