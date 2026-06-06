# CLI configuration

`cyclopsctl run` loads settings from CLI flags and an optional TOML file. **CLI flags override file values.**

## Quick start (copy and run)

1. Copy the checked-in template to a local config file (gitignored):

   ```bash
   cp cyclopsctl.toml.example cyclopsctl.toml
   ```

2. Edit `cyclopsctl.toml`:
   - Set `project_root` to the **absolute** path of your repository.
   - Adjust `cycles`, handover paths, and other optional keys as needed.
   - Do not commit secrets or machine-specific paths you do not intend to share.

3. Run the cyclopsctl:

   ```bash
   cyclopsctl run --config cyclopsctl.toml
   ```

   Override any file value on the command line when needed, for example:

   ```bash
   cyclopsctl run --config cyclopsctl.toml --cycles 3 --plain
   ```

The template at `cyclopsctl.toml.example` documents every supported TOML key with comments. Use it as the source of truth when adding new settings.

## Required settings

| Setting | TOML key | Flag | Notes |
|---------|----------|------|--------|
| Cycles | `cycles` | `--cycles` | Positive integer |
| Project root | `project_root` | `--project-root` | **Absolute** path to repo |
| First prompt | `first_prompt` | `--first-prompt` | Bootstrap-only file (e.g. `prompts/setup-ai-workflow.md`); used when handover missing or `--fresh` — **not** cycle 1 when handover is ready |
| Current handover | `current_handover` | `--current-handover` | File under project |
| Update handover | `update_handover` | `--update-handover` | File under project |

## Optional settings

| Setting | TOML key | Flag | Default |
|---------|----------|------|---------|
| Config file | — | `--config` | — |
| Complexity report | `complexity_report` | `--complexity-report` | `.cyclopsctl/reports/complexity-report.json` |
| Task tag | `tag` | `--tag` | — |
| Fallback model | `default_model` | `--default-model` | `composer-2.5` |
| Plain text logs | `plain` | `--plain` | `false` (Rich dashboard when stderr is a TTY) |
| Strict handover alignment | `strict_handover` | `--strict-handover` | `false` (warn on Task ID mismatch; see `docs/workflow/handover-alignment.md`) |
| Task source | `task_source` | `--task-source` | `handover` (`sequential`; see `docs/tasks/task-selection.md`) |
| Pinned next cycle | `task_id` | `--task-id` | — (pending task validated via `cyclopsctl tasks show`) |

Relative paths in TOML or CLI resolve against `project_root`. Missing or non-absolute `project_root` fails fast with `ConfigError`.

## Cycle-1 prompt selection

`first_prompt` is **required in config** but used only for greenfield bootstrap:

| Situation | Cycle 1 implementation body |
|-----------|----------------------------|
| `current-handover-prompt.md` has `# Task ID:` | `current_handover` |
| Handover missing / no marker | `first_prompt` |
| `--fresh` | `first_prompt` |

On a cyclopsctl-ready repo, normal `cyclopsctl run` always sends the current handover for cycle 1. Run history (`.cyclopsctl/run-history.json`) affects resume warnings only — it does **not** switch back to `first_prompt` when `bootstrap_complete` is false.

Log line to expect: `Using current-handover-prompt.md for cycle 1`. If you see `Starting fresh bootstrap…` while a task handover exists, check for `--fresh` or a missing/invalid handover file.

See `docs/runtime/run-history.md` and `docs/workflow/prompt-handover.md`.

## Example TOML

See `cyclopsctl.toml.example` at the repository root for a fully commented template. Minimal shape:

```toml
cycles = 5
project_root = "/path/to/your/project"
first_prompt = "prompts/setup-ai-workflow.md"
current_handover = "current-handover-prompt.md"
update_handover = "update-handover-prompt.md"
plain = false
```

Implementation: `src/cyclopsctl/config.py` (`CyclopsctlConfig`, `load_run_config`). The run loop is in `src/cyclopsctl/loop.py` — see `docs/runtime/cycle-orchestration.md`.
