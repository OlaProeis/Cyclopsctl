# Task bootstrap model configuration

Centralized `[tasks]` settings and `auto` model resolution for native PRD parsing and complexity analysis.

## Configuration

`cyclopsctl.toml.example` documents the optional `[tasks]` table:

| Key | Default | Purpose |
|-----|---------|---------|
| `backend` | `native` | Task storage backend (native only in v1.0) |
| `parse_model` | `auto` | Cursor model for structured PRD→tasks JSON |
| `analyze_model` | `auto` | Cursor model for complexity scoring |
| `max_tasks` | *(unset)* | Optional cap on parent tasks when parsing a PRD; omit to let the model decide |

`parse_tasks_config()` in `config.py` merges CLI overrides with the `[tasks]` section. `TasksConfig` is the validated result.

CLI keys that override file settings: `parse_model`, `analyze_model`, `max_tasks`. The TOML alias `default_num_tasks` is still read as `max_tasks`.

## Bootstrap presets (`tasks/bootstrap_model.py`)

Init and parse/analyze use the same preset for both steps. Interactive init shows six choices:

| Preset | When to use |
|--------|-------------|
| `auto` | Default — Sonnet first, Composer fallback on failure |
| `composer` | Simple PRDs; included with Cursor (no extra API credits) |
| `sonnet` | Strong JSON/decomposition; good quality/cost balance |
| `opus-high-thinking` | Large technical PRDs needing deep decomposition |
| `sonnet-max` | Very long PRDs; uses `context=1m` / `max=true` variant params when listed |
| `opus-max` | Most complex PRDs; extended context + high thinking variant params when listed |

Premium presets fall back through weaker tiers to Composer on credit or runtime errors.
CLI: `cyclopsctl init --bootstrap-model opus-high-thinking|sonnet-max|opus-max`.

## Model resolution (`tasks/models.py`)

Display/fallback helpers only; parse and analyze run through `bootstrap_model_attempt_chain`.

### Parse (`resolve_parse_model`)

1. Explicit model id → use as-is (logging display only when not `auto`).
2. `auto` → prefer Composer standard for local SDK reliability.
3. Listing failure → fallback `composer-2.5`.

### Analyze (`resolve_analyze_model`)

1. Explicit model id → use as-is.
2. `auto` → Composer standard from listings.
3. Listing failure → fallback `composer-2.5`.

## Wiring

| Consumer | How models are chosen |
|----------|----------------------|
| `parse_prd_with_cursor` | `ParsePrdConfig.parse_model` → `bootstrap_model_attempt_chain` |
| `analyze_complexity_with_cursor` | `AnalyzeComplexityConfig.analyze_model` → `bootstrap_model_attempt_chain` |

## Tests

`tests/test_tasks_models.py` — config defaults, `[tasks]` parsing, explicit/auto resolution, Composer-only parse warning, and example TOML alignment.
