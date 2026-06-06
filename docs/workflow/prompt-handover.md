# Handover prompt utilities

`src/cyclopsctl/prompt.py` reads user-owned Markdown handover files, parses the `# Task ID: <n>` marker, builds whitespace-normalized content hashes for verification, and composes final agent prompts with injected ai-context (see `ai-context-injection.md`).

## Cycle-1 prompt source

Implementation body selection is **not** the same as “first cycle vs later cycles”:

| Condition | Cycle 1 implementation body |
|-----------|----------------------------|
| `current-handover-prompt.md` exists with `# Task ID:` | `current_handover` |
| Handover missing or no Task ID marker | `first_prompt` (bootstrap) |
| `--fresh` | `first_prompt` (ignores ready handover) |

`history.resolve_startup()` in `history.py` implements this. Run history affects resume **warnings** only.

**Do not use `prompts/setup-ai-workflow.md` for normal task cycles.** It is configured as `first_prompt` for greenfield bootstrap when no handover exists. On a cyclopsctl-ready repo, cycle 1 always sends the current handover.

Startup logs: `Using current-handover-prompt.md for cycle 1` vs `Starting fresh bootstrap…`.

## Reading

- `read_prompt_text(path)` — UTF-8 via bytes (preserves `\r\n` as written).
- `snapshot_handover(path, allow_missing=False)` — full `HandoverSnapshot` for pre/post update compare.

## Task ID marker

- Line format: `# Task ID: 7` (parent integer only; `7.1` and invalid lines fail when `required=True`).
- `parse_task_id(text, required=True)` — used by verify when a marker is mandatory.

## Hashing

- `normalize_whitespace` collapses runs of whitespace; `sha256_content_hash` digests normalized text.
- `prompt_content_from_raw` returns `PromptContent` (raw, normalized, hash).

## Cycle 1 missing file

`snapshot_handover(..., allow_missing=True)` returns `missing=True` with empty content and a stable empty-string hash (no raise). Used before the first update creates `current-handover-prompt.md`.

## Errors

`PromptError` — missing file, or missing/invalid marker when required.

Tests: `tests/test_prompt.py`.
