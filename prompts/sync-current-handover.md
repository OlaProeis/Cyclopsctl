Act as an expert AI development assistant. Regenerate **`current-handover-prompt.md`** from the **native task queue** after tasks exist.

**Prerequisites (verify first — CLI only, no MCP):**

1. `.cyclopsctl/tasks/tasks.json` exists.
2. `cyclopsctl tasks next --format json --project-root <absolute-project-root>` returns a valid next task.
3. Read `prd.md`, `ai-context.md`, and the existing `current-handover-prompt.md` Environment block (keep project root and stack consistent).

If `next` returns no task, output a **completion** handover with `# Task ID: 0` (see `update-handover-prompt.md`) and stop.

**Do not** invent tasks from the PRD. All task fields must come from:

```bash
cyclopsctl tasks next --format json --project-root <root>
cyclopsctl tasks show <id> --format json --project-root <root>
```

Complexity from `.cyclopsctl/reports/complexity-report.json` when present (else from show output).

**Prefer automation:** `cyclopsctl bootstrap --sync-handover-only --project-root <root>` performs the same sync in Python. Use this prompt only when the CLI is unavailable or the operator asked for a manual regen.

---

## Output

Write the **complete** `current-handover-prompt.md` with this **exact section order**:

1. `# Session Handover`
2. `# Task ID: <parent-id>` — must match the task from `next` / `show`
3. `## Environment` (project, project root, tech stack, ai-context, branch, tasks CLI with `--project-root`)
4. `## Core Handover Rules` (NO HISTORY, SCOPE, IMPLEMENTATION ONLY)
5. `## Implementation Phase — Do Only This` (mirror `ai-context.md` — test command, Context7 for library docs, no docs/status/handover edits)
6. `## Current Task: <id> — <title>`
7. Task Details table, Description, Implementation Details, Test Strategy — **from `cyclopsctl tasks show` only**
8. `## Verification` (build/test command from PRD / ai-context)
9. `## Model Selection` (informational; 1–8 Composer 2.5, 9–10 Opus high-thinking; cyclopsctl selects runtime model)

Remove any `## Status: Task queue not loaded` section from the previous placeholder.

**Do not** edit `update-handover-prompt.md` or `ai-context.md` unless the user explicitly asked.

After the file, print: *Ensure `.cyclopsctl/reports/complexity-report.json` exists (run `cyclopsctl bootstrap` if needed), then start implementation with this handover.*
