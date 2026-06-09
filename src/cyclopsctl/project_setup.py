"""Idempotent project setup assessment for first-time and repair flows (task 4)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from collections.abc import Callable, Mapping

from cyclopsctl.bootstrap import (
    BootstrapConfig,
    BootstrapError,
    resolve_prd_path,
    sync_current_handover,
)
from cyclopsctl.config import (
    DEFAULT_AI_CONTEXT,
    DEFAULT_CURRENT_HANDOVER,
    load_config_file,
)
from cyclopsctl.env import load_project_env
from cyclopsctl.init_scaffold import (
    SCAFFOLD_TEMPLATE_MAP,
    InitScaffoldError,
    _gitignore_needs_update,
    _normalize_force_paths,
    _relative_path,
    _render_template_content,
    _update_gitignore,
    _validate_profile,
    _write_config,
    discover_scaffold_plan,
)
from cyclopsctl.prompt import PromptError, parse_task_id
from cyclopsctl.tasks.backend import (
    TaskBackend,
    TaskBackendConfig,
    TaskBackendKind,
    get_task_backend,
    resolve_task_backend,
)
from cyclopsctl.tasks.cli import TasksCliError
from cyclopsctl.tasks.store import NATIVE_COMPLEXITY_REPORT_REL
from cyclopsctl.workflow_gen import (
    WorkflowGenConfig,
    detect_stale_workflow_files,
    generate_workflow_files,
    normalize_force_paths,
    refresh_stale_workflow_files,
    resolve_workflow_inputs,
)

DEFAULT_PRD = Path("prd.md")
DEFAULT_TAG = "master"
LAST_PARSED_PRD_REL = Path(".cyclopsctl/last-parsed-prd.json")
NATIVE_TASKS_DIR_REL = Path(".cyclopsctl/tasks")
NATIVE_TASKS_JSON_REL = Path(".cyclopsctl/tasks/tasks.json")
NATIVE_TAG_STATE_REL = Path(".cyclopsctl/tasks/state.json")

ENV_STUB_CONTENT = """# Cyclopsctl — add your API keys below
# Get a Cursor key from https://cursor.com/settings
CURSOR_API_KEY=
"""


def _print_setup_progress(message: str) -> None:
    print(f"cyclopsctl init: {message}", file=sys.stderr)

PRD_CHANGED_MESSAGE = (
    "PRD changed — run `cyclopsctl launch` to start a new task list."
)

INIT_REQUIRED_MESSAGE = (
    "Project is not initialized. Run `cyclopsctl init` first."
)

INIT_MISSING_TOML_MESSAGE = (
    "Missing cyclopsctl.toml. Run `cyclopsctl init --attach --yes` to scaffold "
    "from the existing task queue, or `cyclopsctl init` for a new project."
)

ATTACH_LAUNCH_INFO_MESSAGE = (
    "Brownfield attach mode: launching with existing task queue (no PRD on disk)."
)

PRD_CONTINUE_CONFIRM_MESSAGE = (
    "PRD bootstrap requires confirmation for existing repositories. "
    "Re-run with --yes to skip the interactive prompt."
)

MISSING_PRD_EXISTING_REPO_SUFFIX = (
    " Add prd.md (or use --from-prd) to bootstrap tasks from your PRD; see "
    "prd.example.md for what to include, or run `cyclopsctl init --attach --yes` "
    "if an existing task queue is present."
)

PRD_CHANGED_NO_NEW_TAG_MESSAGE = (
    "PRD changed but --no-new-tag was set. Create a tag manually or omit --no-new-tag."
)

PHASE_TAG_RE = re.compile(r"phase[-\s]?(\d+)", re.IGNORECASE)
TAG_SLUG_RE = re.compile(r"[^a-z0-9]+")


class LaunchReadinessError(RuntimeError):
    """Launch blocked because the project is not ready for cyclopsctl runs."""


class LaunchPrdChangeError(RuntimeError):
    """Launch PRD-change flow failed or was blocked by flags."""


@dataclass(frozen=True)
class LaunchPrdChangeResult:
    """Outcome when launch handles a changed PRD on a mature project."""

    active_tag: str
    new_tag_created: bool
    steps: tuple[str, ...]


class ProjectSetupError(RuntimeError):
    """Project setup prerequisites or repair failure."""


@dataclass(frozen=True)
class ProjectSetupConfig:
    """Validated configuration for project setup assessment."""

    project_root: Path
    tag: str | None = None
    prd_path: Path | None = None
    config_path: Path | None = None
    current_handover: Path | None = None
    complexity_report: Path | None = None
    ai_context: Path | None = None
    profile: str | None = None
    skip_templates: bool = False
    force_paths: frozenset[str] = frozenset()
    force_all: bool = False
    dry_run: bool = False
    fix_env: bool = False
    rules: tuple[str, ...] = ("cursor",)
    refresh_workflow: bool = False
    attach: bool = False
    assume_yes: bool = False
    bootstrap_model: str | None = None
    parse_model: str | None = None
    analyze_model: str | None = None
    max_tasks: int | None = None
    task_backend: TaskBackendKind = "native"  # type: ignore[assignment]


@dataclass(frozen=True)
class LastParsedPrd:
    """Persisted PRD parse state under ``.cyclopsctl/``."""

    path: str
    sha256: str
    tag: str
    parsed_at: str


@dataclass(frozen=True)
class ProjectSetupResult:
    """Outcome of ``run_project_setup``."""

    already_ready: bool
    repairs: tuple[str, ...]
    parsed_prd: bool
    task_id: int | None
    pending_count: int
    next_task_id: int | None
    next_task_title: str | None
    tag: str
    dry_run: bool = False
    planned_paths: tuple[str, ...] = ()
    attach_mode: bool = False
    continue_mode: bool = False


def resolve_project_setup_config(
    *,
    project_root: Path | None = None,
    tag: str | None = None,
    prd_path: Path | None = None,
    config_path: Path | None = None,
    current_handover: Path | None = None,
    complexity_report: Path | None = None,
    ai_context: Path | None = None,
    profile: str | None = None,
    skip_templates: bool = False,
    force_paths: list[str] | None = None,
    force_all: bool = False,
    dry_run: bool = False,
    fix_env: bool = False,
    rules: tuple[str, ...] = ("cursor",),
    refresh_workflow: bool = False,
    attach: bool = False,
    assume_yes: bool = False,
    bootstrap_model: str | None = None,
    parse_model: str | None = None,
    analyze_model: str | None = None,
    max_tasks: int | None = None,
) -> ProjectSetupConfig:
    root = (project_root or Path.cwd()).resolve()
    if not root.is_dir():
        raise ProjectSetupError(f"Project root is not a directory: {root}")

    normalized_tag = tag.strip() if tag and tag.strip() else None
    try:
        _validate_profile(root, profile)
    except InitScaffoldError as exc:
        raise ProjectSetupError(str(exc)) from exc

    normalized_force = _normalize_force_paths(root, force_paths)
    resolved_config_path = (
        config_path.expanduser().resolve()
        if config_path is not None
        else root / "cyclopsctl.toml"
    )
    file_cfg: dict = {}
    if resolved_config_path.is_file():
        file_cfg = load_config_file(resolved_config_path)
    task_backend = resolve_task_backend({}, file_cfg, project_root=root)

    if complexity_report is not None:
        resolved_report = complexity_report.expanduser().resolve()
    else:
        resolved_report = (root / NATIVE_COMPLEXITY_REPORT_REL).resolve()

    return ProjectSetupConfig(
        project_root=root,
        tag=normalized_tag,
        prd_path=prd_path,
        config_path=resolved_config_path,
        current_handover=(current_handover or root / DEFAULT_CURRENT_HANDOVER).resolve(),
        complexity_report=resolved_report,
        ai_context=(ai_context or root / DEFAULT_AI_CONTEXT).resolve(),
        profile=profile,
        skip_templates=skip_templates,
        force_paths=normalized_force,
        force_all=force_all,
        dry_run=dry_run,
        fix_env=fix_env,
        rules=rules,
        refresh_workflow=refresh_workflow,
        attach=attach,
        assume_yes=assume_yes,
        bootstrap_model=bootstrap_model,
        parse_model=parse_model,
        analyze_model=analyze_model,
        max_tasks=max_tasks,
        task_backend=task_backend,
    )


def _effective_tag(config: ProjectSetupConfig) -> str:
    return config.tag or DEFAULT_TAG


def resolve_setup_backend(
    config: ProjectSetupConfig,
) -> TaskBackend:
    """Return the task backend implementation for project setup."""
    _ = config
    return get_task_backend(TaskBackendConfig())


def _tasks_json_path(project_root: Path) -> Path:
    return project_root / NATIVE_TASKS_JSON_REL


def _tag_state_path(project_root: Path) -> Path:
    return project_root / NATIVE_TAG_STATE_REL


def project_tasks_initialized(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> bool:
    """Return True when native task storage is present."""
    _ = backend_kind
    return (project_root / NATIVE_TASKS_DIR_REL).is_dir()


def _expected_prd_path(config: ProjectSetupConfig) -> Path:
    """Return the PRD path init would use, without checking that the file exists."""
    if config.prd_path is not None:
        candidate = config.prd_path
        if not candidate.is_absolute():
            candidate = config.project_root / candidate
        return candidate.resolve()
    return (config.project_root / DEFAULT_PRD).resolve()


def repo_has_existing_content(project_root: Path) -> bool:
    """
    Heuristic: return True when the repo looks like ongoing work, not greenfield.

    Checks for ``src/``, ``README.md``, or a ``.git`` directory.
    """
    root = project_root.resolve()
    if (root / "src").is_dir():
        return True
    if (root / "README.md").is_file():
        return True
    if (root / ".git").exists():
        return True
    return False


def _format_missing_prd_error(expected_prd: Path, *, has_existing_content: bool) -> str:
    message = f"PRD file not found: {expected_prd}"
    if has_existing_content:
        message += MISSING_PRD_EXISTING_REPO_SUFFIX
    return message


def _confirm_prd_continue(
    config: ProjectSetupConfig,
    prd_path: Path,
    *,
    stdin_is_tty: bool | None = None,
) -> bool:
    """Warn and confirm PRD bootstrap when an existing repo has no task queue yet."""
    print(
        "cyclopsctl init: Existing repository detected; parsing PRD will use "
        "the Cursor SDK (typically 1–2 minutes; API cost applies).",
        file=sys.stderr,
    )
    print(f"cyclopsctl init: PRD: {prd_path}", file=sys.stderr)
    if config.assume_yes:
        return True

    is_tty = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    if not is_tty:
        raise ProjectSetupError(PRD_CONTINUE_CONFIRM_MESSAGE)

    try:
        answer = input("Parse PRD into tasks? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _confirm_attach_continue(
    config: ProjectSetupConfig,
    expected_prd: Path,
    *,
    stdin_is_tty: bool | None = None,
) -> bool:
    """Warn and confirm brownfield attach when tasks exist but the PRD is missing."""
    print(
        f"cyclopsctl init: Warning: PRD file not found: {expected_prd}",
        file=sys.stderr,
    )
    print(
        "cyclopsctl init: Existing tasks detected; attach mode repairs scaffold "
        "and syncs handover without re-parsing.",
        file=sys.stderr,
    )
    if config.attach or config.assume_yes:
        return True

    is_tty = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    if not is_tty:
        raise ProjectSetupError(
            f"PRD file not found: {expected_prd}. "
            "Re-run with --attach --yes to attach without a PRD."
        )

    try:
        answer = input("Continue attaching without PRD? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _resolve_prd(config: ProjectSetupConfig) -> Path:
    expected = _expected_prd_path(config)
    if not expected.is_file():
        raise ProjectSetupError(f"PRD file not found: {expected}")
    return expected


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_last_parsed_prd(project_root: Path) -> LastParsedPrd | None:
    """Load persisted parse state when present."""
    path = project_root / LAST_PARSED_PRD_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectSetupError(
            f"Invalid last-parsed PRD state: {path}"
        ) from exc
    if not isinstance(data, dict):
        raise ProjectSetupError(f"Invalid last-parsed PRD state: {path}")

    required = ("path", "sha256", "tag", "parsed_at")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ProjectSetupError(
            f"last-parsed PRD state missing fields: {', '.join(missing)}"
        )

    return LastParsedPrd(
        path=str(data["path"]),
        sha256=str(data["sha256"]),
        tag=str(data["tag"]),
        parsed_at=str(data["parsed_at"]),
    )


def write_last_parsed_prd(
    project_root: Path,
    prd_path: Path,
    *,
    tag: str,
) -> Path:
    """Persist PRD parse metadata after a successful first parse."""
    try:
        relative = prd_path.relative_to(project_root)
        stored_path = relative.as_posix()
    except ValueError:
        stored_path = prd_path.name

    record = {
        "path": stored_path,
        "sha256": sha256_file(prd_path),
        "tag": tag,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
    }
    destination = project_root / LAST_PARSED_PRD_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(record, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination


def tasks_exist_in_tag(
    project_root: Path,
    *,
    tag: str | None = None,
    backend_kind: TaskBackendKind | None = None,
) -> bool:
    """Return True when native ``tasks.json`` contains tasks for the given tag."""
    _ = backend_kind
    backend = get_task_backend(TaskBackendConfig())
    return backend.tasks_exist(project_root, tag=tag)


def load_current_tag(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> str | None:
    """Return the active tag from native tag state when set."""
    _ = backend_kind
    state_path = _tag_state_path(project_root)
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    current = data.get("currentTag")
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def resolve_backfill_tag(
    project_root: Path,
    *,
    explicit_tag: str | None = None,
) -> str | None:
    """Pick a tag to record in last-parsed-prd when backfilling missing parse state."""
    if explicit_tag and tasks_exist_in_tag(project_root, tag=explicit_tag):
        return explicit_tag

    current = load_current_tag(project_root)
    if current and tasks_exist_in_tag(project_root, tag=current):
        return current

    if tasks_exist_in_tag(project_root, tag=DEFAULT_TAG):
        return DEFAULT_TAG

    for tag_name in sorted(list_existing_tags(project_root)):
        if tasks_exist_in_tag(project_root, tag=tag_name):
            return tag_name
    return None


def backfill_last_parsed_prd_if_missing(
    project_root: Path,
    prd_path: Path,
    *,
    tag: str | None = None,
) -> bool:
    """
    Record parse state for mature projects missing ``last-parsed-prd.json``.

    Returns True when a new state file was written.
    """
    if load_last_parsed_prd(project_root) is not None:
        return False
    if not is_project_initialized_for_launch(project_root):
        return False
    if not prd_path.is_file():
        return False

    backfill_tag = resolve_backfill_tag(project_root, explicit_tag=tag)
    if backfill_tag is None:
        return False

    write_last_parsed_prd(project_root, prd_path, tag=backfill_tag)
    return True


def prd_changed_with_existing_tasks(
    project_root: Path,
    prd_path: Path,
    *,
    tag: str | None = None,
) -> bool:
    """
    Return True when tasks already exist and the PRD hash differs from last parse.

    Used to refuse destructive re-parse during setup; launch handles new tags.
    """
    last = load_last_parsed_prd(project_root)
    effective_tag = tag or (last.tag if last is not None else DEFAULT_TAG)
    if not tasks_exist_in_tag(project_root, tag=effective_tag):
        return False
    if last is None:
        return False
    return sha256_file(prd_path) != last.sha256


def project_has_non_empty_tasks(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> bool:
    """Return True when native storage contains at least one queued task."""
    _ = backend_kind
    tags = list_existing_tags(project_root)
    if tags:
        return any(
            tasks_exist_in_tag(project_root, tag=tag_name)
            for tag_name in tags
        )
    return tasks_exist_in_tag(project_root)


def is_brownfield_attach_context(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> bool:
    """Return True when tasks exist but the default PRD file is absent."""
    root = project_root.resolve()
    if (root / DEFAULT_PRD).is_file():
        return False
    return project_has_non_empty_tasks(root, backend_kind=backend_kind)


def _resolve_readiness_backend(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> TaskBackend:
    _ = (project_root, backend_kind)
    return get_task_backend(TaskBackendConfig())


def handover_launch_ready(
    project_root: Path,
    *,
    current_handover: Path | None = None,
    update_handover: Path | None = None,
    ai_context: Path | None = None,
    backend_kind: TaskBackendKind | None = None,
    tag: str | None = None,
) -> bool:
    """Return True when workflow handover files exist or can be synced at launch."""
    root = project_root.resolve()
    handover_path = current_handover or (root / DEFAULT_CURRENT_HANDOVER)
    update_path = update_handover or (root / "update-handover-prompt.md")
    ai_path = ai_context or (root / DEFAULT_AI_CONTEXT)

    if handover_path.is_file() and update_path.is_file() and ai_path.is_file():
        return True

    if not project_has_non_empty_tasks(root, backend_kind=backend_kind):
        return False

    backend = _resolve_readiness_backend(root, backend_kind=backend_kind)
    try:
        pending = backend.list_pending(root, tag=tag)
        next_lookup = backend.get_next(root, tag=tag)
    except (TasksCliError, ValueError, RuntimeError):
        return False
    return len(pending) > 0 and next_lookup.found


def format_init_required_message(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> str:
    """Return remediation text for uninitialized vs attach-ready projects."""
    root = project_root.resolve()
    has_toml = (root / "cyclopsctl.toml").is_file()
    has_tasks = project_has_non_empty_tasks(root, backend_kind=backend_kind)
    if has_tasks and not has_toml:
        return INIT_MISSING_TOML_MESSAGE
    return INIT_REQUIRED_MESSAGE


def is_project_initialized_for_launch(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
    current_handover: Path | None = None,
    update_handover: Path | None = None,
    ai_context: Path | None = None,
    tag: str | None = None,
) -> bool:
    """Return True when launch can proceed without greenfield parse/bootstrap state."""
    root = project_root.resolve()
    if not (root / "cyclopsctl.toml").is_file():
        return False
    _ = backend_kind
    if not project_has_non_empty_tasks(root):
        return False
    return handover_launch_ready(
        root,
        current_handover=current_handover,
        update_handover=update_handover,
        ai_context=ai_context,
        tag=tag,
    )


def list_existing_tags(
    project_root: Path,
    *,
    backend_kind: TaskBackendKind | None = None,
) -> frozenset[str]:
    """Return tag names present in native ``tasks.json``."""
    _ = backend_kind
    tasks_path = _tasks_json_path(project_root)
    if not tasks_path.is_file():
        return frozenset()
    try:
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    return frozenset(str(key) for key in data)


def slugify_tag_name(text: str, *, max_length: int = 48) -> str:
    """Convert arbitrary text into a task tag slug."""
    slug = TAG_SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug


def _prd_has_title_heading(prd_path: Path) -> bool:
    try:
        for line in prd_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                return True
    except OSError:
        pass
    return False


def _tag_slug_from_prd_title(prd_path: Path, project_root: Path) -> str:
    from cyclopsctl.bootstrap import _project_name_from_prd, config_project_fallback_name

    if not _prd_has_title_heading(prd_path):
        return ""

    title = _project_name_from_prd(prd_path)
    if title == config_project_fallback_name(project_root):
        return ""

    phase_match = PHASE_TAG_RE.search(title)
    if phase_match:
        return f"phase-{phase_match.group(1)}"
    return slugify_tag_name(title)


def propose_tag_name(
    project_root: Path,
    prd_path: Path,
    *,
    now: datetime | None = None,
) -> str:
    """
    Propose a unique tag name from the PRD title or a date-based fallback.

    Prefers ``phase-N`` when the title mentions a phase number; otherwise
    slugifies the PRD title. Falls back to ``prd-YYYY-MM-DD`` when needed.
    """
    existing = list_existing_tags(project_root)
    slug = _tag_slug_from_prd_title(prd_path, project_root)
    if not slug:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        slug = f"prd-{stamp}"

    base = slug
    counter = 2
    while slug in existing:
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def prd_hash_changed(project_root: Path, prd_path: Path) -> bool:
    """Return True when ``prd_path`` differs from the last parsed PRD hash."""
    last = load_last_parsed_prd(project_root)
    if last is None:
        return False
    return sha256_file(prd_path) != last.sha256


def _stored_prd_relpath(project_root: Path, prd_path: Path) -> str:
    """Return the PRD path as stored in ``last-parsed-prd.json`` (posix relative)."""
    try:
        return prd_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return prd_path.name


def prd_source_changed(
    project_root: Path,
    prd_path: Path,
    *,
    last: LastParsedPrd | None = None,
) -> bool:
    """
    Return True when ``prd_path`` differs from the last parsed PRD by path or hash.

    A different file path (e.g. ``prd-phase2.md`` vs ``prd.md``) counts as a
    change even when its content hash is unrelated, so pointing launch at a new
    PRD file starts a fresh phase tag.
    """
    resolved_last = last if last is not None else load_last_parsed_prd(project_root)
    if resolved_last is None:
        return False
    if _stored_prd_relpath(project_root, prd_path) != resolved_last.path:
        return True
    return sha256_file(prd_path) != resolved_last.sha256


def handle_launch_prd_change(
    project_root: Path,
    *,
    prd_path: Path | None = None,
    current_handover: Path,
    complexity_report: Path,
    ai_context: Path,
    explicit_tag: str | None = None,
    no_new_tag: bool = False,
    assume_yes: bool = False,
    tag_prompt: Callable[[str], str | None] | None = None,
    backend: TaskBackend | None = None,
    task_backend: TaskBackendKind | None = None,
) -> LaunchPrdChangeResult | None:
    """
    Detect PRD changes at launch and create a fresh task tag when needed.

    Returns ``None`` when the PRD is unchanged and no launch-side work is required.
    Raises ``LaunchReadinessError`` when the project is not initialized.
    Raises ``LaunchPrdChangeError`` when ``--no-new-tag`` blocks auto tag creation.
    """
    root = project_root.resolve()
    _ = task_backend
    resolved_backend = backend or get_task_backend(TaskBackendConfig())
    if not is_project_initialized_for_launch(root):
        raise LaunchReadinessError(INIT_REQUIRED_MESSAGE)

    explicit_prd: Path | None = None
    if prd_path is not None:
        explicit_prd = prd_path if prd_path.is_absolute() else (root / prd_path).resolve()

    last = load_last_parsed_prd(root)
    if last is None:
        # Backfill from the canonical default PRD when present so an explicit
        # ``--prd new-file.md`` is still recognized as a phase change afterwards.
        default_prd = root / DEFAULT_PRD
        backfill_src = explicit_prd or (default_prd if default_prd.is_file() else None)
        if backfill_src is not None and backfill_src.is_file():
            backfill_last_parsed_prd_if_missing(
                root,
                backfill_src,
                tag=explicit_tag,
            )
        last = load_last_parsed_prd(root)
        if last is None:
            return None

    # Bare launch tracks the PRD that produced the current tag; an explicit
    # --prd points at a (possibly new) file to start the next phase.
    if explicit_prd is not None:
        resolved_prd = explicit_prd
    else:
        resolved_prd = (root / last.path).resolve()
    if not resolved_prd.is_file():
        raise LaunchReadinessError(f"PRD file not found: {resolved_prd}")

    if not prd_source_changed(root, resolved_prd, last=last):
        return None

    if not tasks_exist_in_tag(root, tag=last.tag):
        raise LaunchReadinessError(INIT_REQUIRED_MESSAGE)

    if no_new_tag:
        raise LaunchPrdChangeError(PRD_CHANGED_NO_NEW_TAG_MESSAGE)

    if explicit_tag:
        new_tag = explicit_tag.strip()
        create_tag = new_tag not in list_existing_tags(root)
    else:
        new_tag = propose_tag_name(root, resolved_prd)
        create_tag = True
        if tag_prompt is not None and not assume_yes:
            prompted = tag_prompt(new_tag)
            if prompted is None:
                raise LaunchPrdChangeError("PRD-change tag creation cancelled.")
            new_tag = prompted.strip() or new_tag

    steps: list[str] = []

    try:
        if create_tag:
            resolved_backend.add_tag(root, new_tag)
            steps.append("add-tag")
        resolved_backend.use_tag(root, new_tag)
        steps.append("use-tag")
        load_project_env(root)
        resolved_backend.parse_prd(root, resolved_prd, tag=new_tag)
        steps.append("parse-prd")
        resolved_backend.analyze_complexity(
            root,
            tag=new_tag,
            skip_if_exists=False,
        )
        steps.append("analyze-complexity")

        workflow_result = generate_workflow_files(
            WorkflowGenConfig(
                project_root=root,
                prd_path=resolved_prd,
                force_workflow=True,
            )
        )
        if workflow_result.generated_paths:
            steps.append("workflow refresh")

        bootstrap_cfg = BootstrapConfig(
            project_root=root,
            from_prd=resolved_prd,
            current_handover=current_handover,
            complexity_report=complexity_report,
            ai_context=ai_context,
            tag=new_tag,
        )
        sync_current_handover(bootstrap_cfg, backend=resolved_backend)
        steps.append("handover sync")

        write_last_parsed_prd(root, resolved_prd, tag=new_tag)
        steps.append("last-parsed-prd")
    except (TasksCliError, BootstrapError, ValueError) as exc:
        raise LaunchPrdChangeError(str(exc)) from exc

    return LaunchPrdChangeResult(
        active_tag=new_tag,
        new_tag_created=create_tag,
        steps=tuple(steps),
    )


def _api_key_available(*, env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return bool(source.get("CURSOR_API_KEY", "").strip())


def _ensure_env(
    project_root: Path,
    *,
    fix: bool,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    env_path = project_root / ".env"
    if env_path.is_file() or _api_key_available(env=env):
        return ()

    if fix:
        env_path.write_text(ENV_STUB_CONTENT, encoding="utf-8", newline="\n")
        return (".env",)

    raise ProjectSetupError(
        "CURSOR_API_KEY is not set. Create `.env` in the project root with "
        "CURSOR_API_KEY=..., or run with --fix to create a stub `.env`."
    )


def _config_relative_path(config: ProjectSetupConfig) -> str:
    return _relative_path(config.project_root, config.config_path)


def _should_write_scaffold_path(
    config: ProjectSetupConfig,
    *,
    relative_path: str,
    exists: bool,
) -> bool:
    if not exists:
        return True
    return config.force_all or relative_path in config.force_paths


def _ensure_scaffold(config: ProjectSetupConfig) -> tuple[str, ...]:
    """Create missing cyclopsctl.toml, gitignore entries, and handover template."""
    repairs: list[str] = []
    root = config.project_root
    config_rel = _config_relative_path(config)

    if _should_write_scaffold_path(
        config,
        relative_path=config_rel,
        exists=config.config_path.is_file(),
    ):
        _write_config(
            project_root=root,
            destination=config.config_path,
            profile=config.profile,
        )
        repairs.append(config_rel)

    if config.skip_templates:
        return tuple(repairs)

    gitignore_path = root / ".gitignore"
    if _gitignore_needs_update(gitignore_path):
        updated = _update_gitignore(root)
        if updated is not None:
            repairs.append(updated)

    for template_name, destination in SCAFFOLD_TEMPLATE_MAP.items():
        if destination in {"ai-context.md", "update-handover-prompt.md"}:
            continue
        target = root / destination
        if not _should_write_scaffold_path(
            config,
            relative_path=destination,
            exists=target.is_file(),
        ):
            continue
        content = _render_template_content(template_name, project_root=root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        repairs.append(destination)

    return tuple(repairs)


def _plan_scaffold_repairs(config: ProjectSetupConfig) -> tuple[str, ...]:
    """Return scaffold paths that would be written without mutating the filesystem."""
    planned = discover_scaffold_plan(
        project_root=config.project_root,
        config_path=config.config_path,
        skip_templates=config.skip_templates,
    )
    repairs: list[str] = []
    config_rel = _config_relative_path(config)
    for item in planned:
        if item == config_rel:
            if _should_write_scaffold_path(
                config,
                relative_path=config_rel,
                exists=config.config_path.is_file(),
            ):
                repairs.append(config_rel)
            continue
        if config.skip_templates and item != config_rel:
            continue
        if item == ".gitignore":
            if _gitignore_needs_update(config.project_root / ".gitignore"):
                repairs.append(item)
            continue
        target = config.project_root / item
        if _should_write_scaffold_path(
            config,
            relative_path=item,
            exists=target.is_file(),
        ):
            repairs.append(item)
    return tuple(repairs)


def _handover_likely_needs_sync(handover_path: Path) -> bool:
    """Return True when handover is missing or still at the bootstrap placeholder."""
    if not handover_path.is_file():
        return True
    try:
        raw = handover_path.read_text(encoding="utf-8")
    except OSError:
        return True
    try:
        task_id = parse_task_id(raw, required=False)
    except PromptError:
        return True
    return task_id is None or task_id == 0


def handover_needs_sync(
    handover_path: Path,
    *,
    next_lookup,
) -> bool:
    if not handover_path.is_file():
        return True
    try:
        raw = handover_path.read_text(encoding="utf-8")
    except OSError:
        return True

    try:
        task_id = parse_task_id(raw, required=False)
    except PromptError:
        return True

    if task_id is None or task_id == 0:
        return next_lookup.found

    if next_lookup.found and next_lookup.task is not None:
        return task_id != next_lookup.task.numeric_id
    return False


def _to_bootstrap_config(
    config: ProjectSetupConfig,
    prd_path: Path | None,
) -> BootstrapConfig:
    return BootstrapConfig(
        project_root=config.project_root,
        from_prd=prd_path,
        current_handover=config.current_handover,
        complexity_report=config.complexity_report,
        ai_context=config.ai_context,
        tag=config.tag,
    )


def _plan_workflow_file_repairs(
    config: ProjectSetupConfig,
    *,
    prd_path: Path | None,
) -> tuple[str, ...]:
    """Return workflow paths that would be written by ``generate_workflow_files``."""
    from cyclopsctl.workflow_gen import (
        WORKFLOW_TEMPLATE_MAP,
        _should_generate_docs_index,
        _should_generate_workflow_file,
    )

    force_paths = normalize_force_paths(
        config.project_root,
        sorted(config.force_paths),
    )
    planned: list[str] = []
    root = config.project_root
    for _template_name, relative_path in WORKFLOW_TEMPLATE_MAP.items():
        target = root / relative_path
        if relative_path == "docs/index.md":
            should_write = _should_generate_docs_index(
                target,
                force_workflow=config.force_all,
                force_paths=force_paths,
            )
        else:
            should_write = _should_generate_workflow_file(
                target,
                relative_path=relative_path,
                force_workflow=config.force_all,
                force_paths=force_paths,
            )
        if should_write:
            planned.append(relative_path)
    return tuple(planned)


def _finalize_project_setup(
    config: ProjectSetupConfig,
    *,
    resolved_backend: TaskBackend,
    repairs: list[str],
    parsed_prd: bool,
    tag: str,
    prd_path: Path | None,
    attach_mode: bool,
    continue_mode: bool = False,
) -> ProjectSetupResult:
    bootstrap_cfg = _to_bootstrap_config(config, prd_path)

    try:
        next_lookup = resolved_backend.get_next(
            config.project_root,
            tag=config.tag,
        )
        pending = resolved_backend.list_pending(
            config.project_root,
            tag=config.tag,
        )
    except (TasksCliError, ValueError, RuntimeError) as exc:
        raise ProjectSetupError(str(exc)) from exc

    if handover_needs_sync(config.current_handover, next_lookup=next_lookup):
        try:
            _, task_id = sync_current_handover(
                bootstrap_cfg,
                backend=resolved_backend,
            )
        except Exception as exc:
            raise ProjectSetupError(str(exc)) from exc
        repairs.append("handover sync")
    else:
        task_id = (
            next_lookup.task.numeric_id
            if next_lookup.found and next_lookup.task is not None
            else None
        )
        if config.current_handover.is_file():
            try:
                raw = config.current_handover.read_text(encoding="utf-8")
                task_id = parse_task_id(raw, required=False)
            except (OSError, PromptError):
                task_id = None

    next_task_id = (
        next_lookup.task.numeric_id
        if next_lookup.found and next_lookup.task is not None
        else None
    )
    next_task_title = (
        next_lookup.task.title
        if next_lookup.found and next_lookup.task is not None
        else None
    )

    return ProjectSetupResult(
        already_ready=not repairs,
        repairs=tuple(repairs),
        parsed_prd=parsed_prd,
        task_id=task_id,
        pending_count=len(pending),
        next_task_id=next_task_id,
        next_task_title=next_task_title,
        tag=tag,
        attach_mode=attach_mode,
        continue_mode=continue_mode,
    )


def run_project_setup(
    config: ProjectSetupConfig,
    *,
    backend: TaskBackend | None = None,
    env: Mapping[str, str] | None = None,
    stdin_is_tty: bool | None = None,
) -> ProjectSetupResult:
    """
    Assess project readiness and repair missing pieces idempotently.

    Pseudocode: check prerequisites → ensure cyclopsctl.toml/gitignore →
    ensure task storage → maybe parse → maybe analyze → sync handover →
    write last-parsed-prd.
    """
    tag = _effective_tag(config)
    repairs: list[str] = []
    resolved_backend = backend or resolve_setup_backend(config)

    load_project_env(config.project_root)
    repairs.extend(_ensure_env(config.project_root, fix=config.fix_env, env=env))

    expected_prd = _expected_prd_path(config)
    prd_missing = not expected_prd.is_file()
    has_tasks = tasks_exist_in_tag(config.project_root, tag=config.tag)
    has_existing_content = repo_has_existing_content(config.project_root)

    if prd_missing and not has_tasks:
        raise ProjectSetupError(
            _format_missing_prd_error(
                expected_prd,
                has_existing_content=has_existing_content,
            )
        )

    attach_mode = prd_missing and has_tasks
    continue_mode = False
    prd_path: Path | None = None

    if attach_mode:
        if not _confirm_attach_continue(
            config,
            expected_prd,
            stdin_is_tty=stdin_is_tty,
        ):
            raise ProjectSetupError(
                f"Attach cancelled; PRD file not found: {expected_prd}"
            )
        _print_setup_progress(
            "Brownfield attach mode: continuing with existing task queue (no PRD)"
        )
        workflow_inputs = resolve_workflow_inputs(
            project_root=config.project_root,
            prd_path=None,
        )
        print(
            "cyclopsctl init: Workflow context source: "
            f"{workflow_inputs.context_source_label}",
            file=sys.stderr,
        )
    else:
        prd_path = _resolve_prd(config)
        if prd_changed_with_existing_tasks(
            config.project_root,
            prd_path,
            tag=config.tag,
        ):
            raise ProjectSetupError(PRD_CHANGED_MESSAGE)
        if not has_tasks and has_existing_content:
            continue_mode = True
            if not _confirm_prd_continue(
                config,
                prd_path,
                stdin_is_tty=stdin_is_tty,
            ):
                raise ProjectSetupError("PRD bootstrap cancelled.")
            _print_setup_progress(
                "PRD continue mode: parsing PRD into tasks for existing repository"
            )

    if config.dry_run:
        planned = list(_plan_scaffold_repairs(config))
        if not attach_mode and not project_tasks_initialized(config.project_root):
            planned.append("native tasks init")
            from cyclopsctl.workflow_gen import (
                cyclopsctl_cursor_rules_present,
                cyclopsctl_skill_present,
            )

            if "cursor" in config.rules and not cyclopsctl_cursor_rules_present(
                config.project_root
            ):
                planned.append("cyclopsctl rules")
            if "cursor" in config.rules and not cyclopsctl_skill_present(
                config.project_root
            ):
                planned.append("cyclopsctl skill")
        if not attach_mode and not has_tasks:
            planned.extend(("parse-prd", "analyze-complexity"))
        elif not config.complexity_report.is_file():
            planned.append("analyze-complexity")
        planned.extend(_plan_workflow_file_repairs(config, prd_path=prd_path))
        if _handover_likely_needs_sync(config.current_handover):
            planned.append("handover sync")
        if config.refresh_workflow:
            stale_entries = detect_stale_workflow_files(
                config.project_root,
                ai_context=config.ai_context,
                current_handover=config.current_handover,
            )
            for entry in stale_entries:
                if entry.relative_path != "current-handover-prompt.md":
                    planned.append(f"workflow refresh: {entry.relative_path}")
        unique_planned = tuple(dict.fromkeys(planned))
        return ProjectSetupResult(
            already_ready=not unique_planned,
            repairs=unique_planned,
            parsed_prd=False,
            task_id=None,
            pending_count=0,
            next_task_id=None,
            next_task_title=None,
            tag=tag,
            dry_run=True,
            planned_paths=unique_planned,
            attach_mode=attach_mode,
            continue_mode=continue_mode,
        )

    repairs.extend(_ensure_scaffold(config))

    if not project_tasks_initialized(config.project_root):
        resolved_backend.init_project(config.project_root, rules=config.rules)
        repairs.append("native tasks init")
        if "cursor" in config.rules:
            repairs.append("cyclopsctl rules")
            repairs.append("cyclopsctl skill")

    workflow_result = generate_workflow_files(
        WorkflowGenConfig(
            project_root=config.project_root,
            prd_path=prd_path,
            force_workflow=config.force_all,
            force_paths=normalize_force_paths(
                config.project_root,
                sorted(config.force_paths),
            ),
        )
    )
    repairs.extend(workflow_result.generated_paths)
    if workflow_result.installed_rule_paths and "cyclopsctl rules" not in repairs:
        repairs.append("cyclopsctl rules")
    if workflow_result.installed_skill_paths and "cyclopsctl skill" not in repairs:
        repairs.append("cyclopsctl skill")

    if config.refresh_workflow:
        refresh_result = refresh_stale_workflow_files(
            WorkflowGenConfig(
                project_root=config.project_root,
                prd_path=prd_path,
            )
        )
        if refresh_result.updated_paths:
            _print_setup_progress(
                "Workflow refresh updated: "
                + ", ".join(refresh_result.updated_paths)
            )
            repairs.extend(
                f"workflow refresh: {path}" for path in refresh_result.updated_paths
            )
        if refresh_result.skipped_paths:
            _print_setup_progress(
                "Workflow refresh skipped (not stale): "
                + ", ".join(refresh_result.skipped_paths)
            )

    parsed_prd = False
    if not attach_mode and backfill_last_parsed_prd_if_missing(
        config.project_root,
        prd_path,
        tag=tag,
    ):
        repairs.append("last-parsed-prd")

    bootstrap_settings = None
    if not attach_mode and (not has_tasks or not config.complexity_report.is_file()):
        from cyclopsctl.tasks.bootstrap_model import (
            persist_bootstrap_tasks_settings,
            resolve_bootstrap_tasks_settings,
        )

        config_path = config.config_path
        file_cfg: dict = {}
        if config_path is not None and config_path.is_file():
            file_cfg = load_config_file(config_path)

        bootstrap_settings = resolve_bootstrap_tasks_settings(
            bootstrap_model=config.bootstrap_model,
            parse_model=config.parse_model,
            analyze_model=config.analyze_model,
            max_tasks=config.max_tasks,
            file_cfg=file_cfg,
            stdin_is_tty=stdin_is_tty,
            interactive=not config.assume_yes and not config.dry_run,
        )
        if not config.dry_run and config_path is not None and config_path.is_file():
            persist_bootstrap_tasks_settings(
                config_path,
                bootstrap_settings,
            )
            _print_setup_progress(
                "Bootstrap model: "
                f"{bootstrap_settings.parse_model!r} "
                "(PRD parse + complexity analysis)"
            )

    if not attach_mode and not has_tasks:
        try:
            _print_setup_progress(
                "Parsing PRD into tasks (AI call — typically 30–90 seconds)..."
            )
            resolved_backend.parse_prd(
                config.project_root,
                prd_path,
                tag=config.tag,
                parse_model=(
                    bootstrap_settings.parse_model
                    if bootstrap_settings is not None
                    else None
                ),
                max_tasks=(
                    bootstrap_settings.max_tasks
                    if bootstrap_settings is not None
                    else None
                ),
            )
            _print_setup_progress(
                "Analyzing task complexity (AI call — typically 30–60 seconds)..."
            )
            resolved_backend.analyze_complexity(
                config.project_root,
                tag=config.tag,
                analyze_model=(
                    bootstrap_settings.analyze_model
                    if bootstrap_settings is not None
                    else None
                ),
            )
        except (TasksCliError, ValueError, RuntimeError) as exc:
            raise ProjectSetupError(str(exc)) from exc
        write_last_parsed_prd(config.project_root, prd_path, tag=tag)
        repairs.extend(("parse-prd", "analyze-complexity"))
        parsed_prd = True
        has_tasks = True
    elif not config.complexity_report.is_file():
        try:
            _print_setup_progress(
                "Analyzing task complexity (AI call — typically 30–60 seconds)..."
            )
            resolved_backend.analyze_complexity(
                config.project_root,
                tag=config.tag,
                analyze_model=(
                    bootstrap_settings.analyze_model
                    if bootstrap_settings is not None
                    else None
                ),
            )
        except (TasksCliError, ValueError, RuntimeError) as exc:
            raise ProjectSetupError(str(exc)) from exc
        repairs.append("analyze-complexity")

    return _finalize_project_setup(
        config,
        resolved_backend=resolved_backend,
        repairs=repairs,
        parsed_prd=parsed_prd,
        tag=tag,
        prd_path=prd_path,
        attach_mode=attach_mode,
        continue_mode=continue_mode,
    )


def format_ready_message(result: ProjectSetupResult) -> str:
    """Human-readable completion message for setup."""
    if result.already_ready:
        prefix = "Already ready."
    else:
        prefix = "Ready."

    lines = [
        f"{prefix} Run `cyclopsctl launch` when you want to start building.",
        f"  Tag: {result.tag}",
        f"  Pending tasks: {result.pending_count}",
    ]
    if result.next_task_id is not None and result.next_task_title:
        lines.append(
            f"  Next task: {result.next_task_id} — {result.next_task_title}"
        )
    elif result.pending_count == 0:
        lines.append("  Next task: (queue complete)")
        lines.append(
            "  Phase complete — start the next phase with "
            "`cyclopsctl launch --prd <new-prd.md>` to parse a new PRD into a "
            "fresh tag (keeps this tag's history)."
        )
    return "\n".join(lines)
