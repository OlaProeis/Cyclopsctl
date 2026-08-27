# Model routing and complexity report

`src/cyclopsctl/routing.py` maps native complexity scores to Cursor runtime models (read-only report, no `tasks.json` writes).

## Complexity report

- **Path:** `CyclopsctlConfig.complexity_report` (default `.cyclopsctl/reports/complexity-report.json`).
- **`load_complexity_report(path)`** — reads JSON; returns empty scores if the file is missing or invalid.
- **`parse_complexity_payload`** — builds `taskId` → `complexityScore` from `complexityAnalysis` (parent task ids only).

## Default routing (no `[routing]` rules)

When `cyclopsctl.toml` has no `[routing]` section, or the section has no `rules`, default score bands apply:

| Score | Model |
|-------|--------|
| 1–5 | Composer (`composer_tier`: standard or fast) |
| 6–8 | Grok (`grok_tier`: standard or fast; default **standard** / `fast=false`) |
| 9–10 | Fable 5 high-thinking (not Max Mode) |
| 9–10 when Fable disabled (`--no-fable` / launch prompt) or unavailable | Grok (same `grok_tier`), then `default_model` |
| Missing score / missing report entry | Composer + `fallback=True` |
| Grok unavailable on account | Composer + warning |
| Fable and Grok both unavailable for 9–10 | `default_model` (config) + one warning per `ModelRouter` instance |

## Configuring routing

### TOML (`[routing]` in `cyclopsctl.toml`)

Copy from `cyclopsctl.toml.example`. Loaded into `CyclopsctlConfig.routing` via `config.py`.

| Key | Purpose |
|-----|---------|
| `composer_tier` | `"standard"` (default), `"fast"`, or explicit model id |
| `grok_tier` | `"standard"` (default), `"fast"`, or explicit model id |
| `fable_enabled` | `false` forces fallback for high-complexity Fable rules / default band |
| `opus_enabled` | `false` disables Opus when a custom rule selects Opus |
| `rules_file` | Optional JSON file with `rules` / `fallback` keys (merged with inline TOML) |
| `[[routing.rules]]` | Score bands: `min_score`, `max_score`, `model` |
| `[routing.fallback]` | `model` and/or `missing_score` for unmatched or missing scores |

**Model aliases** (case-insensitive):

| Alias | Resolves to |
|-------|-------------|
| `composer`, `composer-standard`, `composer-2.5` | Standard Composer |
| `composer-fast`, `composer-2.5-fast` | Fast Composer |
| `grok`, `grok-standard`, `grok-4.6`, `grok-4.5` | Grok (respects `grok_tier` when using `grok` / `grok-standard`) |
| `grok-fast` | Fast Grok |
| `fable`, `fable-high-thinking`, `fable-5` | Fable high-thinking |
| `opus`, `opus-high-thinking` | Opus high-thinking |

Other values matching `^[a-zA-Z][a-zA-Z0-9._-]*$` are treated as explicit model ids. Unknown `composer-*` / `opus-*` / `grok-*` / `fable-*` strings fail validation at config load.

**Validation:** overlapping score bands, reversed ranges (`min_score > max_score`), and invalid aliases raise `ConfigError` during `build_run_config`.

**JSON rules file:** `load_routing_config_from_json(path)` accepts the same shape as the `[routing]` table root.

### CLI / launch overrides

| Flag | Effect |
|------|--------|
| `--composer-tier standard\|fast` | Override Composer tier for this run |
| `--grok-tier standard\|fast` | Override Grok tier for this run |
| `--no-fable` | Disable Fable for complexity 9–10 |
| `--no-opus` | Disable Opus when custom rules select it |

Interactive `cyclopsctl` / `cyclopsctl launch` prompts for:

1. Composer tier (standard / fast)
2. Grok tier (standard / fast) — default **standard** (not-fast)
3. Whether to use Fable on complexity 9–10

### Profiles

Named `[profile.*]` / `[routing_profile.*]` may set `composer_tier`, `grok_tier`, `fable_enabled`, `opus_enabled`, and nested `[profile.<name>.routing]` rules. See `docs/setup/config-profiles.md`.

## APIs

- **`ModelRouter`** — `from_paths(...)` or constructor with `ComplexityReport`, `ModelCapabilities`, and optional `RoutingConfig`; **`route(task_id)`** → `RoutingDecision` (`model`, `complexity_score`, `used_opus`, `used_fable`, `used_grok`, `fallback`).
- **`parse_routing_config`**, **`resolve_model_for_score`** — config parsing and score → model resolution (used by `ModelRouter` and tests).
- Model discovery (`discover_model_capabilities`, `detect_composer`, `detect_grok`, `detect_fable_high_thinking`) lives in `models.py` — see `docs/runtime/model-discovery.md`.
- Lookup uses **`NextTaskResult.numeric_id`** from `tasks/types.py`.

## Types

`ComplexityReport`, `RoutingConfig`, `RoutingRule`, `RoutingFallback`, `ModelCapabilities`, `RoutingDecision`, `ModelSelection` (from `cursor_sdk`).

Tests: `tests/test_routing.py`.
