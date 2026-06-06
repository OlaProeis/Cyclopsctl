# PRD → Tasks: Splitting Quality Assessment & Proposed Prompts

**Scope (narrowed):** Only the **PRD → task list** step. How we split a PRD into parent tasks, and the quality/richness of each task. Plus one analyze fix that directly affects this goal.
**Out of scope:** building/implementation phase, model routing, the two-pass architecture, `max_tasks` plumbing.
**Goal:** more tasks rather than fewer, genuinely **lower complexity per task**, and **richer, more actionable info** in every task.
**Status:** Implemented (June 2026). The prompt drafts below were shipped to
`parse-prd-prompt.md` and `analyze-complexity-prompt.md`; the subtask concept was
also removed entirely (tasks are flat). The live template files are the source of
truth — the drafts here are kept for design rationale.

---

## 1. The lever is the prompt — and three things are missing from it

The current `parse-prd-prompt.md` is structurally fine but under-specifies the three things that actually control our goal:

1. **It anchors on counts, not on a unit-of-work definition.** "3–8 / 8–15 / 15–30" tells the model to hit a number. It never defines *what one task is* or *how to decide where one task ends and the next begins*. So the model consolidates.
2. **It gives zero decomposition strategy.** There is no guidance on *how* to split (by module? by endpoint? by data model? by vertical slice?). Without split heuristics, the model defaults to a few broad "implement X subsystem" epics — the opposite of what we want.
3. **It does not specify what makes `details`/`testStrategy` good.** "implementation notes" and "how to verify" are vague, so the model writes vague tasks — which both starve the implementation agent *and* read as high-complexity to the analyzer.

The fix is a prompt that (a) defines a task as one small cohesive implementable unit, (b) gives explicit split heuristics, (c) biases toward splitting, and (d) demands concrete content in every field. Full draft in §3.

---

## 2. Is analyze broken? One real defect, one non-issue

**Real defect — scores are not calibrated across batches.** `analyze.py` sends tasks in isolated chunks once the list exceeds `DEFAULT_BATCH_THRESHOLD = 15` (`DEFAULT_CHUNK_SIZE = 15`). The prompt only says "1 (trivial) to 10 (very high)" with no concrete rubric. So each chunk scores **relative to the tasks it can see**, not against a fixed scale. The moment we succeed at our goal (40–50 tasks → 3+ chunks), scoring becomes inconsistent: a task scored 6 in chunk 1 is not comparable to a 6 in chunk 3. This undermines "keep complexity low" because "low" stops meaning the same thing.

**Fix (prompt-only, no logic change):** give the analyze prompt an **absolute, anchored rubric** — concrete descriptions of what a 1–2, 3–4, 5–6, 7–8, 9–10 task looks like, with examples. With a fixed rubric, every chunk scores against the same yardstick and batch boundaries stop mattering. Full draft in §4.

**Non-issue — `testStrategy` is not sent to analyze.** `task_metadata_for_prompt` sends only `id/title/description/details`. That's acceptable: complexity is about *building*, not *verifying*. Leave it. (Sending it would add noise and tokens for little signal.)

> Note: chunking itself is fine and worth keeping — it's what makes large lists reliable. The bug is the missing rubric, not the batching.

---

## 3. Proposed parse-prd prompt (full draft)

Ready to drop into `src/cyclopsctl/templates/parse-prd-prompt.md` (keeps `{{PRD_CONTENT}}`; drops the `{{MAX_TASKS_SUFFIX}}` count-anchor — see note after).

