> **Cyclopsctl guard:** This file is **`first_prompt` bootstrap only**. The cyclopsctl sends it only when `current-handover-prompt.md` is missing or the operator passes `--fresh`. **Normal task cycles always use `current-handover-prompt.md` for implementation** — including cycle 1 on a cyclopsctl-ready repo. If you received this prompt while implementing a numbered task (`# Task ID: N`), startup resolution is wrong; see `docs/runtime/run-history.md`.

Act as an expert AI development assistant. I am setting up a new software project and need to establish my AI development workflow for **native cyclopsctl tasks + file-based handovers**, driven by a **Cyclopsctl** (implement → update → verify cycles).

---

## Step 0 — Detect task queue (mandatory)

Read `prd.md` for product name, tech stack, architecture, test commands, and cyclopsctl usage.

**Check whether the native task queue exists** (CLI only — no MCP):

1. Confirm `.cyclopsctl/tasks/tasks.json` exists and contains at least one task, **or**
2. Run `cyclopsctl tasks next --format json --project-root <absolute-project-root>` and confirm the response indicates a **valid next task** (not empty / not an error).

| Result | Mode |
|--------|------|
| **No `tasks.json` or `next` returns no task** | **PRE-TASKS mode** — do **not** put a real task in `current-handover-prompt.md` |
| **Tasks exist and `next` returns a task** | **READY mode** — populate handover from `cyclopsctl tasks` only |

**Critical rule:** Never invent task id `1` (or any id) from the PRD. Task content in `current-handover-prompt.md` must come **only** from `cyclopsctl tasks show` after the native queue exists. The PRD is not a substitute for `cyclopsctl bootstrap`.

If the user has not run `cyclopsctl init` / `cyclopsctl bootstrap` yet, you are in **PRE-TASKS mode**.

---

## Output

Provide the **complete contents** of exactly **four** markdown files. Use concrete values from the PRD where possible (not lorem ipsum).

---

## Global workflow rules (all files)

- **Two phases:** **Implementation** = `current-handover-prompt.md`. **Update** = `update-handover-prompt.md` after implementation in the same session.
- **Task queue:** **`cyclopsctl tasks` CLI only** (`--project-root <root>`). No MCP task tools in handover or update prompts.
- **Parent tasks only:** `# Task ID:` = parent id only (never `1.2`).
- **Cyclopsctl:** Snapshots handover before update; fails if `# Task ID:` and content unchanged. Complexity 1–8 → Composer 2.5; 9–10 → Opus 4.8 high-thinking (not Max). Handover model line is informational.
- **Context7 MCP:** library docs only — not for task queue operations.

| Value | Source |
|-------|--------|
| Project name, stack, architecture | `prd.md` |
| Project root | Absolute repo path |
| Build/test command | PRD |
| Task id, title, body, complexity | **READY mode only:** `cyclopsctl tasks next` + `show` + complexity report |

---

### File 1: `docs/index.md`

**Purpose:** Documentation map only.

**Constraints:** Index only — no history, task lists, or architecture essays. Include **index rules** at the top.

```markdown
# Documentation Index

> **Index rules:** This file is a documentation map only. Do not add project history, task lists, architecture overviews, or session notes. When adding docs, append a single bullet with a one-line description under the appropriate section.

## Core Context
- `ai-context.md` - [one line]

## Technical Docs
*(Feature docs added here as built)*
```

---

### File 2: `ai-context.md`

**Purpose:** Rules + architecture (~100 lines). **No current task content.**

**Required sections:** Rules (DO NOT UPDATE) → Implementation Phase Rules → Update Phase Rules → Handover Files table → Tech Stack → Architecture & Data Model → Conventions → Where Things Live.

**Required rules (Rules section):** test command from PRD, follow patterns, **Context7 MCP** for library docs (resolve ID first), **`cyclopsctl tasks` CLI only**.

Include in **Where Things Live**:

- `prd.md`, handover files, `.cyclopsctl/tasks/tasks.json`, `.cyclopsctl/reports/complexity-report.json`
- `prompts/setup-ai-workflow.md` — initial workflow bootstrap
- `prompts/sync-current-handover.md` — manual handover regen when CLI bootstrap is not used

---

### File 3: `current-handover-prompt.md`

Generate **one of two variants** based on Step 0. **Do not merge variants.**

#### PRE-TASKS mode (no native queue / no next task)

Placeholder only. **No `## Current Task:` section with implementation work.**

```markdown
# Session Handover

# Task ID: 0

## Environment
[same Environment block as READY mode — project, root, stack, ai-context, branch, tasks CLI]

## Status: Task queue not loaded

The native task queue has no tasks yet. **Do not implement product work from this file.**

### Required human steps (in order)
1. Parse the PRD and sync handover:
   `cyclopsctl bootstrap --from-prd prd.md --project-root <project-root>`
2. (Recommended) ensure complexity report exists (bootstrap runs analyze by default)
3. Regenerate this file if needed:
   `cyclopsctl bootstrap --sync-handover-only --project-root <project-root>`
   or run the prompt in `prompts/sync-current-handover.md`.

### Agent rules while Task ID is 0
- **Do not** treat the PRD as a substitute task list.
- **Do not** implement features or scaffold the product unless the user explicitly asks in chat (outside this handover).
- You may help with bootstrap, native task config, or editing workflow files if asked.

## Core Handover Rules
[NO HISTORY, SCOPE — keep brief]

## Implementation Phase — Do Only This
[Standard DO NOT list from ai-context — nothing to implement until Task ID is non-zero]
```

#### READY mode (tasks exist)

Full implementation handover. Populate **only** from `cyclopsctl tasks show <id> --format json` and complexity report — **not** from PRD task guessing.

Use the exact heading order documented in `prompts/sync-current-handover.md` (Session Handover → Task ID → Environment → Core rules → Implementation Phase → Current Task → Verification → Model Selection).

---

### File 4: `update-handover-prompt.md`

Same as the project’s standard update template:

- Update phase only; CLI only; `cyclopsctl tasks set-status`, `list pending`, `show` with `--project-root`
- Docs + `docs/index.md`; minimal docs for scaffold work
- Rewrite `current-handover-prompt.md` only here; must change `# Task ID:`
- Pick next task by **lowest numeric pending parent id** — not `cyclopsctl tasks next` priority ordering
- If no next task → `# Task ID: 0` completion stub
- Do not edit `update-handover-prompt.md`
- Final checklist + cyclopsctl verification note if applicable

(Copy structure and tone from the repo’s canonical `update-handover-prompt.md` if it already exists; otherwise generate from the rules above.)

---

## Final delivery — human checklist

Print this after the four files:

**Always**
- [ ] `ai-context.md` and `update-handover-prompt.md` saved
- [ ] `docs/index.md` saved

**If PRE-TASKS mode**
- [ ] `current-handover-prompt.md` has `# Task ID: 0` and **no** fabricated task 1
- [ ] Run `cyclopsctl bootstrap --from-prd prd.md --project-root <root>`
- [ ] Then run `cyclopsctl bootstrap --sync-handover-only` or `prompts/sync-current-handover.md` for the first real handover

**If READY mode**
- [ ] `current-handover-prompt.md` task body matches `cyclopsctl tasks show` for the id in `# Task ID:`
- [ ] Complexity report exists under `.cyclopsctl/reports/` before long orchestrated batches

**Never**
- [ ] Do not start the cyclopsctl or implementation handover with `Task ID: 0` unless intentionally idle

Do not create other files unless the PRD requires them.
