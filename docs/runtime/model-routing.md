# Model routing and complexity report

`src/cyclopsctl/routing.py` maps native complexity scores to Cursor runtime models (read-only report, no `tasks.json` writes).

## Complexity report

- **Path:** `CyclopsctlConfig.complexity_report` (default `.cyclopsctl/reports/complexity-report.json`).
- **`load_complexity_report(path)`** — reads JSON; returns empty scores if the file is missing or invalid.
- **`parse_complexity_payload`** — builds `taskId` → `complexityScore` from `complexityAnalysis` (parent task ids only).

## Default routing (no `[routing]` config)

When `cyclopsctl.toml` has no `[routing]` section, or the section has no `rules`, default score bands apply:

| Score | Model |
|-------|--------|
| 1–8 | Standard Composer (`composer-2.5` or account listing) |
| 9–10 | Opus 4.8 high-thinking preset (from account listing) |
| Missing score / missing report entry | Composer + `fallback=True` |
| Opus unavailable on account | `default_model` (config) + one warning per `ModelRouter` instance |

## Configurable routing (`[routing]` in TOML)

Optional routing table in `cyclopsctl.toml` (see `cyclopsctl.toml.example`). Loaded into `CyclopsctlConfig.routing` via `config.py`.

| Key | Purpose |
|-----|---------|
| `composer_tier` | `"standard"` (default), `"fast"`, or explicit model id — passed to model discovery |
| `opus_enabled` | `false` forces fallback for high-complexity Opus rules |
| `rules_file` | Optional JSON file with `rules` / `fallback` keys (merged with inline TOML) |
| `[[routing.rules]]` | Score bands: `min_score`, `max_score`, `model` |
| `[routing.fallback]` | `model` and/or `missing_score` for unmatched or missing scores |

**Model aliases** (case-insensitive): `composer`, `composer-standard`, `composer-2.5`, `composer-fast`, `composer-2.5-fast`, `opus`, `opus-high-thinking`. Other values matching `^[a-zA-Z][a-zA-Z0-9._-]*$` are treated as explicit model ids. Unknown `composer-*` / `opus-*` strings fail validation at config load.

**Validation:** overlapping score bands, reversed ranges (`min_score > max_score`), and invalid aliases raise `ConfigError` during `build_run_config`.

**JSON rules file:** `load_routing_config_from_json(path)` accepts the same shape as the `[routing]` table root.

## APIs

- **`ModelRouter`** — `from_paths(...)` or constructor with `ComplexityReport`, `ModelCapabilities`, and optional `RoutingConfig`; **`route(task_id)`** → `RoutingDecision` (`model`, `complexity_score`, `used_opus`, `fallback`).
- **`parse_routing_config`**, **`resolve_model_for_score`** — config parsing and score → model resolution (used by `ModelRouter` and tests).
- Model discovery (`discover_model_capabilities`, `detect_composer`) lives in `models.py` — see `docs/runtime/model-discovery.md`.
- Lookup uses **`NextTaskResult.numeric_id`** from `tasks/types.py`.

## Types

`ComplexityReport`, `RoutingConfig`, `RoutingRule`, `RoutingFallback`, `ModelCapabilities`, `RoutingDecision`, `ModelSelection` (from `cursor_sdk`).

Tests: `tests/test_routing.py`.
