# Cyclopsctl - AI Context

This file is **whole-project** agent memory across all phases and task tags. It is not phase-scoped. Update it additively; never rewrite it for the current phase alone.

## Rules (DO NOT UPDATE)
- **Implementation sessions:** follow **Implementation Phase Rules** below only. Do not apply update-phase work.
- **Update sessions:** follow **Update Phase Rules** below only when you receive the update handover prompt.
- Only do the task specified; do not start the next task or go over scope.
- Run `python -m pytest` after code changes to verify tests pass.
- Follow existing code patterns and conventions.
- Use Context7 MCP to fetch library documentation when needed (resolve library ID first, then fetch docs). Task queue operations use **`cyclopsctl tasks` CLI only**.

## Cyclopsctl prompt selection (read this first)
The cyclopsctl sends **one implementation body** and **one update body** per cycle. Know which file you received:

| Phase | What the cyclopsctl sends | When |
|-------|---------------------------|------|
| **Implementation** | `current-handover-prompt.md` (+ `ai-context.md` prepended) | **Every normal cycle**, including cycle 1, when the handover file exists with `# Task ID:` |
| **Update** | `update-handover-prompt.md` only (same agent session; no ai-context re-attach) | After implementation completes |
| **Bootstrap only** | `prompts/setup-ai-workflow.md` (configured as `first_prompt`) | Handover missing / no Task ID marker, or human passed `--fresh` |

**Critical — do not confuse bootstrap with task cycles:**
- `prompts/setup-ai-workflow.md` is **greenfield bootstrap only**. It tells the agent to create docs, `ai-context.md`, and handover templates. It is **not** part of normal task execution on this repo.
- If you are implementing a numbered task (`# Task ID: N` in the handover), you received **`current-handover-prompt.md`**, not setup-ai-workflow.
- **Run history does not choose the cycle-1 prompt.** If `current-handover-prompt.md` is ready, cycle 1 uses it even when `.cyclopsctl/run-history.json` says `bootstrap_complete: false`.

**Symptoms of the wrong prompt:** implementation agent edits `docs/`, `docs/index.md`, `ai-context.md`, or handover files instead of `src/` / `tests/`. That usually means setup-ai-workflow was sent by mistake — fix cyclopsctl startup (`docs/runtime/run-history.md`), not agent discipline.

See `docs/runtime/run-history.md`, `docs/runtime/cycle-orchestration.md`, and `docs/workflow/prompt-handover.md`.

## Implementation Phase Rules
When working from **`current-handover-prompt.md`** (the normal case for every cyclopsctl task cycle):

- **DO:** Implement and test only the current parent task described in the handover.
- **DO:** Run `python -m pytest` before finishing; meet the task test strategy.
- **DO NOT:** Read `prd.md` during cyclopsctl cycles — task scope, details, and test strategy are already in this handover.
- **DO NOT:** Mark tasks done or change task status during implementation.
- **DO NOT:** Run `cyclopsctl tasks next`, rewrite `current-handover-prompt.md`, or edit `ai-context.md`.
- **DO NOT:** Create or update docs in `docs/`, or edit `docs/index.md`.
- **DO NOT:** Edit `update-handover-prompt.md`.
- **DO NOT:** Split the task into smaller units or expand its scope; implement it as one flat task.
- **DO NOT:** Run bootstrap/setup workflows (`prompts/setup-ai-workflow.md`) — if you see instructions to generate workflow files, you received the wrong prompt; stop and report.

If you receive `prompts/setup-ai-workflow.md` instead of a task handover, that is **bootstrap-only** (missing handover or `--fresh`). Do not treat it as a task cycle on a cyclopsctl-ready repo.

Task completion (`cyclopsctl tasks set-status`) and all documentation updates happen only in the **update phase** (`update-handover-prompt.md`).

## Update Phase Rules
When `update-handover-prompt.md` is provided (after implementation in the same agent session):

