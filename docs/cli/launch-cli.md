# Interactive launch CLI

`cyclopsctl` (no subcommand) and `cyclopsctl launch` run pre-run interactive setup before dispatching `cyclopsctl run` internally. This complements `cyclopsctl doctor` (read-only checks) and the in-run Rich dashboard — it is for choosing how many cycles to run, not cycle progress.

## Commands

```bash
# Recommended interactive path (defaults to launch when no subcommand is given)
cyclopsctl
cyclopsctl launch --project-root /path/to/project

# Non-interactive run (CI / scripts)
cyclopsctl launch --action run --project-root /path/to/project --cycles 3 --yes
cyclopsctl launch --action run --cycles 2 --profile daytime-fast --composer-tier fast \
  --no-opus --strict-handover --resume --tag phase-2 --yes --plain

# Power-user entry actions (non-interactive or explicit --action on TTY)
cyclopsctl launch --action bootstrap --from-prd prd.md --tag master --skip-analyze --yes
cyclopsctl launch --action doctor [--fix]
cyclopsctl launch --action models
```

Bare invocations without a known subcommand are normalized to `launch` (e.g. `cyclopsctl --cycles 3` → `cyclopsctl launch --cycles 3`, implying `--action run`). `--help` and `--version` are not rewritten.

Shared flags: `--no-env`, `--config`, `--project-root`, `--current-handover`, `--update-handover`, `--complexity-report`, `--ai-context`, `--tag`, `--plain`, `--state-file`, `--history-file`.

Launch-only flags: `--action`, `--profile`, `--composer-tier`, `--no-opus`, `--cycles`, `--strict-handover`, `--fresh`, `--resume`, `--from-prd`, `--skip-analyze`, `--fix`, `--yes` / `-y`.

## Default startup flow

1. Load `LaunchConfig` (defaults `project_root` to `cwd` when omitted).
2. Load project-root `.env` unless `--no-env`.
3. On **Windows**, enter `managed_sdk_bridge` for the full launch session (PRD parse/analyze and any dispatched subcommand) when bridge env vars are unset — see [launch-prd-change.md](launch-prd-change.md#windows-sdk-bridge).
4. Run extended diagnostics via `run_launch_diagnostics` (doctor checks plus update-handover, ai-context, and pending-task list).
5. Print Rich launch overview (TTY) or plain checklist (non-TTY / `--plain`): active tag, pending count, queue next, handover task ID, suggested cycles.
6. For **Run** (default on TTY): detect PRD changes and optionally create a new tag (see [launch-prd-change.md](launch-prd-change.md)).
7. **Auto-repair handover** when the active queue has pending tasks and handover is missing, has no `# Task ID:` marker, is placeholder `# Task ID: 0`, or is out of sync with `cyclopsctl tasks next` — calls `sync_current_handover` and prints one line: `Synced handover for task N — <title>`.
8. Ask **Number of cycles** (default `min(pending, 5)`), **Use Opus on high-complexity tasks (complexity 9-10)?** (default from `cyclopsctl.toml` / profile), and confirm `Start N cycles on task M? [Y/n]` (default yes). Skip the Opus question when `--no-opus` is passed.
9. Infer fresh/resume, strict-handover, profile, and composer tier from `cyclopsctl.toml`, run history, and optional CLI flags — not prompted in the default flow.
10. Block spawn if any preflight check failed.
11. Assemble `cyclopsctl run` argv and delegate via `_dispatch_launch_argv`.

Power users reach bootstrap, doctor, and models via `--action` (no action menu on the default TTY path).

## Inferred run options

| Setting | Default inference |
|---------|-------------------|
| **Resume** | `--resume` when run history is aligned with the current handover (`resolve_startup`) |
| **Fresh** | Only when `--fresh` is passed (forces first-prompt bootstrap for cycle 1) |
| **Strict handover** | `strict_handover` in `cyclopsctl.toml`, or `--strict-handover` |
| **Profile** | `--profile` when passed; otherwise base config |
| **Composer tier / Opus** | `[routing]` defaults from config (`resolve_routing_defaults`) |
| **Plain** | `plain` in config or `--plain` |
| **Tag** | Active tag from config, PRD-change flow, or `--tag` |

See [run-history.md](../runtime/run-history.md) for cycle-1 prompt selection (handover vs `--fresh`).

## Diagnostics

In addition to [doctor checks](doctor-cli.md), launch validates:

| Check | Pass criteria |
|-------|---------------|
| Update handover | `update-handover-prompt.md` (or configured path) exists |
| ai-context | `ai-context.md` (or configured path) exists |
| Native pending list | `TaskBackend.list_pending()` succeeds; pending count summarized |

The overview also shows active tag, handover Task ID vs queue next, suggested cycles (`min(pending, 5)`), resume availability, configured profile names, and default composer tier / Opus settings from `cyclopsctl.toml`.

## Interactive prompts (default Run flow)

When stdin is a TTY and `--plain` is not set, the default Run flow asks for:

- **Number of cycles** (default: suggested cycles)
- **Use Opus on high-complexity tasks (complexity 9-10)?** (default: config/profile; skipped when `--no-opus` is set)
- **Confirmation** before spawn (default yes)

Other run options are inferred (see table above). Explicit CLI flags still override inference when passed with `--yes`.

## Interactive prompts (Bootstrap flow)

Use `--action bootstrap` to reach bootstrap prompts:

- PRD file path (default: `prd.md` under project root)
- Optional task tag
- Whether to run complexity analysis after parse
- Confirmation before spawn

## Non-TTY fallback

Without a TTY, the launcher prints a checklist of all actions and required flags and **does not** block on prompts. It exits with code **2** unless `--action run` (or implicit run via `--cycles`) includes `--cycles`. Bootstrap, doctor, and models actions work non-interactively with `--action` alone (bootstrap defaults apply). Use `--yes` to skip confirmation when passing explicit flags on a TTY.

## Configuration

`LaunchConfig` in `config.py` resolves paths under `project_root` without requiring files to exist at config load time (checks run at launch). When `--config` points to `cyclopsctl.toml`, assembled `run` argv passes `--config` only; otherwise all required run paths are passed explicitly.

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `LaunchAction` / `LaunchDispatch` | `launcher.py` | Entry action enum and dispatch result |
| `run_launch` | `launcher.py` | Status gather, handover repair, cycles prompt, argv assembly |
| `infer_launch_defaults` | `launcher.py` | Infer resume, routing, and config flags without prompting |
| `maybe_repair_handover_at_launch` | `launcher.py` | Sync stale/missing handover before prompting |
| `handover_needs_sync` | `project_setup.py` | Detect missing, placeholder, or stale handover |
| `build_run_argv` / `build_bootstrap_argv` | `launcher.py` | Subcommand argv from resolved choices |
| `run_launch_diagnostics` | `doctor.py` | Extended preflight for launch |
| `load_launch_config` | `config.py` | `LaunchConfig` merge and validation |
| `print_launch_status` | `tui.py` | Rich/plain launch overview |
| `_launch_command` / `_dispatch_launch_argv` | `cli.py` | Loads env, calls `run_launch`, dispatches subcommand |

Tests: `tests/test_launcher.py`, `tests/test_tui.py` (launch overview).

Related: `docs/cli/doctor-cli.md`, `docs/runtime/run-history.md`, `docs/runtime/cycle-dashboard.md`, `docs/setup/cli-configuration.md`, `docs/setup/config-profiles.md`, `docs/setup/prd-bootstrap.md`, `docs/cli/launch-prd-change.md`.