```markdown
# PRD to Task List

You are a senior software architect breaking a Product Requirements Document into a
task list for an AI implementation workflow. Each task will be implemented by an
agent in a single focused session, one task at a time. Your decomposition quality
directly determines whether each task succeeds.

## What ONE task is

A single task is the smallest cohesive unit of work that:
- can be implemented and tested in one focused session,
- produces a working, verifiable increment (not a half-feature),
- touches a bounded set of files/modules (roughly one component or slice),
- can be completed WITHOUT starting the next task.

Think in terms of implementation complexity on a 1–10 scale (1 = trivial change,
10 = a whole subsystem). **Size every task so it would score about 3–6.** If a task
would plausibly score 7 or higher, or would touch many unrelated areas, SPLIT IT.

## How to split (decomposition strategy)

Prefer MORE small tasks over fewer large ones. When in doubt, split. Apply these
split lines, in roughly this order:

1. **Foundation before features** — scaffolding, config, schema, shared types, and
   project setup are their own early tasks that later tasks depend on.
2. **One surface per task** — one data model/table, one API endpoint or route group,
   one CLI command, one UI view/screen, one integration with an external system,
   one background job. Do not bundle several of these into one task.
3. **Separate wiring from logic** — e.g. "define the data model" vs "implement the
   service that uses it" vs "expose it via the endpoint" can be separate tasks when
   each is non-trivial.
4. **Split large features into vertical slices** — a thin end-to-end slice first,
   then additional slices, rather than one giant "implement feature X" task.
5. **Carve out substantial cross-cutting work** — auth, error handling, validation,
   migrations, observability, and the test suite become their own tasks when they
   are sizeable; do not silently fold them into a feature task.

Do NOT create busywork or artificial tasks. Every task must map to real PRD scope.
A large, advanced PRD will naturally produce many tasks (often 30–60+). Let real
scope drive the count — there is no target number and no maximum.

## Required content per task — be concrete, not vague

Each task must include ALL fields below. Vague tasks fail; specific tasks succeed.

- `id` (integer) — sequential starting at 1.
- `title` (string) — imperative and specific; name the component (e.g.
  "Add /auth/login endpoint with JWT issuance", not "Backend work").
- `description` (string, one paragraph) — what this task delivers and why.
- `details` (string) — the implementation brief. MUST include, as best inferred
  from the PRD:
  - target files / modules / paths to create or change,
  - the concrete approach (key functions, classes, data shapes, libraries),
  - patterns/conventions to follow and constraints to respect,
  - explicitly what is OUT of scope for this task (to stop scope bleed).
  Write enough that an implementer does not have to re-design the architecture,
  but do not paste the PRD verbatim.
- `testStrategy` (string) — concrete, verifiable checks: specific test
  files/commands to run, what to assert, and any manual verification steps.
  Never "make sure it works."
- `priority` (one of: `high`, `medium`, `low`).
- `dependencies` (array of task id strings that must finish first; `[]` if none).
  Reflect real ordering; no cycles; foundation tasks come first.
- `status` (always `"pending"`).

(There is no subtask concept — every task is a flat, self-contained unit.)

## Coverage

Cover the full PRD with no important requirement left out and no duplicated work
across tasks. Every PRD requirement should be traceable to at least one task.

## Before you answer — self-check

Re-read your task list and revise until ALL are true:
- No task bundles multiple endpoints/models/screens/integrations.
- No task would plausibly score 7+ in implementation complexity.
- Every `details` names concrete files/modules and an approach.
- Every `testStrategy` is concretely verifiable.
- Dependencies are acyclic and foundation-first.

## Output

Return ONLY valid JSON — no markdown fences, commentary, or prose. A JSON object
with a single `tasks` array:

{
  "tasks": [
    {
      "id": 1,
      "title": "Imperative, component-specific title",
      "description": "What this task delivers and why.",
      "details": "Files/modules to touch, approach, patterns, out-of-scope.",
      "testStrategy": "Specific commands/assertions to verify.",
      "priority": "high",
      "dependencies": [],
      "status": "pending"
    }
  ]
}

## Example of good decomposition (split a monolith)

BAD (one monolithic task):
  "Implement user authentication" — covers schema, password hashing, login,
  logout, session middleware, and tests. Too large; would score ~9.

GOOD (split into cohesive ~3–5 tasks):
  1. "Add users table and User model with migration"
  2. "Implement password hashing and verification utilities"
  3. "Add POST /auth/login endpoint issuing a session token"
  4. "Add session-validation middleware for protected routes"
  5. "Add auth integration tests for login + protected-route access"

## Product Requirements Document

{{PRD_CONTENT}}
```