- **DO:** Follow every step in `update-handover-prompt.md`.
- **DO:** Use `cyclopsctl tasks list pending` and pick the **lowest numeric parent id** for the next handover — not `cyclopsctl tasks next` (priority can skip ahead).
- **DO:** Rewrite `current-handover-prompt.md` for the **next** task (this is the only time that file may change).
- **DO:** Update `ai-context.md` as **additive whole-project memory** per `update-handover-prompt.md` step 2 (key facts only, not a changelog; never clear prior-phase facts unless obsolete/wrong; soft target ≤ ~1000 lines).
- **DO NOT:** Rewrite `ai-context.md` from scratch or treat it as phase-only context.
- **DO NOT:** Re-implement or extend the task you just finished unless tests are broken.

## Handover Files
| File | Who may edit | When |
|------|----------------|------|
| `current-handover-prompt.md` | Update-phase agent only | After implementation, when `update-handover-prompt.md` is sent |
| `update-handover-prompt.md` | Human / template only | Never edited by agents |
| `ai-context.md` | Update-phase agent only | Every update phase — additive whole-project memory (see `update-handover-prompt.md` step 2) |

The cyclopsctl **snapshots** `current-handover-prompt.md` before the update phase and **fails the run** if `# Task ID:` and content are unchanged afterward.

In default mode, if an implementation agent edits handover files anyway, the cyclopsctl **restores** them from the cycle-start snapshot before the update phase. With `--strict-handover`, that violation fails the run immediately with `ImplementationHandoverViolationError`.

## Tech Stack
- **Language:** Python 3.10+
- **Agent runtime:** `cursor-sdk` (`cursor_sdk`) — `Agent.create` / `send` / `wait`, local `cwd` = project root
- **Task system:** Native `.cyclopsctl/tasks/` via **`cyclopsctl tasks` CLI**
- **Config:** Environment `CURSOR_API_KEY` (auto-loaded from project-root `.env`); optional `cyclopsctl.toml` for run defaults (copy from `cyclopsctl.toml.example`); `python-dotenv`
- **Handover files:** `current-handover-prompt.md`, `update-handover-prompt.md` (cyclopsctl passthrough only)

## Architecture & Data Model
Lightweight CLI that sequences Cursor agent runs: **implement** (new agent per cycle) → **update** (same agent) → **verify** handover advanced. Does not plan tasks, edit `tasks.json`, or rewrite handover templates.

**Per-cycle flow:** **task selection** (`handover` default, or `sequential`; empty queue → successful stop) → complexity routing on selected task → selected-vs-next warning → **handover alignment** vs selected task (see `docs/workflow/handover-alignment.md`) → implementation prompt (`current_handover` + injected `ai-context.md`; cycle 1 uses handover when ready — see **Cyclopsctl prompt selection** above) → snapshot `current-handover-prompt.md` → update prompt (`update_handover` only, same session) → verify Task ID / content hash changed.

