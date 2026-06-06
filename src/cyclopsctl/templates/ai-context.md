# AI Context

## Rules (DO NOT UPDATE)
- **Implementation sessions:** follow **Implementation Phase Rules** below only.
- **Update sessions:** follow **Update Phase Rules** below only when you receive the update handover prompt.
- Only do the task specified; do not start the next task or go over scope.
- Run your project test command before finishing.
- Follow existing code patterns and conventions.
- Use Context7 MCP to fetch library documentation when needed (resolve library ID first, then fetch docs). Task operations use **`cyclopsctl tasks` CLI only**.

## Implementation Phase Rules
- Implement and test only the current parent task in `current-handover-prompt.md`.
- Use Context7 MCP for up-to-date library documentation when implementing unfamiliar APIs or frameworks.
- Do not mark tasks done, rewrite handover files, or edit docs during implementation.

## Update Phase Rules
- Follow every step in `update-handover-prompt.md` after implementation.
- Rewrite `current-handover-prompt.md` for the next task only in the update phase.
- Use `cyclopsctl tasks list` (all tasks table), `list pending`, `show`, and `set-status` with `--project-root` from the handover Environment section.
- Document by feature (e.g., `auth-layer.md`), not by task number; update `docs/index.md` when adding documentation.

## Conventions
- **Documentation:** Feature-based names in `docs/`, not `task-1.md`. Update `docs/index.md` in the update phase only.
- **Tasks:** `cyclopsctl tasks` CLI only from agents.

## Handover Files
| File | Who may edit | When |
|------|----------------|------|
| `current-handover-prompt.md` | Update-phase agent only | After implementation |
| `update-handover-prompt.md` | Human / template only | Never edited by agents |
| `ai-context.md` | Update-phase agent only | Every update phase — project memory bullets (see update handover step 2) |

## Where Things Live
| Want to... | Look in... |
|------------|------------|
| Product requirements | `prd.md` |
| Current implementation handover | `current-handover-prompt.md` |
| Post-task update rules | `update-handover-prompt.md` |
| Tasks and complexity | `.cyclopsctl/tasks/tasks.json`, `.cyclopsctl/reports/complexity-report.json` |