### Why this draft hits the goal

- **More tasks:** the unit-of-work definition + explicit "prefer more / when in doubt split" + split heuristics directly push count up without inventing busywork.
- **Lower complexity:** "size every task to score ~3–6, split anything 7+" sets the complexity target the analyzer later measures — and the split heuristics make it achievable, not a hollow instruction.
- **Richer info:** `details`/`testStrategy` now have concrete content requirements (files, approach, out-of-scope, real test commands), and the self-check forces a revision pass.
- **No count anchor / no cap:** the numeric ranges and the `{{MAX_TASKS_SUFFIX}}` are gone, replaced by scope-driven emergence — matching the "no fixed count, no maximum" stance.
- **Few-shot anchor:** the monolith→split example teaches granularity far better than prose.

> If you keep the `max_tasks` config field for now, leave it unused by the prompt (don't reintroduce a "do not exceed N" line). The renderer can simply ignore it.

---

## 4. Proposed analyze prompt rubric (full draft)

Add an absolute rubric so chunked scoring is consistent. Drop-in for the Requirements
section of `src/cyclopsctl/templates/analyze-complexity-prompt.md` (keeps
`{{TASKS_JSON}}`):

```markdown
## Requirements

1. Return ONLY valid JSON — no markdown fences, commentary, or prose.
2. Analyze EVERY task in the input list; do not skip or invent tasks.
3. `complexityScore` is an integer 1–10, scored against this ABSOLUTE rubric
   (score by the task's own scope, NOT relative to the other tasks in this list):
   - 1–2  Trivial: a single small file/function; config or copy change; no design.
   - 3–4  Small: one cohesive component (one model, one endpoint, one command);
          clear approach; few files; little integration.
   - 5–6  Moderate: a component with real logic or one integration point;
          several files; some edge cases to handle.
   - 7–8  Large: multiple integration points or cross-cutting concerns; many files;
          notable design decisions. (Signals the task should have been scoped
          smaller at parse time.)
   - 9–10 Very high: spans a whole subsystem or many features; far too large for a
          single implementation cycle.
4. `reasoning` — one or two sentences explaining the score against the rubric.
```

### Why this fixes the calibration defect

- Every chunk now scores against the **same fixed yardstick**, so 40–50-task lists (which span 3+ chunks) get consistent scores regardless of batch boundaries.
- The rubric's "prefer a split at 7+" reinforces the parse prompt's "split at 7+" — parse and analyze finally share one definition of "too big."

---

## 5. Optional, low-effort knobs (only if needed after measuring)

- **Raise `DEFAULT_BATCH_THRESHOLD`** so typical lists stay in one analyze call (fewer cross-batch concerns). Only helps until JSON/context limits bite; the rubric in §4 is the more robust fix and should come first.
- **Content floor in validation** (reject empty `details`/`testStrategy`, duplicate titles) — a small safety net so a bad parse can't silently ship empty tasks. Warn-and-accept after one repair; never hard-fail. (Mentioned only; not required to meet the goal if the prompt does its job.)

---

## 6. Summary

The PRD→Tasks problem is a prompt problem, as suspected. The current parse prompt anchors on task *counts* and never tells the model what a task is or how to split one — so it consolidates into a few large tasks with thin `details`. Replace it with the §3 draft: define a task as one small cohesive testable unit, give explicit split heuristics, bias toward splitting, set an explicit complexity target (~3–6, split at 7+), and demand concrete `details`/`testStrategy` with a self-check pass and a worked split example. Separately, analyze has one real defect for large lists — chunked scoring with no fixed rubric makes scores incomparable across batches; fix it with the §4 absolute rubric so "low complexity" means the same thing everywhere. Both fixes are prompt-only and need no building-phase changes.