**Internal modules (target layout):**
| Module | Role |
|--------|------|
| `config` | CLI flags, paths, project root, tag, cycle count, ai-context path, optional `[routing]`, profile merge |
| `profiles` | Named `[profile.*]` / `[routing_profile.*]` tables, merge precedence (CLI > profile > base TOML) |
| `init_scaffold` | Low-level scaffold helpers (config, templates, gitignore); used by `project_setup` and tests |
| `env` | Project-root `.env` loading via `python-dotenv`; `--no-env` bypass |
| `tasks` | `TaskBackend` protocol + `get_task_backend()` factory (`backend.py`); shared types (`types.py`); native storage (`store.py`); native CRUD + `cyclopsctl tasks` CLI (`cli.py`); native PRD parse (`parse_prd.py`); native complexity analyze (`analyze.py`) |
| `bootstrap` | PRD bootstrap pipeline (`cyclopsctl bootstrap`); handover sync from native task show output |
| `project_setup` | Idempotent assess-and-setup engine: prerequisites, repair, parse/analyze gating, `last-parsed-prd.json`, launch PRD-change / new-tag flow |
| `workflow_gen` | PRD-aware workflow file generation; staleness heuristics (`detect_stale_workflow_files`, `refresh_stale_workflow_files`); `install_cyclopsctl_cursor_rules()` for `.cursor/rules/cyclopsctl/` |
| `task_selection` | Per-cycle task source (`handover`, `sequential`), `--task-id` pin |
| `models` | `Cursor.models.list()` capability discovery (Opus/Composer presets) |
| `routing` | Complexity report lookup + model selection via `ModelRouter` |
| `prompt` | Handover read/hash, `compose_agent_prompt` (ai-context injection) |
| `preflight` | `CURSOR_API_KEY` check before run start |
| `doctor` | Preflight diagnostics (`cyclopsctl doctor` / `check`; extended checks for launch) |
| `launcher` | Interactive pre-run setup (`cyclopsctl` / `launch`), PRD-change detection, auto handover repair, cycles-only default prompt, inferred resume/routing, `LaunchDispatch` argv assembly |
| `alignment` | Pre-agent handover Task ID vs selected task (warn / strict) |
| `verify` | Pre/post update snapshot compare |
| `runner` | SDK agent lifecycle, run wait, activity stream parsing (`TodoWrite` / `todo_write` plan updates + activity lines), error handling (`AgentRunError` carries `phase`), transient-failure classification |
| `sdk_bridge` | Windows `cursor-sdk-bridge` bootstrap (blocking discovery); installs an env-fallback default `Client` so run-scoped RPCs (`wait`/`observe`/`cancel`) authenticate |
| `failure_report` | Actionable `AgentRunError` report: project-slug → local transcript path resolution, stderr report formatting |
| `session` | New agent per implementation; same agent for update |
| `loop` | Main cycle orchestration; optional transient retry; calls verify after each update |
| `interrupt` | SIGINT controller, `RunInterruptedError`, exit code 130 |
| `state` | Crash-recovery run state file, `RunStateTracker`, `cyclopsctl status` |
| `history` | Cross-invocation run history, resume warnings, `--fresh`, `--resume` task skip (does **not** select cycle-1 prompt when handover is ready) |
| `logging` | Cycle logs (text / optional durable JSONL via `--cycle-log` / `[run] cycle_log`) |
| `tui` | Rich live dashboard (incl. stall heartbeat), launch overview, post-run summary (`RunDashboardState`, `RichCycleLogger`, `render_heartbeat`) |
| `cli` | Default `launch`, `run`, `doctor`/`check`, `models`, `status`, `bootstrap`, `init`; `--version` from package metadata |
| `version` | `get_package_version()` via `importlib.metadata` |
| `installer` | Global install helpers: PATH checks, remediation, verification (`python -m cyclopsctl.installer`) |

**Handover convention:** `current-handover-prompt.md` must include `# Task ID: <n>` (integer id only). One cyclopsctl cycle = one task; tasks are flat (no subtask hierarchy).

**Model routing (cyclopsctl):** Reads `.cyclopsctl/reports/complexity-report.json`. Default (no `[routing]` rules): scores 1–5 → Composer; 6–8 → Grok; 9–10 → Fable high-thinking. Optional `[routing]` in `cyclopsctl.toml` sets score bands, `composer_tier` / `grok_tier` (standard/fast), `fable_enabled`, `opus_enabled`, and fallbacks. Launch prompts for Composer/Grok tiers and Fable enablement. The **Model Selection** section in the handover is informational for agents.

## Conventions
- **Modularity:** One feature per file; thin CLI, logic in modules.
- **Errors:** Strict error handling, no silent failures.
- **Documentation:** Feature-based names in `docs/` (e.g. `cli-skeleton.md`), not `task-1.md`. Update `docs/index.md` in the update phase only.
- **Task queue:** Cyclopsctl reads complexity report only; agents use `cyclopsctl tasks` CLI for status/show/list.

