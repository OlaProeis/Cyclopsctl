# Cursor SDK model discovery

`src/cyclopsctl/models.py` wraps `Cursor.models.list()` with a stable capability contract for routing.

## APIs

- **`discover_model_capabilities`** — calls `Cursor.models.list()` (injectable `list_models` / `api_key` for tests) and returns **`ModelCapabilities`**. Accepts optional `composer_tier` and `grok_tier` (`"standard"`, `"fast"`, or explicit model id).
- **`detect_composer`** — resolves Composer from listings with tier preference; prefers explicit `fast=false` / `fast=true` variant params when listed.
- **`detect_grok`** / **`detect_grok_standard`** / **`detect_grok_fast`** — resolve Grok (`grok-4.6`) with `effort=high` and the requested `fast` flag. Catalog default is often `fast=true`; orchestration defaults to **standard** (`fast=false`).
- **`detect_fable_high_thinking`** — picks Fable 5 with `thinking=true` + `effort=high` on non-Max context.
- **`detect_opus_high_thinking`** — picks an Opus high-thinking / high-effort preset; excludes fast and Max tiers.
- **`detect_sonnet_max`** / **`detect_opus_max`** — bootstrap Max Mode presets.
- **`COMPOSER_MODEL_ID`** / **`COMPOSER_FAST_MODEL_ID`** / **`GROK_MODEL_ID`** — fallback catalog ids.

## Tier selection

| Setting | Behavior |
|---------|----------|
| `composer_tier = "standard"` (default) | Prefer Composer with `fast=false` |
| `composer_tier = "fast"` | Prefer `fast=true`; warn and fall back to standard if missing |
| `grok_tier = "standard"` (default) | Prefer Grok `effort=high,fast=false` |
| `grok_tier = "fast"` | Prefer `effort=high,fast=true`; warn and fall back to standard if missing |
| explicit model id | Use `ModelSelection(id=...)` directly |

Set via `[routing]` in `cyclopsctl.toml`, CLI (`--composer-tier`, `--grok-tier`), or interactive launch prompts.

## Default complexity bands (diagnostics labels)

| Band | Model family |
|------|----------------|
| 1–5 | Composer |
| 6–8 | Grok |
| 9–10 | Fable |

## ModelCapabilities

| Field | Meaning |
|-------|---------|
| `composer` | Preferred Composer preset for the configured tier |
| `composer_standard` / `composer_fast` | Explicit tier presets |
| `grok` | Preferred Grok preset for the configured tier |
| `grok_standard` / `grok_fast` / `grok_available` | Grok tier presets and availability |
| `fable` / `fable_available` | Fable high-thinking for complexity 9–10 |
| `opus` / `opus_available` | Opus preset for optional custom rules |

`routing.py` imports discovery from this module; complexity score → model selection stays in `ModelRouter` (see `docs/runtime/model-routing.md`).

Tests: `tests/test_models.py`.
