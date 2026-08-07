# Update Handover Instructions

The **implementation work for this cycle is finished**. Do not write more feature code unless fixing a failing test you just ran.

This prompt runs only in the **update phase** (same agent session after implementation).

**Project root for all `cyclopsctl tasks` commands:** use the path from `## Environment` in `current-handover-prompt.md` (`--project-root <path>`). Use **CLI only**.

## 1. Mark current task done

```bash
cyclopsctl tasks set-status --id=<current-task-id> --status=done --project-root <project-root>
```

## 2. Documentation and project memory

Create feature-based documentation for what was implemented, update `docs/index.md`, then update `ai-context.md` as **whole-project** memory (all phases/tags):

- Never rewrite `ai-context.md` from scratch or clear prior-phase facts unless obsolete/wrong.
- Add 1–3 durable bullets; prune duplicates only; soft target ≤ ~1000 lines.
- Never edit Rules (DO NOT UPDATE), Implementation Phase Rules, Update Phase Rules, or Handover Files rules.

## 3. Get next task

Use the **lowest numeric pending parent task id** — not `cyclopsctl tasks next` priority ordering.

```bash
cyclopsctl tasks list pending --format json --project-root <project-root>
cyclopsctl tasks show <next-task-id> --format json --project-root <project-root>
```

If there is no next task, set `# Task ID: 0` in `current-handover-prompt.md` and stop.

## 4. Rewrite `current-handover-prompt.md`

This is the **only** step that may edit `current-handover-prompt.md`. Preserve the standard section order from `prompts/sync-current-handover.md`.

## 5. Final checks

- [ ] `cyclopsctl tasks set-status` succeeded
- [ ] `docs/index.md` updated
- [ ] `ai-context.md` updated with project memory (or explicit “no new memory” line with reason)
- [ ] `current-handover-prompt.md` rewritten with a new `# Task ID:`
- [ ] Project tests pass
