# Environment loading

The cyclopsctl loads `<project-root>/.env` automatically before preflight so values such as `CURSOR_API_KEY` are available without exporting them manually.

**Important:** Keys belong in the **target project's** `.env` at `project_root` — not inside the Python venv and not in the cyclopsctl package directory unless that directory is the project you are initializing.

## Behavior

| Condition | Result |
|-----------|--------|
| `.env` exists and is readable | Variables loaded with `override=False` (existing process env wins) |
| `cyclopsctl init` / `bootstrap` | Requires `CURSOR_API_KEY` in `.env` or the process environment before native PRD parse/analyze |
| `.env` missing | No-op |
| `.env` unreadable | `EnvLoadError`; CLI exits with a configuration error |
| `--no-env` flag set | Loading skipped entirely |

## Project root resolution

| Subcommand | Root used for `.env` |
|------------|----------------------|
| `cyclopsctl run` | `project_root` from config (after `--config` / flags merge) |
| `cyclopsctl doctor` / `check` | `project_root` from config |
| `cyclopsctl models` | Current working directory |

Loading runs in the shared startup path **before** preflight on `run`, before diagnostics on `doctor`/`check`, and before model inspection on `models`.

## CLI flag

```bash
cyclopsctl run --no-env --config cyclopsctl.toml
cyclopsctl doctor --no-env --project-root /path/to/project
cyclopsctl models --no-env
```

Use `--no-env` in CI or when environment variables are set explicitly.

## Logging

On successful load, the cyclopsctl logs the resolved `.env` path at info level (path only, no secret values).

## Implementation

- `src/cyclopsctl/env.py` — `load_project_env`, `project_env_path`, `EnvLoadError`
- `src/cyclopsctl/cli.py` — `_startup_load_env`, shared `--no-env` on `run`, `doctor`/`check`, and `models`
- Dependency: `python-dotenv`

Tests: `tests/test_env.py`.
