# Documentation Index

> **Index rules:** This file is a documentation map only. Do not add project history, task lists, architecture overviews, or session notes. When adding docs, append a single bullet with a one-line description under the appropriate section.

## Core Context
- `ai-context.md` - Core project architecture, implementation vs update phase rules, cyclopsctl prompt selection (handover vs bootstrap), and module map.
- `prompts/setup-ai-workflow.md` - **Bootstrap only** — generate workflow files when no handover exists; never sent during normal task cycles when `current-handover-prompt.md` is ready.
- `prompts/sync-current-handover.md` - Regenerate `current-handover-prompt.md` from the native task queue after parse-prd.

## User Guides
- `guides/testing-guide.md` - Manual validation on a fresh repo and on an existing repo (greenfield, brownfield, resume); links to pytest regression.
- `guides/user-documentation.md` - Scenario-first README and technical-brief layout, plus `test_readme_docs.py` content checks.
- `guides/technical-brief.md` - Agent-oriented module map, adoption flow (install → init → bootstrap → launch), and testing approach.

## Setup & Configuration
- `setup/package-scaffold.md` - Python package layout, editable install, and `cyclopsctl` entrypoint.
- `setup/package-distribution.md` - Global install scripts (`install.ps1` / `install.sh`), git/local `pip install`, version metadata, and wheel packaging.
- `setup/cli-configuration.md` - `cyclopsctl run` flags, TOML config (`cyclopsctl.toml.example` template), path validation, and `CyclopsctlConfig`.
- `setup/config-profiles.md` - Named `[profile.*]` / `[routing_profile.*]` TOML presets, merge precedence, `--profile` on run/init, and built-in init seeding profiles.
- `setup/project-scaffold.md` - `cyclopsctl init` as the single first-time setup command: delegates to `project_setup`, fail-fast prerequisites, power-user flags, and ready messaging for `launch`.
- `setup/project-setup.md` - Idempotent assess-and-setup engine (`project_setup.py`): prerequisites, greenfield/attach/continue init modes table, repair sequence, PRD-change guard, `last-parsed-prd.json` state, and init CLI integration tests.
- `setup/env-loading.md` - Automatic project-root `.env` loading, `--no-env`, and startup order before preflight.
- `setup/prd-bootstrap.md` - `cyclopsctl bootstrap` PRD pipeline (native parse/analyze, handover sync, optional workflow generation).
- `setup/prd-continue-init.md` - `cyclopsctl init` PRD continue mode when PRD exists but queue is empty on an existing repo: `repo_has_existing_content()` heuristic, SDK cost confirm UX, `init --from-prd`/`--yes`, and same parse pipeline as greenfield.
- `setup/brownfield-attach-init.md` - `cyclopsctl init` attach mode when tasks exist without PRD: warn/confirm UX, `--attach`/`--yes`, non-destructive repairs, and skipped parse/last-parsed state.

## Workflow & Handover
- `workflow/prompt-handover.md` - Handover file read, Task ID parsing, normalization, and content hashing.
- `workflow/ai-context-injection.md` - Cyclopsctl-side prepending of `ai-context.md` to implementation prompts only; update sends handover template unchanged.
- `workflow/handover-verification.md` - Pre/post update handover snapshots, advancement rules, and `HandoverVerificationError`.
- `workflow/handover-alignment.md` - Pre-agent handover Task ID vs selected task check, warn/strict modes, and `HandoverAlignmentError`.
- `workflow/workflow-generation.md` - PRD-aware generation of `ai-context.md` (Context7, test cmd, phase rules), `update-handover-prompt.md`, and `docs/index.md` via `--with-workflow`.
- `workflow/workflow-readme-fallback.md` - README and repo-metadata context when `prd.md` is absent: context-source preference, attach-init logging, and golden-test fixtures.
- `workflow/workflow-refresh.md` - Staleness heuristics for outdated workflow files, doctor/launch upgrade signals, and selective `init --refresh-workflow` regeneration.
- `workflow/native-workflow-templates.md` - Native `cyclopsctl tasks` command references in bundled/generated workflow files and cyclopsctl Cursor rules (`.cursor/rules/cyclopsctl/`).

## Tasks
- `tasks/task-backend.md` - `TaskBackend` protocol, `get_task_backend()` factory, `NativeTaskBackend`, and shared dataclasses under `tasks/`.
- `tasks/backend-wiring.md` - Init, launch, and bootstrap wired through `get_task_backend()`; native path (CURSOR_API_KEY only) and complexity report resolution.
- `tasks/task-store.md` - Native `.cyclopsctl/tasks/` layout, atomic JSON persistence, path resolution, circular dependency validation, and `TaskStore` helpers.
- `tasks/tasks-cli.md` - `cyclopsctl tasks` native CRUD: `list` (all tasks table), `list pending` / `done` / status filters, `show`, `next`, `set-status`, JSON/plain output, and lazy-import pattern for `NativeTaskBackend`.
- `tasks/parse-prd.md` - Native PRD→tasks pipeline via Cursor SDK (`tasks/parse_prd.py`): Sonnet-class model, schema validation, repair retry, replace/append persistence, and `last-parsed-prd.json`.
- `tasks/parse-prd-task-granularity.md` - Parse-prd task granularity design notes and implementation guidance.
- `tasks/parse-prd-granularity-assessment.md` - Assessment of parse-prd granularity changes and rollout status.
- `tasks/analyze-complexity.md` - Native complexity scoring via Cursor SDK (`tasks/analyze.py`): batching/chunking, routing-compatible report write, optional task backfill, and skip-when-exists semantics.
- `tasks/task-models.md` - `[tasks]` TOML settings (`TasksConfig`), and centralized `auto` model resolution for parse (Sonnet-preferred) and analyze (Composer) in `tasks/models.py`.
- `tasks/task-selection.md` - Per-cycle task source (`handover`, `sequential`), `--task-id` pinning, and selected-vs-next warnings.
- `tasks/empty-queue-completion.md` - Empty task queue as normal run completion (exit 0, partial `--cycles` stop).

