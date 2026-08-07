# AI context injection

The cyclopsctl prepends `ai-context.md` to **implementation** prompts only. Handover files stay unchanged; injection is cyclopsctl-side only. The **update** phase sends `update-handover-prompt.md` without re-attaching ai-context (same agent session already has it).

`ai-context.md` is **whole-project** memory across phases and task tags — not phase-scoped. New-phase PRD change preserves a customized file (workflow generation uses `force_workflow=False`). Update-phase agents must update it additively (see `update-handover-prompt.md` step 2).

## Prompt shape

**Implementation** (`compose_agent_prompt(..., attach_ai_context=True)`):

```
# AI Context

<full ai-context file contents>

---

<current-handover body>
```

**Update** (`attach_ai_context=False`):

```
<update-handover file contents unchanged>
```

Composition lives in `compose_agent_prompt()` (`src/cyclopsctl/prompt.py`). The run loop calls it via `_agent_prompt()` in `loop.py` for implementation; update reads the update handover file directly.

## Which implementation body gets ai-context?

| Cycle | Body | Notes |
|-------|------|-------|
| 1 (handover ready) | `current-handover-prompt.md` | Normal case — **not** `first_prompt` |
| 1 (no handover / `--fresh`) | `first_prompt` (e.g. `prompts/setup-ai-workflow.md`) | Bootstrap only |
| 2+ | `current-handover-prompt.md` | Same as cycle 1 when handover exists |

See `docs/runtime/run-history.md` for startup resolution. **Run history does not gate** whether cycle 1 uses the handover.

## Configuration

| Setting | Flag | Default |
|---------|------|---------|
| AI context path | `--ai-context` | `<project-root>/ai-context.md` |
| Require file | `--require-ai-context` | off (warn once if missing) |
| Max attachment size | `--ai-context-max-chars` | `100000` (truncate with warning) |

TOML keys: `ai_context`, `require_ai_context`, `ai_context_max_chars` (same semantics as CLI).

When the file is missing and not required, the handover body is sent unchanged. When required, `ConfigError` at startup.

## Logging

On successful attachment, logs path, content hash prefix (12 chars), and whether truncation occurred — not full file content.

## Post-update integrity checks

After handover verification each cycle, `verify_ai_context_after_update()` (`src/cyclopsctl/verify.py`) snapshots `ai-context.md` before the update phase and checks the result:

| Condition | Outcome |
|-----------|---------|
| File missing after update (existed before, or `--require-ai-context`) | Fail (`AiContextVerificationError`) |
| Protected sections missing (`Rules (DO NOT UPDATE)`, `Implementation Phase Rules`, `Update Phase Rules`) | Fail |
| Destructive shrink (≥40% fewer lines when prior size ≥80 lines) | Fail |
| Soft max exceeded (~1000 lines) | Warn only |

## Errors

`PromptComposeError` — required ai-context missing at compose time (normally caught at config when `--require-ai-context`).

`AiContextVerificationError` — post-update integrity failure (see above).

Tests: `tests/test_prompt.py` (composition), `tests/test_loop.py` (integration), `tests/test_config.py` (config keys), `tests/test_verify.py` (integrity checks).
