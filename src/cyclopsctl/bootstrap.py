"""PRD bootstrap pipeline via native task backend."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cyclopsctl.config import (
    DEFAULT_AI_CONTEXT,
    DEFAULT_COMPLEXITY_REPORT,
    DEFAULT_CURRENT_HANDOVER,
    load_config_file,
)
from cyclopsctl.env import load_project_env
from cyclopsctl.prompt import render_synced_handover
from cyclopsctl.routing import ComplexityReport, load_complexity_report
from cyclopsctl.tasks.backend import (
    TaskBackend,
    TaskBackendConfig,
    get_task_backend,
)
from cyclopsctl.tasks.cli import TasksCliError
from cyclopsctl.tasks.store import NATIVE_COMPLEXITY_REPORT_REL
from cyclopsctl.tasks.types import TaskShowDetail
from cyclopsctl.workflow_gen import (
    WorkflowGenError,
    generate_workflow_from_bootstrap,
    normalize_force_paths,
)

DEFAULT_PRD = Path("prd.md")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

WORKFLOW_TEMPLATE_MAP: dict[str, str] = {
    "ai-context.md": "ai-context.md",
    "update-handover-prompt.md": "update-handover-prompt.md",
    "docs-index.md": "docs/index.md",
}


class BootstrapError(RuntimeError):
    """Invalid bootstrap inputs or pipeline failure."""


def _guard_destructive_reparse(config: "BootstrapConfig") -> None:
    """
    Refuse a destructive re-parse that would overwrite an existing task list.

    When the PRD differs (by path or content) from the last parse and tasks
    already exist in the target tag, replacing them would discard finished work.
    Direct the user to the non-destructive options instead.
    """
    # Lazy import: project_setup imports from this module.
    from cyclopsctl.project_setup import (
        DEFAULT_TAG,
        load_last_parsed_prd,
        prd_source_changed,
        tasks_exist_in_tag,
    )

    last = load_last_parsed_prd(config.project_root)
    if last is None:
        return
    effective_tag = config.tag or last.tag or DEFAULT_TAG
    if not tasks_exist_in_tag(config.project_root, tag=effective_tag):
        return
    prd_path = resolve_prd_path(config)
    if not prd_source_changed(config.project_root, prd_path, last=last):
        return

    try:
        prd_display = prd_path.relative_to(config.project_root).as_posix()
    except ValueError:
        prd_display = prd_path.name
    raise BootstrapError(
        f"PRD differs from the last parse and tasks already exist in tag "
        f"{effective_tag!r}. Re-parsing would replace them. Run "
        f"`cyclopsctl launch --prd {prd_display}` to start a new phase tag "
        f"(keeps history), pass `--tag <name>` to target a fresh tag, or "
        f"`--append` to add to the existing queue."
    )


@dataclass(frozen=True)
class BootstrapConfig:
    """Validated configuration for ``cyclopsctl bootstrap``."""

    project_root: Path
    from_prd: Path | None
    current_handover: Path
    complexity_report: Path
    ai_context: Path
    tag: str | None = None
    skip_analyze: bool = False
    sync_handover_only: bool = False
    append: bool = False
    with_workflow: bool = False
    force_workflow: bool = False
    force_workflow_paths: frozenset[str] = frozenset()
    tech_stack: str | None = None
    test_cmd: str | None = None


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of a bootstrap pipeline run."""

    handover_path: Path
    task_id: int | None
    copied_templates: tuple[str, ...]


def resolve_bootstrap_config(
    *,
    project_root: Path | None = None,
    from_prd: Path | None = None,
    current_handover: Path | None = None,
    complexity_report: Path | None = None,
    ai_context: Path | None = None,
    tag: str | None = None,
    skip_analyze: bool = False,
    sync_handover_only: bool = False,
    append: bool = False,
    with_workflow: bool = False,
    force_workflow: bool = False,
    force_workflow_paths: list[str] | None = None,
    tech_stack: str | None = None,
    test_cmd: str | None = None,
) -> BootstrapConfig:
    root = (project_root or Path.cwd()).resolve()
    if not root.is_dir():
        raise BootstrapError(f"Project root is not a directory: {root}")

    if complexity_report is not None:
        resolved_report = complexity_report.expanduser().resolve()
    else:
        resolved_report = (root / NATIVE_COMPLEXITY_REPORT_REL).resolve()

    return BootstrapConfig(
        project_root=root,
        from_prd=from_prd,
        current_handover=(current_handover or root / DEFAULT_CURRENT_HANDOVER).resolve(),
        complexity_report=resolved_report,
        ai_context=(ai_context or root / DEFAULT_AI_CONTEXT).resolve(),
        tag=tag.strip() if tag and tag.strip() else None,
        skip_analyze=skip_analyze,
        sync_handover_only=sync_handover_only,
        append=append,
        with_workflow=with_workflow,
        force_workflow=force_workflow,
        force_workflow_paths=normalize_force_paths(root, force_workflow_paths),
        tech_stack=tech_stack.strip() if tech_stack and tech_stack.strip() else None,
        test_cmd=test_cmd.strip() if test_cmd and test_cmd.strip() else None,
    )


def resolve_bootstrap_backend(
    config: BootstrapConfig,
) -> TaskBackend:
    """Return the native task backend implementation for bootstrap."""
    _ = config
    return get_task_backend(TaskBackendConfig())


def project_tasks_ready(project_root: Path) -> bool:
    """Return True when native task storage exists."""
    return (project_root / ".cyclopsctl/tasks").is_dir()


