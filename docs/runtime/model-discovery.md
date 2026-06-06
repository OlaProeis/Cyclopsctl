# Cursor SDK model discovery

`src/cyclopsctl/models.py` wraps `Cursor.models.list()` with a stable capability contract for routing.

## APIs

- **`discover_model_capabilities`** — calls `Cursor.models.list()` (injectable `list_models` / `api_key` for tests) and returns **`ModelCapabilities`**. Accepts optional `composer_tier` (`"standard"`, `"fast"`, or explicit model id).
- **`detect_opus_high_thinking`** — picks an Opus 4.8 high-thinking preset from listings; prefers variant params (`reasoning`, `thinking`, `reasoning_effort` = high); excludes fast and Max tiers.
- **`detect_sonnet_max`** / **`detect_opus_max`** — bootstrap Max Mode presets; prefer SDK variant params (`context=1m`, `max=true`) on base model ids, with legacy `*-max` catalog ids as fallback.
- **`detect_composer`** — resolves Composer from listings with tier preference; fast tier falls back to standard with a warning when unavailable.
- **`COMPOSER_MODEL_ID`** / **`COMPOSER_FAST_MODEL_ID`** — default Composer ids (`composer-2.5`, `composer-2.5-fast`).

## Composer tier selection

| `composer_tier` | Behavior |
|-----------------|----------|
| `"standard"` (default) | Prefer non-fast Composer from account listings |
| `"fast"` | Prefer `composer-2.5-fast` (or account equivalent); warn and fall back to standard if missing |
| explicit model id | Use `ModelSelection(id=...)` directly |

Set via `[routing].composer_tier` in `cyclopsctl.toml` or passed into `discover_model_capabilities` / `ModelRouter`.

## ModelCapabilities

| Field | Meaning |
|-------|---------|
| `composer` | Preferred Composer preset for the configured tier |
| `composer_standard` | Standard (non-fast) Composer preset |
| `composer_fast` | Fast Composer preset when listed, else `None` |
| `opus_available` | Whether a high-thinking Opus preset was found |
| `opus` | `ModelSelection` for high-complexity cycles, or `None` |

`routing.py` imports discovery from this module; complexity score → model selection stays in `ModelRouter` (see `docs/runtime/model-routing.md`).

Tests: `tests/test_models.py`.