## Where Things Live
| Want to... | Look in... |
|------------|------------|
| Product requirements (bootstrap / planning only — not per-cycle) | `prd.md` |
| Cyclopsctl package | `src/cyclopsctl/` (`pyproject.toml`, `pip install -e .` or global `install.ps1` / `install.sh`) |
| Global install (git/local pip) | `install.ps1`, `install.sh`, `src/cyclopsctl/installer.py`, `docs/setup/package-distribution.md` |
| Package version | `src/cyclopsctl/version.py`, `cyclopsctl --version` |
| Run configuration | `src/cyclopsctl/config.py`, `cyclopsctl.toml.example`, local `cyclopsctl.toml` |
| Config profiles | `src/cyclopsctl/profiles.py`, `docs/setup/config-profiles.md` |
| Project init (`cyclopsctl init`) | `src/cyclopsctl/cli.py` (`_init_command`), `src/cyclopsctl/project_setup.py`, `docs/setup/project-scaffold.md` |
| Scaffold helpers / templates | `src/cyclopsctl/init_scaffold.py`, `src/cyclopsctl/templates/` |
| Assess-and-setup engine | `src/cyclopsctl/project_setup.py`, `docs/setup/project-setup.md`, `docs/setup/brownfield-attach-init.md`, `docs/setup/prd-continue-init.md` |
| `.env` / API key loading | `src/cyclopsctl/env.py`, `docs/setup/env-loading.md` |
| Handover for next implementation run | `current-handover-prompt.md` |
| Handover read / prompt compose | `src/cyclopsctl/prompt.py`, `docs/workflow/prompt-handover.md`, `docs/workflow/ai-context-injection.md` |
| Handover verify (snapshot/compare) | `src/cyclopsctl/verify.py`, `docs/workflow/handover-verification.md` |
| Handover alignment (pre-agent) | `src/cyclopsctl/alignment.py`, `docs/workflow/handover-alignment.md` |
| Cycle structured logs | `src/cyclopsctl/logging.py`, `docs/runtime/structured-logging.md` |
| Rich live dashboard / post-run summary | `src/cyclopsctl/tui.py`, `docs/runtime/cycle-dashboard.md`, `docs/runtime/post-run-summary.md` |
| Agent plan panel (TodoWrite) | `runner.py` (`parse_todo_write_from_event`), `tui.py` (`AgentPlanState`, `plan_callback_for_logger`), `docs/runtime/agent-plan-panel.md` |
| TUI task queue strip | `loop.py` (`_capture_queue_snapshot`, `session_completed_ids`), `tui.py` (`TaskQueueSnapshot`, `TaskQueueStripState`, `format_queue_strip_line`), `docs/runtime/tui-queue-strip.md` |
| Native tasks CLI (`list` table, `list pending` / `done`, `show`, `next`, `set-status`, `tags`, `use-tag`) | `src/cyclopsctl/tasks/cli.py`, `docs/tasks/tasks-cli.md` |
| Task backend protocol / factory | `src/cyclopsctl/tasks/`, `docs/tasks/task-backend.md` |
| Init/launch/bootstrap backend wiring | `src/cyclopsctl/project_setup.py`, `bootstrap.py`, `launcher.py`, `docs/tasks/backend-wiring.md` |
| Native task JSON storage | `src/cyclopsctl/tasks/store.py`, `docs/tasks/task-store.md` |
| Native PRD parsing (Cursor SDK) | `src/cyclopsctl/tasks/parse_prd.py`, `src/cyclopsctl/templates/parse-prd-prompt.md`, `docs/tasks/parse-prd.md` |
| Native complexity analysis (Cursor SDK) | `src/cyclopsctl/tasks/analyze.py`, `src/cyclopsctl/templates/analyze-complexity-prompt.md`, `docs/tasks/analyze-complexity.md` |
| Task bootstrap model config | `src/cyclopsctl/tasks/models.py`, `parse_tasks_config()` in `config.py`, `[tasks]` in `cyclopsctl.toml.example`, `docs/tasks/task-models.md` |
| PRD bootstrap (parse → analyze → handover sync) | `cyclopsctl bootstrap`, `src/cyclopsctl/bootstrap.py`, `docs/setup/prd-bootstrap.md` |
| Workflow file generation from PRD | `src/cyclopsctl/workflow_gen.py`, `docs/workflow/workflow-generation.md`, `docs/workflow/workflow-readme-fallback.md`, `docs/workflow/native-workflow-templates.md` |
| Stale workflow detection / refresh | `src/cyclopsctl/workflow_gen.py`, `src/cyclopsctl/project_setup.py`, `src/cyclopsctl/doctor.py`, `docs/workflow/workflow-refresh.md` |
| Cyclopsctl Cursor rules (native) | `src/cyclopsctl/templates/cursor-rules/cyclopsctl/`, `.cursor/rules/cyclopsctl/`, `install_cyclopsctl_cursor_rules()` in `workflow_gen.py` |
| Per-cycle task selection | `src/cyclopsctl/task_selection.py`, `docs/tasks/task-selection.md` |
| Empty queue stop | `src/cyclopsctl/loop.py`, `docs/tasks/empty-queue-completion.md` |
| Model discovery | `src/cyclopsctl/models.py`, `docs/runtime/model-discovery.md` |
| Preflight diagnostics | `cyclopsctl doctor`, `src/cyclopsctl/doctor.py`, `docs/cli/doctor-cli.md` |
| Install scripts | `install.ps1`, `install.sh`, `src/cyclopsctl/installer.py` |
| Interactive launch setup | `cyclopsctl` / `launch`, `src/cyclopsctl/launcher.py`, `docs/cli/launch-cli.md` |
| Launch PRD change / new tag (path-aware `--prd`, phase continuation, bootstrap guard) | `src/cyclopsctl/project_setup.py` (`handle_launch_prd_change`, `prd_source_changed`), `src/cyclopsctl/bootstrap.py` (`_guard_destructive_reparse`), `src/cyclopsctl/launcher.py` (`format_phase_complete_hint`), `docs/cli/launch-prd-change.md` |
| Model inspection CLI | `cyclopsctl models`, `docs/cli/model-inspection-cli.md` |
| Complexity routing | `src/cyclopsctl/routing.py`, `docs/runtime/model-routing.md` |
| Agent create/send/wait | `src/cyclopsctl/runner.py`, `docs/runtime/agent-session.md` |
| Windows SDK bridge / env-fallback client | `src/cyclopsctl/sdk_bridge.py`, `docs/runtime/agent-session.md` |
| Agent run failure diagnostics (report + transcript path) | `src/cyclopsctl/failure_report.py`, `cli.py` (`_log_and_exit_agent_run`), `loop.py` (`_persist_phase_failure`), `docs/runtime/failure-diagnostics.md` |
| Per-cycle session | `src/cyclopsctl/session.py`, `docs/runtime/agent-session.md` |
| Run loop / cycle orchestration | `src/cyclopsctl/loop.py`, `docs/runtime/cycle-orchestration.md` |
| Graceful Ctrl+C interrupt | `src/cyclopsctl/interrupt.py`, `docs/runtime/graceful-interrupt.md` |
| Crash recovery state / status | `src/cyclopsctl/state.py`, `cyclopsctl status`, `docs/runtime/crash-recovery-state.md` |
| Run history / resume startup | `src/cyclopsctl/history.py`, `docs/runtime/run-history.md` (`--resume` skips completed parent tasks) |
| Run observability (dry-run, git summary, transcript export) | `src/cyclopsctl/loop.py`, `git_summary.py`, `transcript_export.py`, `docs/runtime/run-observability.md` |
| Transient SDK retry | `src/cyclopsctl/runner.py`, `src/cyclopsctl/loop.py`, `docs/runtime/transient-retry.md` |
| Post-task workspace update rules | `update-handover-prompt.md` |
| Doc map | `docs/index.md` |
| Tasks & complexity | `.cyclopsctl/tasks/tasks.json`, `.cyclopsctl/reports/complexity-report.json` |
| Manual testing runbook | `docs/guides/testing-guide.md` |
| Brownfield pytest fixtures / regression | `tests/fixtures/brownfield-*`, `tests/test_brownfield_integration.py`, `docs/testing/brownfield-regression.md` |
| Brownfield readiness diagnostics | `src/cyclopsctl/doctor.py` (`run_brownfield_readiness_checks`), `alignment.py` (`compare_handover_to_backend_next`), `launcher.py` (`format_launch_summary_line`), `docs/testing/brownfield-readiness.md` |
| Launch attach readiness | `project_setup.py` (`is_project_initialized_for_launch`, `handover_launch_ready`), `launcher.py` (`prepare_launch_workspace`), `docs/cli/launch-attach-readiness.md` |

