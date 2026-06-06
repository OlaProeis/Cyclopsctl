"""Project scaffold for ``cyclopsctl init`` (config, templates, gitignore)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyclopsctl.bootstrap import config_project_fallback_name
from cyclopsctl.config import ConfigError
from cyclopsctl.profiles import (
    PROFILE_SECTION,
    ROUTING_PROFILE_SECTION,
    merge_effective_file_config,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"

SCAFFOLD_TEMPLATE_MAP: dict[str, str] = {
    "ai-context.md": "ai-context.md",
    "current-handover-prompt.md": "current-handover-prompt.md",
    "update-handover-prompt.md": "update-handover-prompt.md",
}

GITIGNORE_ENTRIES: tuple[str, ...] = (
    "cyclopsctl.toml",
    ".cyclopsctl/",
    ".env",
)

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "solo-default": {
        "cycles": 5,
        "task_source": "handover",
        "default_model": "composer-2.5",
    },
    "daytime-fast": {
        "composer_tier": "fast",
        "routing_profile": "fast-default",
        "retry_on": "transient",
    },
    "overnight-quality": {
        "composer_tier": "standard",
        "routing_profile": "quality-default",
        "retry_on": "transient",
        "retry_max_attempts": 3,
    },
    "low-credits": {
        "composer_tier": "fast",
        "routing_profile": "fast-default",
        "opus_enabled": False,
    },
}

BUILTIN_ROUTING_PROFILES: dict[str, dict[str, Any]] = {
    "fast-default": {
        "composer_tier": "fast",
        "opus_enabled": False,
    },
    "quality-default": {
        "composer_tier": "standard",
        "opus_enabled": True,
    },
}


class InitScaffoldError(ConfigError):
    """Invalid init scaffold inputs or filesystem conflict."""


@dataclass(frozen=True)
class InitScaffoldResult:
    """Outcome of ``cyclopsctl init``."""

    project_root: Path
    config_path: Path
    profile: str | None
    written_paths: tuple[str, ...] = ()
    dry_run: bool = False
    planned_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ScaffoldTarget:
    relative_path: str
    absolute_path: Path
    exists: bool


def _require_absolute_project_root(value: Path) -> Path:
    resolved = value.expanduser().resolve()
    if not resolved.is_absolute():
        raise InitScaffoldError(
            f"project-root must be an absolute path (got: {value}). "
            "Use a full path such as G:/DEV/MyProject or /home/user/project."
        )
    if not resolved.is_dir():
        raise InitScaffoldError(f"project-root is not a directory: {resolved}")
    return resolved


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


def _normalize_force_path(project_root: Path, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(project_root.resolve())
        except ValueError as exc:
            raise InitScaffoldError(
                f"--force path is outside project root: {value}"
            ) from exc
        return rel.as_posix()
    return candidate.as_posix()


def _normalize_force_paths(
    project_root: Path, force_paths: list[str] | None
) -> frozenset[str]:
    if not force_paths:
        return frozenset()
    return frozenset(_normalize_force_path(project_root, item) for item in force_paths)


def _seed_raw_config(project_root: Path) -> dict[str, Any]:
    """Build the raw config tables used for profile-aware seeding."""
    raw: dict[str, Any] = {
        "project_root": project_root.as_posix(),
        "cycles": 5,
        "first_prompt": "prompts/setup-ai-workflow.md",
        "current_handover": "current-handover-prompt.md",
        "update_handover": "update-handover-prompt.md",
        "complexity_report": ".cyclopsctl/reports/complexity-report.json",
        "default_model": "composer-2.5",
        "task_source": "handover",
        PROFILE_SECTION: dict(BUILTIN_PROFILES),
        ROUTING_PROFILE_SECTION: dict(BUILTIN_ROUTING_PROFILES),
    }
    return raw


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise InitScaffoldError(f"Unsupported TOML value type for seeding: {type(value)!r}")


def _render_seed_config(
    *,
    effective: dict[str, Any],
    raw_profiles: dict[str, Any],
    raw_routing_profiles: dict[str, Any],
) -> str:
    """Render a minimal cyclopsctl.toml with top-level effective values."""
    lines = [
        "# Generated by cyclopsctl init",
        "",
        f"project_root = {_format_toml_value(effective['project_root'])}",
        f"cycles = {effective['cycles']}",
        f"first_prompt = {_format_toml_value(effective['first_prompt'])}",
        f"current_handover = {_format_toml_value(effective['current_handover'])}",
        f"update_handover = {_format_toml_value(effective['update_handover'])}",
        f"complexity_report = {_format_toml_value(effective['complexity_report'])}",
        f"default_model = {_format_toml_value(effective.get('default_model', 'composer-2.5'))}",
    ]

    optional_keys = (
        "task_source",
        "retry_on",
        "retry_max_attempts",
        "plain",
        "strict_handover",
        "tag",
    )
    for key in optional_keys:
        if key in effective and effective[key] is not None:
            lines.append(f"{key} = {_format_toml_value(effective[key])}")

    routing = effective.get("routing")
    if isinstance(routing, dict) and routing:
        lines.extend(["", "[routing]"])
        for key, value in routing.items():
            if key == "rules" or key == "fallback":
                continue
            lines.append(f"{key} = {_format_toml_value(value)}")

    lines.append("")
    lines.append("# Reusable run presets (select with --profile NAME)")
    for name in sorted(raw_profiles):
        lines.append("")
        lines.append(f"[profile.{name}]")
        table = raw_profiles[name]
        if not isinstance(table, dict):
            raise InitScaffoldError(f"profile.{name} must be a table")
        for key, value in table.items():
            lines.append(f"{key} = {_format_toml_value(value)}")

    if raw_routing_profiles:
        lines.append("")
        lines.append("# Routing presets referenced by profile.routing_profile")
        for name in sorted(raw_routing_profiles):
            lines.append("")
            lines.append(f"[routing_profile.{name}]")
            table = raw_routing_profiles[name]
            if not isinstance(table, dict):
                raise InitScaffoldError(f"routing_profile.{name} must be a table")
            for key, value in table.items():
                lines.append(f"{key} = {_format_toml_value(value)}")

    lines.append("")
    return "\n".join(lines)


def _render_config_content(project_root: Path, profile: str | None) -> str:
    raw = _seed_raw_config(project_root)
    effective = merge_effective_file_config(raw, profile)
    return _render_seed_config(
        effective=effective,
        raw_profiles=raw[PROFILE_SECTION],
        raw_routing_profiles=raw[ROUTING_PROFILE_SECTION],
    )


def _validate_profile(project_root: Path, profile: str | None) -> None:
    if profile is None:
        return
    raw = _seed_raw_config(project_root)
    merge_effective_file_config(raw, profile)


def _relative_path(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def _config_target(project_root: Path, config_path: Path) -> _ScaffoldTarget:
    rel = _relative_path(project_root, config_path)
    return _ScaffoldTarget(
        relative_path=rel,
        absolute_path=config_path,
        exists=config_path.is_file(),
    )


def _template_targets(project_root: Path) -> list[_ScaffoldTarget]:
    if not TEMPLATES_DIR.is_dir():
        raise InitScaffoldError(f"Bundled scaffold templates missing: {TEMPLATES_DIR}")

    targets: list[_ScaffoldTarget] = []
    for template_name, destination in SCAFFOLD_TEMPLATE_MAP.items():
        source = TEMPLATES_DIR / template_name
        if not source.is_file():
            raise InitScaffoldError(f"Bundled scaffold template missing: {source}")
        absolute = (project_root / destination).resolve()
        targets.append(
            _ScaffoldTarget(
                relative_path=destination,
                absolute_path=absolute,
                exists=absolute.is_file(),
            )
        )
    return targets


def _gitignore_target(project_root: Path) -> _ScaffoldTarget:
    absolute = (project_root / ".gitignore").resolve()
    return _ScaffoldTarget(
        relative_path=".gitignore",
        absolute_path=absolute,
        exists=absolute.is_file(),
    )


def _gitignore_needs_update(path: Path) -> bool:
    if not path.is_file():
        return True
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InitScaffoldError(f"Cannot read .gitignore: {path}") from exc
    existing_lines = {line.strip() for line in content.splitlines()}
    return any(entry not in existing_lines for entry in GITIGNORE_ENTRIES)


def discover_scaffold_plan(
    *,
    project_root: Path,
    config_path: Path | None = None,
    skip_templates: bool = False,
) -> tuple[str, ...]:
    """Return relative paths that would be created or updated by init."""
    root = _require_absolute_project_root(project_root)
    destination = (
        config_path.expanduser().resolve()
        if config_path is not None
        else (root / "cyclopsctl.toml").resolve()
    )
    planned: list[str] = []

    config = _config_target(root, destination)
    planned.append(config.relative_path)

    if not skip_templates:
        for target in _template_targets(root):
            planned.append(target.relative_path)

        gitignore = _gitignore_target(root)
        if _gitignore_needs_update(gitignore.absolute_path):
            planned.append(gitignore.relative_path)

    return tuple(planned)


def _check_overwrite_conflicts(
    targets: list[_ScaffoldTarget],
    *,
    force_paths: frozenset[str],
    force_all: bool,
) -> None:
    conflicts = [
        target.relative_path
        for target in targets
        if target.exists
        and not force_all
        and target.relative_path not in force_paths
    ]
    if conflicts:
        joined = ", ".join(conflicts)
        raise InitScaffoldError(
            f"Refusing to overwrite existing file(s): {joined}. "
            "Use --force PATH for specific paths or --force-all."
        )


def _substitute_placeholders(text: str, mapping: dict[str, str]) -> str:
    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def _render_template_content(
    template_name: str,
    *,
    project_root: Path,
) -> str:
    source = TEMPLATES_DIR / template_name
    text = source.read_text(encoding="utf-8")
    if template_name != "current-handover-prompt.md":
        return text

    root_display = str(project_root.resolve())
    mapping = {
        "PROJECT_NAME": config_project_fallback_name(project_root),
        "PROJECT_ROOT": root_display,
        "TECH_STACK": "See prd.md and ai-context.md",
        "BRANCH": _detect_git_branch(project_root),
    }
    return _substitute_placeholders(text, mapping)


def _write_config(
    *,
    project_root: Path,
    destination: Path,
    profile: str | None,
) -> None:
    content = _render_config_content(project_root, profile)
    try:
        destination.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise InitScaffoldError(f"Cannot write config: {destination}") from exc


def _write_templates(*, project_root: Path) -> list[str]:
    written: list[str] = []
    for template_name, destination in SCAFFOLD_TEMPLATE_MAP.items():
        target_path = (project_root / destination).resolve()
        content = _render_template_content(template_name, project_root=project_root)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise InitScaffoldError(f"Cannot write template: {target_path}") from exc
        written.append(destination)
    return written


def _update_gitignore(project_root: Path) -> str | None:
    gitignore_path = (project_root / ".gitignore").resolve()
    if not _gitignore_needs_update(gitignore_path):
        return None

    if gitignore_path.is_file():
        try:
            existing = gitignore_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InitScaffoldError(f"Cannot read .gitignore: {gitignore_path}") from exc
        existing_lines = {line.strip() for line in existing.splitlines()}
        additions = [entry for entry in GITIGNORE_ENTRIES if entry not in existing_lines]
        if not additions:
            return None
        block = "\n".join(
            [
                "",
                "# Cyclopsctl (added by cyclopsctl init)",
                *additions,
                "",
            ]
        )
        content = existing.rstrip("\n") + block
    else:
        content = "\n".join(
            [
                "# Cyclopsctl",
                *GITIGNORE_ENTRIES,
                "",
            ]
        )

    try:
        gitignore_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise InitScaffoldError(f"Cannot write .gitignore: {gitignore_path}") from exc
    return ".gitignore"


def run_init_scaffold(
    *,
    project_root: Path,
    config_path: Path | None = None,
    profile: str | None = None,
    skip_templates: bool = False,
    force_paths: list[str] | None = None,
    force_all: bool = False,
    dry_run: bool = False,
) -> InitScaffoldResult:
    """
    Scaffold cyclopsctl config and starter files for a project directory.

    Non-destructive by default: existing targets raise ``InitScaffoldError``
    unless ``force_paths`` or ``force_all`` allows overwrite.
    """
    root = _require_absolute_project_root(project_root)
    destination = (
        config_path.expanduser().resolve()
        if config_path is not None
        else (root / "cyclopsctl.toml").resolve()
    )
    normalized_force = _normalize_force_paths(root, force_paths)

    _validate_profile(root, profile)

    planned = discover_scaffold_plan(
        project_root=root,
        config_path=destination,
        skip_templates=skip_templates,
    )
    if dry_run:
        return InitScaffoldResult(
            project_root=root,
            config_path=destination,
            profile=profile,
            dry_run=True,
            planned_paths=planned,
        )

    overwrite_targets: list[_ScaffoldTarget] = [_config_target(root, destination)]
    if not skip_templates:
        overwrite_targets.extend(_template_targets(root))

    _check_overwrite_conflicts(
        overwrite_targets,
        force_paths=normalized_force,
        force_all=force_all,
    )

    written: list[str] = []

    _write_config(project_root=root, destination=destination, profile=profile)
    written.append(_relative_path(root, destination))

    if not skip_templates:
        written.extend(_write_templates(project_root=root))

        gitignore_written = _update_gitignore(root)
        if gitignore_written is not None:
            written.append(gitignore_written)

    return InitScaffoldResult(
        project_root=root,
        config_path=destination,
        profile=profile,
        written_paths=tuple(written),
    )


def format_init_checklist() -> str:
    """Human-readable next steps after a successful init."""
    return "\n".join(
        [
            "Next steps:",
            "  1. Ensure `.env` has CURSOR_API_KEY and `prd.md` exists (required before first init on greenfield repos)",
            "  2. Run `cyclopsctl init`",
            "  3. Run `cyclopsctl launch` (or bare `cyclopsctl`)",
            "",
            "Optional: `cyclopsctl doctor` for read-only checks; `cyclopsctl bootstrap` to re-parse a PRD.",
        ]
    )