def resolve_prd_path(config: BootstrapConfig) -> Path:
    if config.from_prd is not None:
        candidate = config.from_prd
        if not candidate.is_absolute():
            candidate = config.project_root / candidate
        candidate = candidate.resolve()
    else:
        candidate = (config.project_root / DEFAULT_PRD).resolve()

    if not candidate.is_file():
        raise BootstrapError(f"PRD file not found: {candidate}")
    return candidate


def _project_name_from_prd(prd_path: Path) -> str:
    try:
        for line in prd_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                if title.lower().startswith("prd:"):
                    return title.split(":", 1)[1].strip()
                return title
    except OSError:
        pass
    return config_project_fallback_name(prd_path.parent)


def config_project_fallback_name(project_root: Path) -> str:
    return project_root.name or "Project"


def _tech_stack_from_existing_handover(handover_path: Path) -> str | None:
    if not handover_path.is_file():
        return None
    try:
        text = handover_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- **Tech stack:**"):
            return stripped.split(":", 1)[1].strip()
    return None


def _detect_git_branch(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return "master"
    branch = (completed.stdout or "").strip()
    return branch or "master"


def _default_tech_stack(config: BootstrapConfig, prd_path: Path | None) -> str:
    existing = _tech_stack_from_existing_handover(config.current_handover)
    if existing:
        return existing
    if prd_path is not None and prd_path.is_file():
        return "See prd.md and ai-context.md"
    return "See ai-context.md"


def copy_workflow_templates(project_root: Path) -> tuple[str, ...]:
    """Copy bundled workflow templates into the project when targets are absent."""
    if not TEMPLATES_DIR.is_dir():
        raise BootstrapError(f"Bundled workflow templates missing: {TEMPLATES_DIR}")

    copied: list[str] = []
    for template_name, destination in WORKFLOW_TEMPLATE_MAP.items():
        source = TEMPLATES_DIR / template_name
        if not source.is_file():
            raise BootstrapError(f"Bundled workflow template missing: {source}")
        target = project_root / destination
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(destination)
    return tuple(copied)


def sync_current_handover(
    config: BootstrapConfig,
    *,
    backend: TaskBackend | None = None,
) -> tuple[str, int | None]:
    resolved_backend = backend or resolve_bootstrap_backend(config)
    next_lookup = resolved_backend.get_next(
        config.project_root,
        tag=config.tag,
    )
    prd_path = resolve_prd_path(config) if config.from_prd or (config.project_root / DEFAULT_PRD).is_file() else None
    project_name = (
        _project_name_from_prd(prd_path)
        if prd_path is not None
        else config_project_fallback_name(config.project_root)
    )
    tech_stack = _default_tech_stack(config, prd_path)
    branch = _detect_git_branch(config.project_root)

    task_detail = None
    task_id: int | None = None
    complexity = None

    if next_lookup.found and next_lookup.task is not None:
        task_id = next_lookup.task.numeric_id
        task_detail = resolved_backend.show(
            config.project_root,
            str(task_id),
            tag=config.tag,
        )
        report = load_complexity_report(config.complexity_report)
        complexity = _complexity_for_task(report, task_detail)
        if complexity is not None and task_detail.complexity != complexity:
            task_detail = _replace_complexity(task_detail, complexity)

    handover_text = render_synced_handover(
        task=task_detail,
        project_name=project_name,
        project_root=config.project_root,
        tech_stack=tech_stack,
        branch=branch,
    )
    config.current_handover.parent.mkdir(parents=True, exist_ok=True)
    config.current_handover.write_text(handover_text, encoding="utf-8", newline="\n")
    return handover_text, task_id


def _complexity_for_task(report: ComplexityReport, task_detail) -> int | None:
    if task_detail.complexity is not None:
        return task_detail.complexity
    return report.score_for(task_detail.numeric_id)


def _replace_complexity(task_detail, complexity: int) -> TaskShowDetail:
    return TaskShowDetail(
        task_id=task_detail.task_id,
        title=task_detail.title,
        description=task_detail.description,
        details=task_detail.details,
        test_strategy=task_detail.test_strategy,
        priority=task_detail.priority,
        dependencies=task_detail.dependencies,
        status=task_detail.status,
        complexity=complexity,
    )


def run_bootstrap(
    config: BootstrapConfig,
    *,
    backend: TaskBackend | None = None,
) -> BootstrapResult:
    """
    Execute the PRD bootstrap pipeline.

    Pseudocode: ensure task storage → parse_prd → analyze_complexity →
    sync_current_handover → optionally_copy_workflow_templates.
    """
    copied: tuple[str, ...] = ()
    resolved_backend = backend or resolve_bootstrap_backend(config)
    try:
        if not config.sync_handover_only:
            if not config.append:
                _guard_destructive_reparse(config)
            if not project_tasks_ready(config.project_root):
                resolved_backend.init_project(config.project_root)
            prd_path = resolve_prd_path(config)
            load_project_env(config.project_root)
            resolved_backend.parse_prd(
                config.project_root,
                prd_path,
                append=config.append,
                tag=config.tag,
            )
            if not config.skip_analyze:
                resolved_backend.analyze_complexity(
                    config.project_root,
                    tag=config.tag,
                )

        _, task_id = sync_current_handover(
            config,
            backend=resolved_backend,
        )

        if config.with_workflow:
            workflow_result = generate_workflow_from_bootstrap(
                project_root=config.project_root,
                from_prd=config.from_prd,
                tech_stack=config.tech_stack,
                test_cmd=config.test_cmd,
                force_workflow=config.force_workflow,
                force_paths=sorted(config.force_workflow_paths),
            )
            copied = workflow_result.generated_paths
    except (TasksCliError, WorkflowGenError, ValueError, RuntimeError) as exc:
        raise BootstrapError(str(exc)) from exc

    return BootstrapResult(
        handover_path=config.current_handover,
        task_id=task_id,
        copied_templates=copied,
    )