## Project Memory
- `TaskBackend` lives in `tasks/backend.py`; shared dataclasses in `tasks/types.py`; factory is **native-only**.
- `tasks/store.py` owns native `.cyclopsctl/tasks/` persistence: tag-keyed `tasks.json`, tag state, complexity report paths, atomic writes via `state.write_atomic`, and `CircularDependencyError` on save.
- `tasks/cli.py` implements native CRUD and `cyclopsctl tasks`; `NativeTaskBackend` lazy-imports `cli` to avoid circular imports.
- `tasks/parse_prd.py` and `tasks/analyze.py` implement native PRD parse and complexity analysis via Cursor SDK.
- `resolve_project_root()` in `config.py` defaults CLI project root to cwd when `--project-root` is omitted.
- `workflow_gen.py` staleness heuristics flag outdated workflow refs; `cyclopsctl init --refresh-workflow` regenerates stale files — see `docs/workflow/workflow-refresh.md`.
- Manual validation: `docs/guides/testing-guide.md` (fresh + existing repo); pytest regression in `test_fresh_repo_integration.py` and `test_brownfield_integration.py`.
- `AgentRunError` carries `phase`, optional `diagnostic_detail` (from `runner.extract_run_error_detail` / `run.conversation_json()`), and `same_agent_continued`; `cli._log_and_exit_agent_run` prints an actionable report (result detail, optional `context:` / `recovery:` lines, resolved local transcript path via `failure_report.encode_project_slug` + agent's last actions via `summarize_transcript_tail`, ASCII-only).
- Default `retry_on = "transient"` (3 attempts, backoff 5s/15s); `--retry-on off` restores stop-on-first-failure. `CycleSession` first sends one same-agent continue on empty-detail `RunFailureKind.RUN` errors. `is_transient_agent_failure` then retries empty-detail run errors unless continue already ran **and** the diagnostic looks like a mid-work drop (last agent output / `cargo test` / full suite); run errors with SDK detail (billing, agent failure reports) are never retried.
- `run_cycles` best-effort persists `run-history.json` on mid-run failure or interrupt when ≥1 cycle verified, so `--resume` can skip completed tasks on the next invocation.
- On `AgentRunError` the loop persists `RunStateStatus.FAILED` with agent/run ids (`_persist_phase_failure`); `cyclopsctl status` shows the failed phase.
- Windows bridge: `sdk_bridge.install_env_fallback_default_client` installs a `Client(allow_api_key_env_fallback=True)` so `run.wait()` doesn't fail with `missing_api_key` on caller-supplied bridges.
- Windows bridge cleanup kills the whole process tree (`taskkill /F /T`) so agent child node/npm/test processes don't orphan; `_ACTIVE_BRIDGES` + `atexit` safety net closes leaked bridges. `ManagedBridge.close()` is idempotent.
- Bridge client uses a generous RPC timeout (`resolve_bridge_client_timeout`, default 1h, env `CYCLOPSCTL_BRIDGE_TIMEOUT_SECONDS`) so long agent runs (soak/acceptance suites) don't fail `run.wait()` with `ReadTimeout` (SDK defaults are 60s unary / 600s stream).
