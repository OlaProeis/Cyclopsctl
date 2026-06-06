# Session Handover

# Task ID: 0

## Environment
- **Project:** {{PROJECT_NAME}}
- **Project root:** `{{PROJECT_ROOT}}`
- **Tech stack:** {{TECH_STACK}}
- **Context file:** Cyclopsctl prepends `ai-context.md` automatically — follow its implementation rules.
- **Branch:** `{{BRANCH}}`
- **Tasks CLI:** `cyclopsctl tasks ... --project-root {{PROJECT_ROOT}}`

## Status: Task queue not loaded

The native task queue has no tasks yet. **Do not implement product work from this file.**

### Required human steps (in order)
1. Parse the PRD into tasks and sync handover:
   `cyclopsctl bootstrap --project-root {{PROJECT_ROOT}}`
2. If tasks already exist but this file is stale, regenerate handover:
   `cyclopsctl bootstrap --sync-handover-only --project-root {{PROJECT_ROOT}}`
   or use the prompt in `prompts/sync-current-handover.md`.

### Agent rules while Task ID is 0
- **Do not** treat the PRD as a substitute task list.
- **Do not** implement features unless the user explicitly asks in chat.
- You may help with bootstrap, native task config, or editing workflow files if asked.

## Core Handover Rules
- **NO HISTORY:** This file describes only the current task.
- **SCOPE:** No task is loaded — do not start implementation from this file.
- **IMPLEMENTATION ONLY:** Do not edit docs, `ai-context.md`, or this handover during implementation.

## Implementation Phase — Do Only This
- Nothing to implement until Task ID is non-zero.
- **Do not** mark tasks done, run `cyclopsctl tasks next`, rewrite this file, or edit `update-handover-prompt.md`.
