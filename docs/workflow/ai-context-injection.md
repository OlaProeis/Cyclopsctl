# AI context injection

The cyclopsctl prepends `ai-context.md` to **implementation** prompts only. Handover files stay unchanged; injection is cyclopsctl-side only. The **update** phase sends `update-handover-prompt.md` without re-attaching ai-context (same agent session already has it).

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

## Errors

`PromptComposeError` — required ai-context missing at compose time (normally caught at config when `--require-ai-context`).

Tests: `tests/test_prompt.py` (composition), `tests/test_loop.py` (integration), `tests/test_config.py` (config keys).