## Runtime
- `runtime/model-routing.md` - Complexity report lookup, optional `[routing]` TOML/JSON rules, composer tier, Opus gating, and `ModelRouter`.
- `runtime/model-discovery.md` - `Cursor.models.list()` wrapper, Composer tier selection (standard/fast), Opus preset detection, and `ModelCapabilities`.
- `runtime/agent-session.md` - SDK `create`/`send`/`wait` wrapper, error classification, per-cycle `CycleSession`, and the Windows SDK bridge bootstrap with its env-fallback default client (fixes `missing_api_key` on `run.wait()`).
- `runtime/cycle-orchestration.md` - Main implement → update run loop, verification gating, and CLI wiring for `cyclopsctl run`.
- `runtime/structured-logging.md` - Per-cycle text/JSONL logging, `CycleLogger`, and error context helpers.
- `runtime/cycle-dashboard.md` - Rich live terminal dashboard, live agent activity streaming (prose coalescing, width-aware wrap), `RunDashboardState`, `RichCycleLogger`, and `managed_cycle_display`.
- `runtime/agent-plan-panel.md` - Composer `TodoWrite` parsing from the SDK activity stream, `AgentPlanState`, merge/replace semantics, and Rich dashboard checklist above Recent activity.
- `runtime/tui-queue-strip.md` - Parent-task queue strip at cycle start: `TaskQueueSnapshot`/`TaskQueueStripState`, backend `list_pending` injection from `loop.py`, Rich and plain one-line summaries.
- `runtime/post-run-summary.md` - Post-run Rich/plain summary table of verified cycle outcomes and resume-skipped tasks after Live teardown.
- `runtime/run-observability.md` - `cyclopsctl run --dry-run`, optional per-cycle git diff summaries, transcript JSON sidecars, and extended post-run/JSONL fields.
- `runtime/graceful-interrupt.md` - SIGINT handling, cooperative shutdown, exit code 130, and Rich Live teardown during `cyclopsctl run`.
- `runtime/crash-recovery-state.md` - Atomic `.cyclopsctl/state.json` persistence, `cyclopsctl status`, `failed` status on agent failure, and post-crash inspection lifecycle.
- `runtime/failure-diagnostics.md` - Actionable `AgentRunError` report on exit: result detail, resolved local transcript path (`encode_project_slug`), `AgentRunError.phase`, and `failed` state persistence.
- `runtime/run-history.md` - Cross-invocation `.cyclopsctl/run-history.json`, cycle-1 handover vs bootstrap prompt selection, `--fresh`, `--resume` task-level skip of completed parent tasks, and handover validation (history warns only; does not gate handover use).
- `runtime/transient-retry.md` - Optional `--retry-on transient` for SDK send/wait blips, backoff config, and non-retryable failure boundaries.

## CLI
- `cli/model-inspection-cli.md` - `cyclopsctl models` diagnostic subcommand and routing preset report.
- `cli/doctor-cli.md` - `cyclopsctl doctor` / `check` preflight diagnostics, native checks, remediation hints, `--fix` stub `.env`, exit codes, and Rich/plain output.
- `cli/launch-cli.md` - Default `cyclopsctl` / `cyclopsctl launch` cycles-only TTY flow, auto handover repair, inferred resume/routing from config, power-user `--action` dispatch, and internal `run` spawn.
- `cli/analyze-complexity-cli.md` - `cyclopsctl analyze-complexity` standalone scoring for the active tag (no PRD parse); recovery when phase tags lack scores; `--skip-if-exists` and Windows SDK bridge.
- `cli/launch-prd-change.md` - Path-aware PRD-change detection at launch (`--prd` for new phase files or in-place `prd.md` edits), new tag creation, forced analyze on new tags, parse/analyze/handover sync, tag naming, phase-complete nudge, Windows SDK bridge on launch, and the bootstrap/init destructive-reparse guards.
- `cli/launch-attach-readiness.md` - Relaxed `cyclopsctl launch` readiness for attach repos: toml + non-empty tasks + repairable handover (no PRD/last-parsed), `prepare_launch_workspace`, and attach-aware doctor remediation.

## Testing
- `testing/fresh-repo-regression.md` - Pytest fixture (`minimal-smoke-prd.md`), mocked init/launch integration tests, and CI guidance for fresh-repo regression.
- `testing/brownfield-regression.md` - Pytest fixtures (`tests/fixtures/brownfield-*`), `test_brownfield_integration.py` mocked init/launch chains, and CI guidance for brownfield scenarios B1–B6.
- `testing/brownfield-readiness.md` - Warn-by-default doctor/launch preflight via `TaskBackend`: handover drift, stale workflow, tag mismatch, launch summary line, and git dirty check.
