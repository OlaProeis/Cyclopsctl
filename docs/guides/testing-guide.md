# Testing guide

Manual checklist for validating Cyclopsctl on a **fresh repository** and on an **existing repository**. Automated coverage lives in `python -m pytest` (no live Cursor API required for most tests).

**Requirements:** Python 3.10+, `CURSOR_API_KEY` in the target project's `.env` for live runs and native PRD parse/analyze.

---

## Before you start

Install the cyclopsctl once per machine:

```powershell
# Windows (from the dev repo or after clone)
.\install.ps1

# macOS / Linux
./install.sh

# Or editable while developing this repo
pip install -e ".[dev]"
```

Verify:

```powershell
cyclopsctl --version
```

From any project directory, diagnostics use the **current working directory** as the project root unless you pass `--project-root`:

```powershell
cd G:\path\to\your-project
cyclopsctl doctor
cyclopsctl tasks list
cyclopsctl status
```

---

## Part A — Fresh repository (greenfield)

Use an **empty directory outside** the CursorOrchestrator dev repo so paths and history do not collide.

### A1. Create the test project

```powershell
mkdir G:\DEV\OrchestratorTestFresh
cd G:\DEV\OrchestratorTestFresh
git init
```

### A2. Add credentials and a minimal PRD

`.env`:

```env
CURSOR_API_KEY=your_key_here
```

`prd.md` — three small tasks is enough. Copy the smoke fixture:

```powershell
copy G:\DEV\CursorOrchestrator\tests\fixtures\minimal-smoke-prd.md prd.md
```

### A3. Init (one-time setup)

```powershell
cyclopsctl init --profile solo-default --yes
```

**Expect:**

| Artifact | Present |
|----------|---------|
| `cyclopsctl.toml` | Yes |
| `.cyclopsctl/tasks/tasks.json` | Yes (parsed from PRD) |
| `.cyclopsctl/reports/complexity-report.json` | Yes |
| `current-handover-prompt.md` with `# Task ID: 1` | Yes |
| `ai-context.md`, `update-handover-prompt.md` | Yes |

### A4. Doctor

```powershell
cyclopsctl doctor
```

**Expect:** `[PASS]` for API key, SDK bridge, native tasks, complexity report. Exit code `0`.

### A5. Launch (interactive)

```powershell
cyclopsctl launch
```

**Expect:** Pending count > 0, handover task id shown, cycles prompt. Choose **1 cycle** for a first live test.

Alternatively, non-interactive:

```powershell
cyclopsctl run --cycles 1 --plain
```

### A6. After one cycle

```powershell
cyclopsctl tasks list pending
cyclopsctl status
```

**Expect:**

- Task 1 marked `done` in native storage (update phase responsibility)
- `current-handover-prompt.md` advanced to the next pending parent id (or `# Task ID: 0` if queue empty)
- Run history updated under `.cyclopsctl/run-history.json`

### A7. Optional dry-run (no API spend)

```powershell
cyclopsctl run --dry-run --cycles 1
```

**Expect:** Resolves task, model, and alignment without starting an agent.

---

## Part B — Existing repository (brownfield)

Use a repo that **already has code** and/or an **in-progress task queue**. Copy a pytest fixture for a reproducible starting point:

```powershell
mkdir G:\DEV\OrchestratorTestBrownfield
Copy-Item -Recurse G:\DEV\CursorOrchestrator\tests\fixtures\brownfield-mid-backlog\* G:\DEV\OrchestratorTestBrownfield\
cd G:\DEV\OrchestratorTestBrownfield
```

Add `.env` with your real `CURSOR_API_KEY` for live runs.

### B1. Mid-backlog (tasks + handover aligned)

Fixture: `brownfield-mid-backlog` — tasks 1–5 done, handover at task 6.

```powershell
cyclopsctl doctor
cyclopsctl tasks list pending
cyclopsctl launch
```

**Expect:** Doctor passes; pending includes task 6; launch offers cycles without init failure; one cycle completes **implement → update → verify**.

### B2. PRD changed on a mature project

Fixture: `brownfield-prd-change` — `last-parsed-prd.json` hash differs from current `prd.md`.

```powershell
cyclopsctl launch
```

**Expect:** Launch detects PRD change, offers new tag / re-parse flow (confirm on TTY). New tag tasks should have complexity scores after parse (launch forces analyze even when a prior-phase report exists). If scores are missing on an older install, run `cyclopsctl analyze-complexity` — see [`docs/cli/analyze-complexity-cli.md`](../cli/analyze-complexity-cli.md).

### B3. Resume after partial run

Fixture: `brownfield-resume` — run history lists completed parent ids.

```powershell
cyclopsctl run --resume --cycles 2 --plain
```

**Expect:** Skips tasks already in `completed_cycle_task_ids`; continues from next pending handover.

### B4. Tasks exist, no PRD (attach)

Fixture: `brownfield-no-prd`

```powershell
cyclopsctl init --attach --yes
cyclopsctl doctor
cyclopsctl launch
```

**Expect:** No parse-prd; scaffold repairs; handover synced from queue; launch works without `prd.md`.

### B5. PRD exists, no tasks (continue)

Fixture: `brownfield-prd-only`

```powershell
cyclopsctl init --yes
# optional explicit PRD path: cyclopsctl init --from-prd prd.md --yes
```

**Expect:** Warns about SDK cost; parses PRD into native tasks; syncs handover.

### B6. Customized workflow files preserved

Fixture: `brownfield-customized-workflow`

```powershell
cyclopsctl init --yes
```

**Expect:** Custom `ai-context.md` and `docs/index.md` **not** overwritten without `--force`.

---

## Automated regression (no live API)

From the dev repo:

```powershell
cd G:\DEV\CursorOrchestrator
python -m pytest
```

Key integration modules:

| Test file | Covers |
|-----------|--------|
| `tests/test_fresh_repo_integration.py` | Mocked init → launch on empty tree |
| `tests/test_brownfield_integration.py` | Fixtures under `tests/fixtures/brownfield-*` — B1–B6 init/launch chains |
| `tests/test_project_setup.py` | Init modes, attach, continue |
| `tests/test_tasks_cli.py` | Native CRUD |
| `tests/test_doctor.py` | Preflight diagnostics |

---

## Quick reference — native task CLI

```powershell
cyclopsctl tasks list
cyclopsctl tasks list pending
cyclopsctl tasks list done --format json
cyclopsctl tasks show <id> --format json
cyclopsctl tasks next --format json
cyclopsctl tasks set-status --id=<id> --status=done
```

Bare `list` prints a Rich table (ID, title, status, complexity, dependencies). Add `--plain-table` for fixed-width text. See [`docs/tasks/tasks-cli.md`](../tasks/tasks-cli.md).

Queue storage: `.cyclopsctl/tasks/tasks.json` (active tag in `.cyclopsctl/tasks/state.json`).

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `Missing required setting: cycles` on `run` | Pass `--cycles N` or set `cycles` in `cyclopsctl.toml` |
| Doctor fails on API key | `.env` in **target project root** with `CURSOR_API_KEY=` |
| Launch shows 0 pending but you expect work | `cyclopsctl tasks list pending` — handover may show `# Task ID: 0` (queue complete) |
| Handover drift warning | `cyclopsctl bootstrap --sync-handover-only` or rewrite handover from `cyclopsctl tasks show <id>` |
| Stale workflow files mention old CLI | `cyclopsctl init --refresh-workflow` |
