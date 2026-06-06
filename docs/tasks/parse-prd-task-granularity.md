# PRD → Task List Granularity: Problem Statement & Current Implementation

**Audience:** Opus (or any reviewer) evaluating how CursorOrchestrator should decide task count, granularity, and information quality when parsing a PRD.

**Status:** Historical problem statement. The granularity/quality work has since
been **implemented** — see `docs/tasks/parse-prd-granularity-assessment.md` for the
shipped approach. Notably: the prompt now drives granularity from a complexity
target (not count ranges), the analyze prompt uses an absolute rubric, and the
**subtask concept was removed entirely** (tasks are flat). Descriptions of "current
implementation" below reflect the pre-change state and are kept for context.

---

## Why this matters

Task generation is arguably **the most important step** in this application. Everything downstream depends on it:

1. **Handover quality** — Each run cycle gives the implementation agent one parent task via `current-handover-prompt.md`, built from `title`, `description`, `details`, and `testStrategy`.
2. **Model routing** — After parse, `analyze-complexity` scores each parent task 1–10. Scores ≥ 9 route to Opus; 1–8 route to Composer (by default).
3. **Scope control** — Agents are instructed to implement **one parent task only** per cycle. If a task is too large, the agent either fails, drifts, or smuggles future work into the current cycle.
4. **Cost** — Smaller, well-scoped tasks tend to score lower complexity → more cycles run on Composer (subscription) instead of Opus (expensive).

**Hypothesis (product owner):** For advanced PRDs, **more tasks is better** — e.g. 40–50 parent tasks — because each unit of work stays small, complexity scores stay low, and cheaper models can execute reliably. There should be **no artificial cap** on task count; the model should scale with PRD scope.

**Counter-tension:** Too many tasks creates overhead (more cycles, more handover churn, dependency management noise). Too few tasks creates monolithic work units that need Opus or fail. The right granularity is a **fine line** and hard to encode in a single prompt.

---

## The design problem (in plain terms)

We need the parse-PRD step to answer three coupled questions at once:

| Question | Too little | Too much |
|----------|------------|----------|
| **How many parent tasks?** | 5 huge epics; agent overwhelmed; complexity 8–10 | 80 micro-tasks; cycle overhead; dependency graph spaghetti |
| **How much goes in each task?** | Vague titles; agent improvises architecture | Over-specified; brittle; duplicates PRD verbatim |
| **What fields must be rich?** | `details` empty → agent guesses file paths and patterns | `details` novel-length → context bloat; agent skips reading |

### Desired properties of a good task list

- **One implementable unit per parent task** — Completable in a single cyclopsctl cycle without starting the next task.
- **Cohesive scope** — Not a random slice of a file; not an entire subsystem unless unavoidable.
- **Actionable `details`** — File paths, modules, patterns, constraints, and approach — enough that Composer can execute without re-planning the architecture.
- **Verifiable `testStrategy`** — Concrete checks (pytest targets, CLI commands, manual steps), not "make sure it works."
- **Correct dependencies** — Real ordering without cycles; no accidental forward-deps.
- **Full PRD coverage** — Nothing important left out; no duplicate work across tasks.
- **Complexity-aligned sizing** — Tasks sized so most land in Composer territory (complexity 1–8) after analysis, not because we lie in the prompt but because scope is genuinely small.

### Product owner stance (June 2026)

- **No fixed task count** (we removed `default_num_tasks = 10` for this reason).
- **No max cap either** — `max_tasks` exists in code today as an optional ceiling; product direction is to **remove or stop using it**. A sophisticated PRD should be allowed to produce 40–50+ tasks if that reflects real scope.
- **Prompt guidance ranges may be wrong** — Current template suggests "15–30 for a large product"; that may under-shoot for advanced PRDs.
- **Quality over count** — Count is a means to an end; bad tasks at any count are worse than good tasks at any count.

---

## How tasks flow through the system today

```
prd.md
  │
  ▼
parse-prd (Cursor SDK, single agent call)
  │  prompt: templates/parse-prd-prompt.md
  │  output: JSON { "tasks": [ ... ] }
  ▼
validate (sequential ids 1..N, required fields, allowed statuses)
  ▼
.cyclopsctl/tasks/tasks.json  (per-tag queue)
  │
  ▼
analyze-complexity (Cursor SDK, may batch large lists)
  │  prompt: templates/analyze-complexity-prompt.md
  │  output: complexity-report.json (scores 1–10 per task)
  ▼
sync handover → current-handover-prompt.md (task 1 first)
  │
  ▼
cyclopsctl run cycles
  │  pick task (handover or sequential)
  │  route model: complexity 1–8 → Composer, 9–10 → Opus
  │  agent implements ONE parent task
  ▼
update handover → next task
```

**Key observation:** Parse produces **parent tasks only** (`subtasks` is always `[]` in the prompt). Complexity analysis may *recommend* subtasks (`recommendedSubtasks`, `expansionPrompt`) but **native cyclopsctl does not auto-expand** parent tasks into subtasks before running. The run loop operates on **parent task IDs**.

So parent-task granularity at parse time is the primary lever — not subtask expansion.

