# PRD: Cyclopsctl

## Overview

This product is a lightweight orchestration layer for a Cursor-based AI development workflow. Its purpose is to automate a strict, repeatable handover pattern between sequential agent runs while keeping project control in file-based prompts and the native task queue (`.cyclopsctl/tasks/`) rather than embedding workflow intelligence into the cyclopsctl itself.

The cyclopsctl is intentionally narrow in scope. It does not plan tasks, interpret project requirements, rewrite handover templates, or manage project knowledge directly. It sequences agent runs, waits for completion, resumes the same session for the update pass, starts a fresh session for each implementation phase, validates that handovers advanced correctly, selects models from complexity data, and stops after a configured number of cycles.

## Problem

The current workflow requires repeated manual steps: starting a new Cursor agent, providing the correct prompt or handover file, waiting for completion, sending a standard update handover prompt, and then manually starting the next agent with the newly prepared current handover file. This pattern works, but the repetition creates friction and increases the chance of inconsistency between tasks.

Failed or partial update runs are especially risky: the next cycle may repeat the same task with no obvious signal until a human notices. The goal is to preserve the existing workflow structure because it already provides strong continuity through `current-handover-prompt.md`, `update-handover-prompt.md`, project docs, and `ai-context.md`, while adding a small harness that automates sequencing and catches stuck handovers.

## Goals

- Automate the exact existing task loop without changing the workflow philosophy.
- Keep handover logic in Markdown prompt files controlled by the user.
- Keep task progression and documentation updates inside the agent prompts and native `cyclopsctl tasks` workflow, not inside the cyclopsctl.
- Support running a limited number of task cycles instead of requiring a full-project run.
- Route models by complexity scores from `.cyclopsctl/reports/complexity-report.json` (Composer 2.5 vs Opus 4.8).
- Integrate with the native task queue via **`cyclopsctl tasks` CLI only**.
- Verify that `current-handover-prompt.md` advances after each update phase.

## Non-Goals

- Parsing or mutating task state during runs (read-only use of the complexity report is allowed).
- Choosing or rewriting the next task in handover files (the update agent owns that).
- Editing or regenerating `update-handover-prompt.md`.
- Routing or tracking individual **subtasks** (work is scoped to parent tasks).
- Summarizing project knowledge on its own.
- Building a general-purpose autonomous coding platform.
- Calling external task MCP tools from the cyclopsctl process.

## Users

The primary user is an advanced Cursor-based developer running a file-based handover workflow with a native task queue, AI context files, and sequential task execution. The user wants to stay inside Cursor, retain direct control over prompts and project rules, and reduce manual glue work between task runs.

## Workflow Context

Before the cyclopsctl is used, the user completes the project planning phase:

1. Write the PRD (`prd.md`).
2. Run `cyclopsctl init` and `cyclopsctl bootstrap` (or `cyclopsctl tasks parse-prd` + `analyze-complexity`) to populate `.cyclopsctl/tasks/`.
3. Prepare prompt template files, including `update-handover-prompt.md` and `ai-context.md`.
4. Ensure `current-handover-prompt.md` includes a stable **Task ID** marker (see Handover Conventions).

The cyclopsctl begins only after this setup is complete.

## Integration Principles

### Native task queue: CLI only

All task reads from agents use the **`cyclopsctl tasks` CLI** with JSON output where available. Example:

```bash
cyclopsctl tasks next --format json --project-root /path/to/project
```

### Cursor agents: Python SDK

Agent execution uses the **Python `cursor-sdk`** (`cursor_sdk`). Requirements:

- Python 3.10+
- `CURSOR_API_KEY` in the environment
- `cursor-sdk` package

If Opus models or required presets are not returned by `Cursor.models.list()` for the account, the cyclopsctl **falls back to Composer 2.5** and logs a clear warning.

## Handover Conventions

`current-handover-prompt.md` must contain:

```markdown
# Task ID: 7
```

The update handover prompt requires the agent to update this line when preparing the next cycle's handover. One cyclopsctl cycle targets a **parent task** as a whole.

## Product Behavior

### Core Loop

Each cycle: **implementation phase** → **update phase** → **handover verification**.

**Before each implementation phase:**

1. Resolve the driving task from handover (`# Task ID:`) or configured task selection mode.
2. Resolve complexity for that task's parent ID from the complexity report.
3. Select the Cursor model for this cycle.

**Cycle 1 (when handover is ready):** send `current-handover-prompt.md` (+ injected `ai-context.md`), not bootstrap setup.

**Later cycles:** fresh agent per implementation; same agent for update.

The update prompt owns marking tasks done, selecting the next task, updating docs, updating `ai-context.md`, and rewriting `current-handover-prompt.md`.

### Handover Verification

After each update phase, compare pre/post snapshots of `current-handover-prompt.md` (Task ID + content hash). Fail the run when the handover did not advance meaningfully.

### Model Routing

Complexity scores come from `.cyclopsctl/reports/complexity-report.json` (`complexityScore` per parent `taskId`, scale **1–10**).

| Complexity score | Model |
|------------------|--------|
| 1–8 | `composer-2.5` |
| 9–10 | Opus 4.8 high-thinking (not Max) |

## Functional Requirements

### Required Inputs

- Number of task cycles to run.
- Project root (absolute path) for Cursor `local.cwd` and task CLI.
- Paths to `current-handover-prompt.md`, `update-handover-prompt.md`, and optional `ai-context.md`.
- First-run bootstrap prompt (greenfield only).
- Path to complexity report (default: `.cyclopsctl/reports/complexity-report.json`).
- Optional task tag (`--tag`).

### Session Management

- New `Agent` for each implementation phase.
- Same agent handle for that cycle's update phase.
- Log `agent_id` and run ids after each `send`.

## CLI Interface

```bash
cyclopsctl launch
cyclopsctl run --cycles 5 --project-root /path/to/repo
cyclopsctl init
cyclopsctl bootstrap --from-prd prd.md
cyclopsctl tasks list
cyclopsctl tasks list pending
cyclopsctl tasks list done
cyclopsctl doctor
```

## Error Handling

- Missing prompt files → stop immediately.
- Cursor startup failure → exit code 1.
- Run failure → exit code 2; do not start the next cycle.
- Handover verification failure → stop; log before/after snapshots.
- Opus unavailable → warn; use Composer for affected cycles.

## Success Criteria

The first release is successful if it can:

- Run a configured number of **verified** cycles without manual prompt copy-paste.
- Preserve the two-phase workflow (implement → update) on the same agent per cycle.
- Use native task storage and complexity-based model routing.
- Detect a stuck handover and stop instead of looping blindly.
- Keep update logic inside the user's update handover template.

## MVP Scope

**In scope:**

- Python CLI (`cyclopsctl`, `cyclopsctl run`, `cyclopsctl init`, `cyclopsctl bootstrap`, `cyclopsctl tasks`, `cyclopsctl doctor`).
- `cursor-sdk` runner (local `cwd` = project root).
- Native task queue under `.cyclopsctl/tasks/`.
- Complexity report read + routing.
- Handover snapshot + verification after update.
- Cycle counting and structured logging.

**Out of scope:**

- External task system integrations.
- Dashboards, web UI, daemons.
- Subtask-level routing.
- Automatic mid-cycle agent resume after crash.
