# Configuration profiles

Named **profiles** let you reuse run presets (daytime-fast, overnight-quality, low-credits, solo-default, or custom) without repeating CLI flags.

## Precedence

Effective settings merge in this order (later wins):

1. Built-in defaults (`config.py`)
2. Base `cyclopsctl.toml` (top-level keys)
3. Selected profile (`--profile NAME`)
4. CLI flags

CLI always overrides profile and base TOML values.

## TOML layout

Define profiles and optional routing presets in `cyclopsctl.toml`:

```toml
[routing_profile.fast-default]
composer_tier = "fast"
grok_tier = "fast"
fable_enabled = true
opus_enabled = false

[profile.daytime-fast]
routing_profile = "fast-default"
retry_on = "transient"
cycles = 3

[profile.solo-default]
cycles = 5
task_source = "handover"
```

- `[profile.<name>]` — run settings: `cycles`, `task_source`, `retry_on`, `retry_max_attempts`, `default_model`, etc.
- `routing_profile` — references a `[routing_profile.<name>]` preset.
- Profile-level `composer_tier`, `grok_tier`, `fable_enabled`, `opus_enabled`, or `[profile.<name>.routing]` merge into the effective `[routing]` section.

Profile and routing-profile tables are stripped from the effective config when no `--profile` is selected, so defining profiles does not change default runs.

## CLI usage

```bash
# Run with a named profile (requires --config)
cyclopsctl run --config cyclopsctl.toml --profile daytime-fast

# Seed cyclopsctl.toml with profile defaults applied at top level
cyclopsctl init --project-root G:/DEV/MyProject --profile solo-default
```

`--profile` without `--config` is rejected for `run` (profiles are read from the TOML file).

## Built-in profiles (init seeding)

`cyclopsctl init` ships built-in profile and routing-preset tables in `init_scaffold.py`:

| Profile | Purpose |
|---------|---------|
| `solo-default` | Standard solo dev: 5 cycles, `handover` task source |
| `daytime-fast` | Fast Composer tier, transient retry |
| `overnight-quality` | Standard tier, quality routing preset, retry |
| `low-credits` | Fast tier, Opus disabled |

Init writes `cyclopsctl.toml` and optional starter files non-destructively (refuses overwrite unless `--force` / `--force-all`). See `docs/setup/project-scaffold.md` for template and gitignore behavior.

## Modules

| Module | Role |
|--------|------|
| `profiles.py` | Load `[profile.*]` / `[routing_profile.*]`, merge layers |
| `config.py` | `load_effective_config()` applies profile before `build_run_config` |
| `init_scaffold.py` | Profile-aware `cyclopsctl init` seeding |

## Errors

- Unknown profile name → `ConfigError` listing available profiles
- Unknown `routing_profile` reference → `ConfigError` with available routing presets
- Init on existing `cyclopsctl.toml` → refuse overwrite (validate profile if `--config` points at existing file)