---

## Current implementation (code & prompts)

### Entry points

| Surface | Module |
|---------|--------|
| `cyclopsctl init` (bootstrap) | `project_setup.py` → `NativeTaskBackend.parse_prd()` |
| `cyclopsctl bootstrap` | `bootstrap.py` |
| `cyclopsctl tasks parse-prd` | `tasks/cli.py` → native backend |
| Core logic | `tasks/parse_prd.py` → `parse_prd_with_cursor()` |
| Prompt template | `templates/parse-prd-prompt.md` |

### Parse prompt (current)

File: `src/cyclopsctl/templates/parse-prd-prompt.md`

Relevant instruction (requirement #2):

> Produce an **appropriate number** of parent tasks (top-level items only; no subtasks) based on PRD scope and complexity. Use your judgment — break work into cohesive, implementable units (not micro-tasks, not monolithic epics). Typical ranges: about **3–8** for a focused prototype, **8–15** for a medium feature set, **15–30** for a large product. Let the PRD drive the count.{{MAX_TASKS_SUFFIX}}

When `max_tasks` is configured, the suffix becomes:

> Do not produce more than **{N}** parent tasks.

**Fields required per task:** `id`, `title`, `description`, `details`, `testStrategy`, `priority`, `dependencies`, `status` (always `"pending"`), `subtasks` (always `[]`).

**Not required / not validated strongly:**

- Minimum length or quality of `details` / `testStrategy`
- Maximum task count (unless `max_tasks` set)
- Alignment between task size and expected complexity
- Coverage checklist against PRD sections

### Validation (current)

File: `src/cyclopsctl/tasks/parse_prd.py` — `validate_parsed_tasks()`

What we **do** enforce:

- Non-empty task list
- Sequential integer ids starting at 1 (1, 2, 3, …, N)
- `title` non-empty string
- `status` in allowed set (`pending`, `in-progress`, `done`, etc.)
- `dependencies` is a list
- JSON shape survives repair retry (one retry with `render_repair_prompt()`)

What we **do not** enforce:

- Task count range (min or max), except optional `max_tasks` in prompt
- Richness of `details` or `testStrategy` (empty strings are accepted)
- Duplicate titles or overlapping scope
- PRD section coverage
- Dependency sanity beyond type checking

### Configuration (current)

| Setting | Default | Location |
|---------|---------|----------|
| `parse_model` | `"auto"` | `[tasks]` in `cyclopsctl.toml` |
| `analyze_model` | `"auto"` | same |
| `max_tasks` | `None` (unset) | same; CLI `--max-tasks` on init |

**History:** We previously had `default_num_tasks = 10`, and the prompt said "Produce exactly **10** tasks." That caused the model to narrate "the user wants exactly 10 tasks" — a fixed count with no PRD reasoning. That was removed.

**Alias:** `default_num_tasks` in older TOML files is still read as `max_tasks` (cap only).

**Example TOML** (`cyclopsctl.toml.example`): `[tasks]` no longer sets a default count; `max_tasks` is documented as optional.

### Model used for parsing

File: `tasks/models.py`, `tasks/bootstrap_model.py`

- Init prompts for bootstrap model: `auto` (Sonnet → Composer fallback), `composer`, or `sonnet`.
- `auto` on local SDK runs effectively prefers **Composer** for reliability.
- Parse is a **single agent call** with the full PRD in context — no chunking, no multi-pass decomposition.

**Implication:** Very large PRDs + very large task lists hit context limits and JSON reliability limits in one shot.

### Complexity analysis (downstream)

File: `templates/analyze-complexity-prompt.md`

- Scores each **parent** task 1–10.
- Emits `recommendedSubtasks` and `expansionPrompt` per task — **informational only** today; native run loop does not expand before implementation.
- Batches tasks if list is large (`analyze.py` chunking).

### Model routing (downstream)

File: `routing.py`

- Default: complexity **1–8** → Composer standard; **9–10** → Opus high-thinking (if available and enabled).
- Threshold constant: `OPUS_COMPLEXITY_THRESHOLD = 9`.
- Handover displays complexity and informational model hint (`prompt.py` → `format_model_selection()`).

**Implication:** If parse produces 8 monolithic tasks each scoring 9–10, most work runs on Opus. If parse produces 45 small tasks each scoring 3–5, most work runs on Composer — **assuming** analysis scores reflect true scope.

---

## Known tensions & failure modes

### 1. Prompt ranges may anchor the model too low

"15–30 for a large product" may cause a 40-section enterprise PRD to compress into ~20 tasks. The model treats ranges as soft targets even when told "let the PRD drive the count."

### 2. Single-shot JSON generation

Producing 50 high-quality tasks with full `details` in one JSON response is:

- Hard for the model to keep consistent quality across all items
- Prone to JSON truncation / validation failures
- Context-heavy when PRD is long

We only retry **once** on parse/validation failure (repair prompt); we do not retry with "generate fewer tasks" or multi-pass refinement.

### 3. No quality gate on content

A task with `"details": ""` passes validation. The handover still renders, but the implementation agent gets little guidance.

### 4. Parent-only tasks vs subtask expansion

Some task planners **expand** complex tasks into subtasks. The cyclopsctl native path:

- Parse: `subtasks: []` always
- Analyze: may recommend subtasks but nothing consumes `recommendedSubtasks` automatically
- Run: one **parent** per cycle

So either parse must get parent granularity right, or we need a first-class expand step — currently absent in native cyclopsctl.

### 5. Complexity analysis is decoupled from parse

Parse decides granularity; analyze scores it after the fact. There is **no feedback loop** such as:

- "If any task scores ≥ 8, re-parse or split those tasks"
- "If average complexity > 6, ask parse agent to subdivide"

### 6. `max_tasks` cap contradicts product direction

Optional cap remains in code (`ParsePrdConfig.max_tasks`, `--max-tasks`). Product owner does not want a cap; advanced PRDs should breathe.

### 7. Opus for parse vs Composer for execution

Parse/bootstrap often runs on Composer (cost, local SDK reliability). Decomposition quality may differ from Sonnet/Opus. We may be using a cheaper model for the **most important** step.

---

## What "good" might look like (evaluation criteria for proposals)

Any solution Opus proposes should be judged against:

1. **PRD-scale adaptive count** — Small PRD → few tasks; advanced PRD → 40–50+ without artificial caps.
2. **Stable Composer complexity** — Most tasks analyze to 1–7 without dumbing down real work.
3. **Rich, consistent task records** — `details` and `testStrategy` genuinely useful; measurable (e.g. non-empty, min length, mentions files/tests).
4. **Reliable machine output** — JSON validity at scale; strategy for 50+ tasks without single-response collapse.
5. **Coverage** — No major PRD requirement orphaned; optional explicit traceability (section → task ids).
6. **Operational cost** — Parse/analyze cost vs savings from fewer Opus implementation cycles.
7. **Simplicity** — Avoid over-engineering; prefer changes to prompts/pipeline over new infrastructure unless necessary.

---

## Open questions for review

1. **Should we remove `max_tasks` entirely** (config, CLI, prompt suffix) rather than leave it as an escape hatch?

2. **Should prompt ranges be removed or inverted?** e.g. explicit encouragement: "For large/advanced PRDs, prefer **30–60** parent tasks rather than consolidating."

3. **Multi-pass parse?**
   - Pass 1: outline / epic list
   - Pass 2: expand each epic into tasks
   - Pass 3: enrich `details` and `testStrategy` per task

4. **PRD-aware sizing heuristic?** e.g. estimate task count from PRD word count, section count, or explicit feature list — fed into prompt as *guidance* not *mandate*.

5. **Quality validation pass?** Reject or repair tasks with empty `details`, duplicate titles, or complexity analysis predicting ≥ 9 (split and re-parse).

6. **When to use a stronger model for parse?** Sonnet/Opus for decomposition only; Composer for execution — worth the credit cost?

7. **Subtask expansion in native path?** Should `recommendedSubtasks > 0` trigger automatic expand before the task enters the run queue?

8. **Chunked PRD parsing?** For PRDs over N tokens, parse section-by-section and merge with dependency resolution.

9. **Human-in-the-loop?** Show proposed task count and sample tasks before committing `tasks.json` on init.

10. **Golden examples in prompt?** Few-shot examples of "good" vs "bad" task granularity for similar PRDs.

---

## Relevant files (quick reference)

| File | Role |
|------|------|
| `src/cyclopsctl/templates/parse-prd-prompt.md` | Task count & field instructions to the model |
| `src/cyclopsctl/tasks/parse_prd.py` | Render prompt, call agent, validate, persist |
| `src/cyclopsctl/templates/analyze-complexity-prompt.md` | Score tasks 1–10 after parse |
| `src/cyclopsctl/tasks/analyze.py` | Complexity pipeline, batching, backfill |
| `src/cyclopsctl/routing.py` | Complexity → model selection at run time |
| `src/cyclopsctl/prompt.py` | Handover sync from task fields |
| `src/cyclopsctl/tasks/models.py` | `DEFAULT_MAX_TASKS = None`, model resolution |
| `src/cyclopsctl/config.py` | `TasksConfig.max_tasks`, `default_num_tasks` alias |
| `src/cyclopsctl/project_setup.py` | Init bootstrap wires parse → analyze |
| `docs/tasks/parse-prd.md` | Pipeline documentation |
| `docs/tasks/task-models.md` | `[tasks]` config documentation |

---

## Summary for Opus

**The bug we fixed:** Fixed count of 10 tasks was baked into config + prompt. That was clearly wrong.

**The problem we have not fixed:** How to intelligently choose task count and content quality from arbitrary PRDs, especially when **more smaller tasks** is a feature (lower complexity → cheaper models), not a bug — and when **too many** or **too vague** tasks also break the system.

**The fine line:** Parse-PRD must decompose work into units that are small enough for Composer-led implementation cycles, rich enough that agents do not re-architect from scratch, and numerous enough that advanced PRDs do not collapse into a handful of Opus-scale epics — **without** hard-coded numbers or caps.

We need a thoughtful strategy for this step, not just better wording in a single prompt.
