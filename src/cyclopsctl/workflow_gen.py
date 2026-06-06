"""Project-aware workflow file generation from PRD and repo metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
GENERIC_STUB_DIR = TEMPLATES_DIR
CURSOR_RULES_TEMPLATE_DIR = TEMPLATES_DIR / "cursor-rules"
CYCLOPSCTL_CURSOR_RULES_DIR = Path(".cursor/rules/cyclopsctl")

WORKFLOW_TEMPLATE_MAP: dict[str, str] = {
    "workflow-ai-context.md": "ai-context.md",
    "workflow-update-handover-prompt.md": "update-handover-prompt.md",
    "docs-index.md": "docs/index.md",
}

WORKFLOW_STALENESS_RELATIVE_PATHS: frozenset[str] = frozenset(
    {
        "ai-context.md",
        "update-handover-prompt.md",
        "docs/index.md",
        "current-handover-prompt.md",
    }
)

WORKFLOW_REFRESH_REMEDIATION = "cyclopsctl init --refresh-workflow"

CYCLOPSCTL_TASKS_RE = re.compile(r"\bcyclopsctl\s+tasks\b", re.IGNORECASE)
CYCLOPSCTL_SET_STATUS_RE = re.compile(
    r"\bcyclopsctl\s+tasks\s+set-status\b",
    re.IGNORECASE,
)

GENERIC_STUB_MAP: dict[str, str] = {
    "ai-context.md": "ai-context.md",
    "update-handover-prompt.md": "update-handover-prompt.md",
    "docs/index.md": "docs-index.md",
}

TECH_STACK_HEADING_RE = re.compile(
    r"^#{1,3}\s+(tech(?:nology)?\s+stack|stack|requirements)\s*$",
    re.IGNORECASE,
)
TEST_SECTION_HEADING_RE = re.compile(
    r"^#{1,3}\s+.*(test(?:ing)?|verification)\s*$",
    re.IGNORECASE,
)
README_CONTEXT_SECTION_RE = re.compile(
    r"^#{1,3}\s+.*(test(?:ing)?|verification|development|quick\s*start|getting\s*started)\s*$",
    re.IGNORECASE,
)
README_DEFAULT_NAME = "README.md"
FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
TEST_CMD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpython\s+-m\s+pytest\b"), "python -m pytest"),
    (re.compile(r"\bpytest\b"), "python -m pytest"),
    (re.compile(r"\bnpm\s+test\b"), "npm test"),
    (re.compile(r"\byarn\s+test\b"), "yarn test"),
    (re.compile(r"\bpnpm\s+test\b"), "pnpm test"),
    (re.compile(r"\bcargo\s+test\b"), "cargo test"),
    (re.compile(r"\bgo\s+test\b"), "go test ./..."),
    (re.compile(r"\bmake\s+test\b"), "make test"),
    (re.compile(r"\bdotnet\s+test\b"), "dotnet test"),
)


class WorkflowGenError(RuntimeError):
    """Invalid workflow generation inputs or filesystem failure."""


def _project_fallback_name(project_root: Path) -> str:
    return project_root.name or "Project"


WorkflowContextSource = Literal["prd", "readme", "repo"]

_CONTEXT_SOURCE_LABELS: dict[WorkflowContextSource, str] = {
    "prd": "prd.md",
    "readme": "README.md",
    "repo": "repository metadata",
}


@dataclass(frozen=True)
class WorkflowInputs:
    """Resolved inputs for workflow file rendering."""

    project_root: Path
    project_name: str
    tech_stack: str
    test_cmd: str
    context_source: WorkflowContextSource = "repo"

    @property
    def context_source_label(self) -> str:
        return _CONTEXT_SOURCE_LABELS[self.context_source]


@dataclass(frozen=True)
class WorkflowGenConfig:
    """Configuration for workflow file generation."""

    project_root: Path
    prd_path: Path | None = None
    tech_stack: str | None = None
    test_cmd: str | None = None
    force_workflow: bool = False
    force_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WorkflowGenResult:
    """Outcome of workflow file generation."""

    generated_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    installed_rule_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowStaleness:
    """Staleness assessment for a single workflow file."""

    relative_path: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowRefreshResult:
    """Outcome of selective stale-workflow refresh."""

    updated_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowGenError(f"Cannot read file: {path}") from exc


def _write_text(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise WorkflowGenError(f"Cannot write file: {path}") from exc


def _normalize_force_path(project_root: Path, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(project_root.resolve())
        except ValueError as exc:
            raise WorkflowGenError(
                f"--force path is outside project root: {value}"
            ) from exc
        return rel.as_posix()
    return candidate.as_posix()


def normalize_force_paths(
    project_root: Path, force_paths: list[str] | None
) -> frozenset[str]:
    if not force_paths:
        return frozenset()
    return frozenset(_normalize_force_path(project_root, item) for item in force_paths)


def _substitute_placeholders(text: str, mapping: dict[str, str]) -> str:
    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def _project_name_from_prd(prd_text: str, project_root: Path) -> str:
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title.lower().startswith("prd:"):
                return title.split(":", 1)[1].strip()
            return title
    return _project_fallback_name(project_root)


def _section_content_after_heading(lines: list[str], heading_index: int) -> str:
    collected: list[str] = []
    heading_level = len(lines[heading_index].split()[0])
    for line in lines[heading_index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped.split()[0])
            if level <= heading_level:
                break
        if not stripped:
            continue
        if stripped.startswith("- "):
            collected.append(stripped[2:].strip())
        elif stripped.startswith("* "):
            collected.append(stripped[2:].strip())
        else:
            collected.append(stripped)

    if not collected:
        return ""

    if len(collected) > 1 or any(
        lines[heading_index + 1 + i].strip().startswith(("- ", "* "))
        for i in range(min(len(collected), 8))
        if heading_index + 1 + i < len(lines)
    ):
        return ", ".join(collected)
    return collected[0]


def _extract_tech_stack_from_prd(prd_text: str) -> str | None:
    lines = prd_text.splitlines()
    for index, line in enumerate(lines):
        if TECH_STACK_HEADING_RE.match(line.strip()):
            content = _section_content_after_heading(lines, index)
            if content:
                return content

    requirements: list[str] = []
    in_requirements = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,3}\s+requirements\s*$", stripped, re.IGNORECASE):
            in_requirements = True
            continue
        if in_requirements:
            if stripped.startswith("#"):
                break
            if stripped.startswith("- "):
                requirements.append(stripped[2:].strip())
            elif stripped and not requirements:
                requirements.append(stripped)

    if requirements:
        return ", ".join(requirements[:6])

    stack_hints: list[str] = []
    for pattern, label in (
        (re.compile(r"\bPython\s+3\.\d+\+?\b", re.IGNORECASE), None),
        (re.compile(r"\bNode\.js\b", re.IGNORECASE), "Node.js"),
        (re.compile(r"\bTypeScript\b", re.IGNORECASE), "TypeScript"),
        (re.compile(r"\bRust\b", re.IGNORECASE), "Rust"),
        (re.compile(r"\bGo\b"), "Go"),
        (re.compile(r"\bReact\b", re.IGNORECASE), "React"),
        (re.compile(r"\bFastAPI\b", re.IGNORECASE), "FastAPI"),
        (re.compile(r"\bDjango\b", re.IGNORECASE), "Django"),
        (re.compile(r"\bcursor-sdk\b", re.IGNORECASE), "cursor-sdk"),
        (re.compile(r"\bcyclopsctl\b", re.IGNORECASE), "cyclopsctl tasks CLI"),
    ):
        match = pattern.search(prd_text)
        if match:
            stack_hints.append(label or match.group(0))

    if stack_hints:
        seen: list[str] = []
        for item in stack_hints:
            if item not in seen:
                seen.append(item)
        return ", ".join(seen)

    return None


def _extract_test_cmd_from_text(text: str) -> str | None:
    for pattern, command in TEST_CMD_PATTERNS:
        if pattern.search(text):
            return command
    return None


def _extract_test_cmd_from_prd(prd_text: str) -> str | None:
    lines = prd_text.splitlines()
    for index, line in enumerate(lines):
        if TEST_SECTION_HEADING_RE.match(line.strip()):
            section = _section_content_after_heading(lines, index)
            found = _extract_test_cmd_from_text(section)
            if found:
                return found

    for block in FENCED_BLOCK_RE.findall(prd_text):
        found = _extract_test_cmd_from_text(block)
        if found:
            return found

    return _extract_test_cmd_from_text(prd_text)


def _detect_test_cmd_from_repo(project_root: Path) -> str | None:
    if (project_root / "pyproject.toml").is_file() or (project_root / "pytest.ini").is_file():
        return "python -m pytest"
    if (project_root / "package.json").is_file():
        return "npm test"
    if (project_root / "Cargo.toml").is_file():
        return "cargo test"
    if (project_root / "go.mod").is_file():
        return "go test ./..."
    makefile = project_root / "Makefile"
    if makefile.is_file():
        try:
            content = makefile.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if re.search(r"^test\s*:", content, re.MULTILINE):
            return "make test"
    return None


def _readme_path(project_root: Path) -> Path:
    return project_root / README_DEFAULT_NAME


def _project_name_from_readme(readme_text: str) -> str | None:
    for line in readme_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            return title or None
    return None


def _stack_hints_from_text(text: str) -> list[str]:
    stack_hints: list[str] = []
    for pattern, label in (
        (re.compile(r"\bPython\s+3\.\d+\+?\b", re.IGNORECASE), None),
        (re.compile(r"\bPython\b", re.IGNORECASE), "Python"),
        (re.compile(r"\bNode\.js\b", re.IGNORECASE), "Node.js"),
        (re.compile(r"\bTypeScript\b", re.IGNORECASE), "TypeScript"),
        (re.compile(r"\bJavaScript\b", re.IGNORECASE), "JavaScript"),
        (re.compile(r"\bRust\b", re.IGNORECASE), "Rust"),
        (re.compile(r"\bGo\b"), "Go"),
        (re.compile(r"\bReact\b", re.IGNORECASE), "React"),
        (re.compile(r"\bFastAPI\b", re.IGNORECASE), "FastAPI"),
        (re.compile(r"\bDjango\b", re.IGNORECASE), "Django"),
        (re.compile(r"\bpytest\b", re.IGNORECASE), "pytest"),
        (re.compile(r"\bVitest\b", re.IGNORECASE), "Vitest"),
        (re.compile(r"\bVite\b", re.IGNORECASE), "Vite"),
    ):
        match = pattern.search(text)
        if match:
            stack_hints.append(label or match.group(0))

    seen: list[str] = []
    for item in stack_hints:
        if item not in seen:
            seen.append(item)
    return seen


def _extract_tech_stack_from_readme(readme_text: str, project_root: Path) -> str | None:
    lines = readme_text.splitlines()
    for index, line in enumerate(lines):
        if TECH_STACK_HEADING_RE.match(line.strip()):
            content = _section_content_after_heading(lines, index)
            if content:
                return content

    hints: list[str] = []
    for block in FENCED_BLOCK_RE.findall(readme_text):
        hints.extend(_stack_hints_from_text(block))
    if not hints:
        hints.extend(_stack_hints_from_text(readme_text))

    repo_stack = _detect_tech_stack_from_repo(project_root)
    if repo_stack:
        for item in repo_stack.split(", "):
            if item not in hints:
                hints.append(item)

    if hints:
        return ", ".join(hints)
    return repo_stack


def _detect_tech_stack_from_repo(project_root: Path) -> str | None:
    hints: list[str] = []
    if (project_root / "pyproject.toml").is_file() or (project_root / "pytest.ini").is_file():
        hints.append("Python")
    if (project_root / "package.json").is_file():
        hints.append("Node.js")
    if (project_root / "Cargo.toml").is_file():
        hints.append("Rust")
    if (project_root / "go.mod").is_file():
        hints.append("Go")
    if hints:
        return ", ".join(hints)
    return None


def _extract_test_cmd_from_readme(readme_text: str) -> str | None:
    lines = readme_text.splitlines()
    for index, line in enumerate(lines):
        if README_CONTEXT_SECTION_RE.match(line.strip()):
            section = _section_content_after_heading(lines, index)
            found = _extract_test_cmd_from_text(section)
            if found:
                return found

    for block in FENCED_BLOCK_RE.findall(readme_text):
        found = _extract_test_cmd_from_text(block)
        if found:
            return found

    return _extract_test_cmd_from_text(readme_text)


def resolve_workflow_inputs(
    *,
    project_root: Path,
    prd_path: Path | None = None,
    tech_stack: str | None = None,
    test_cmd: str | None = None,
) -> WorkflowInputs:
    root = project_root.resolve()
    prd_text = ""
    if prd_path is not None and prd_path.is_file():
        prd_text = _read_text(prd_path)

    readme_text = ""
    readme_path = _readme_path(root)
    if readme_path.is_file():
        readme_text = _read_text(readme_path)

    context_source: WorkflowContextSource
    if prd_text:
        context_source = "prd"
    elif readme_text:
        context_source = "readme"
    else:
        context_source = "repo"

    if prd_text:
        project_name = _project_name_from_prd(prd_text, root)
    elif readme_text:
        project_name = _project_name_from_readme(readme_text) or _project_fallback_name(root)
    else:
        project_name = _project_fallback_name(root)

    resolved_stack = tech_stack
    if not resolved_stack and prd_text:
        resolved_stack = _extract_tech_stack_from_prd(prd_text)
    if not resolved_stack and readme_text:
        resolved_stack = _extract_tech_stack_from_readme(readme_text, root)
    if not resolved_stack:
        resolved_stack = _detect_tech_stack_from_repo(root)
    resolved_test = test_cmd
    if not resolved_test and prd_text:
        resolved_test = _extract_test_cmd_from_prd(prd_text)
    if not resolved_test and readme_text:
        resolved_test = _extract_test_cmd_from_readme(readme_text)
    if not resolved_test:
        resolved_test = _detect_test_cmd_from_repo(root)
    if not resolved_test:
        resolved_test = "python -m pytest"

    final_stack = resolved_stack
    if not final_stack and context_source == "prd":
        final_stack = "See prd.md"

    return WorkflowInputs(
        project_root=root,
        project_name=project_name,
        tech_stack=final_stack or "",
        test_cmd=resolved_test,
        context_source=context_source,
    )


def _generic_stub_content(relative_path: str) -> str:
    stub_name = GENERIC_STUB_MAP.get(relative_path)
    if stub_name is None:
        raise WorkflowGenError(f"No generic stub mapping for: {relative_path}")
    stub_path = GENERIC_STUB_DIR / stub_name
    if not stub_path.is_file():
        raise WorkflowGenError(f"Bundled generic stub missing: {stub_path}")
    return _read_text(stub_path)


def workflow_file_staleness_reasons(content: str, relative_path: str) -> tuple[str, ...]:
    """Return human-readable staleness reasons for a workflow file."""
    if relative_path not in WORKFLOW_STALENESS_RELATIVE_PATHS:
        return ()

    reasons: list[str] = []

    if relative_path == "ai-context.md":
        if not CYCLOPSCTL_TASKS_RE.search(content):
            reasons.append("missing cyclopsctl tasks CLI references")
        if "Implementation Phase Rules" not in content:
            reasons.append("missing Implementation Phase Rules")
        if "Update Phase Rules" not in content:
            reasons.append("missing Update Phase Rules")
    elif relative_path == "update-handover-prompt.md":
        if not CYCLOPSCTL_TASKS_RE.search(content):
            reasons.append("missing cyclopsctl tasks CLI references")
        if not CYCLOPSCTL_SET_STATUS_RE.search(content):
            reasons.append("missing cyclopsctl tasks set-status command")
    elif relative_path == "current-handover-prompt.md":
        return tuple(reasons)

    return tuple(reasons)


def is_workflow_file_stale(content: str, relative_path: str) -> bool:
    """Return True when workflow content matches staleness heuristics."""
    return bool(workflow_file_staleness_reasons(content, relative_path))


def resolve_workflow_check_paths(
    project_root: Path,
    *,
    ai_context: Path | None = None,
    update_handover: Path | None = None,
    current_handover: Path | None = None,
) -> list[tuple[str, Path]]:
    """Resolve workflow paths used by doctor, launch, and refresh checks."""
    resolved: list[tuple[str, Path]] = []
    for relative in sorted(WORKFLOW_STALENESS_RELATIVE_PATHS):
        if relative == "ai-context.md" and ai_context is not None:
            path = ai_context
        elif relative == "update-handover-prompt.md" and update_handover is not None:
            path = update_handover
        elif relative == "current-handover-prompt.md" and current_handover is not None:
            path = current_handover
        else:
            path = project_root / relative
        resolved.append((relative, path.resolve()))
    return resolved


def detect_stale_workflow_files(
    project_root: Path,
    *,
    ai_context: Path | None = None,
    update_handover: Path | None = None,
    current_handover: Path | None = None,
) -> tuple[WorkflowStaleness, ...]:
    """Detect workflow files that predate native cyclopsctl tasks conventions."""
    stale: list[WorkflowStaleness] = []
    for relative, path in resolve_workflow_check_paths(
        project_root,
        ai_context=ai_context,
        update_handover=update_handover,
        current_handover=current_handover,
    ):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        reasons = workflow_file_staleness_reasons(content, relative)
        if reasons:
            stale.append(WorkflowStaleness(relative_path=relative, reasons=reasons))
    return tuple(stale)


def workflow_upgrade_recommended(checks: list[object]) -> bool:
    """Return True when diagnostics include a stale-workflow upgrade signal."""
    for check in checks:
        name = getattr(check, "name", None)
        informational = getattr(check, "informational", False)
        detail = getattr(check, "detail", "")
        if name == "Stale workflow" and informational and "upgrade" in str(detail).lower():
            return True
    return False


def is_generic_workflow_file(path: Path, *, relative_path: str) -> bool:
    """Return True when an existing file still matches the bundled generic stub."""
    if not path.is_file():
        return False
    existing = _read_text(path)
    generic = _generic_stub_content(relative_path)
    return existing == generic


def _should_generate_docs_index(
    target: Path,
    *,
    force_workflow: bool,
    force_paths: frozenset[str],
) -> bool:
    relative = "docs/index.md"
    if force_workflow or relative in force_paths:
        return True
    return not target.is_file()


def _should_generate_workflow_file(
    target: Path,
    *,
    relative_path: str,
    force_workflow: bool,
    force_paths: frozenset[str],
) -> bool:
    if force_workflow or relative_path in force_paths:
        return True
    if not target.is_file():
        return True
    return is_generic_workflow_file(target, relative_path=relative_path)


def cyclopsctl_cursor_rules_present(project_root: Path) -> bool:
    """Return True when cyclopsctl Cursor rules exist with content."""
    rules_dir = project_root / CYCLOPSCTL_CURSOR_RULES_DIR
    if not rules_dir.is_dir():
        return False
    return any(rules_dir.iterdir())


def _bundled_cursor_rule_paths() -> tuple[Path, ...]:
    source_root = CURSOR_RULES_TEMPLATE_DIR / "cyclopsctl"
    if not source_root.is_dir():
        raise WorkflowGenError(
            f"Bundled cyclopsctl Cursor rules missing: {source_root}"
        )
    return tuple(sorted(source_root.rglob("*.mdc")))


def install_cyclopsctl_cursor_rules(
    project_root: Path,
    *,
    project_root_value: str | None = None,
    force: bool = False,
) -> tuple[str, ...]:
    """
    Copy bundled cyclopsctl Cursor rules into the project.

    Non-destructive by default: existing rule files are skipped unless forced.
    """
    root = project_root.resolve()
    bundled_rules = _bundled_cursor_rule_paths()
    if not bundled_rules:
        return ()

    installed: list[str] = []
    root_display = project_root_value or str(root)
    for source in bundled_rules:
        relative = source.relative_to(CURSOR_RULES_TEMPLATE_DIR / "cyclopsctl")
        target = root / CYCLOPSCTL_CURSOR_RULES_DIR / relative
        relative_display = (CYCLOPSCTL_CURSOR_RULES_DIR / relative).as_posix()
        if target.is_file() and not force:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = _read_text(source)
            rendered = _substitute_placeholders(
                content,
                {"PROJECT_ROOT": root_display},
            )
            target.write_text(rendered, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise WorkflowGenError(f"Cannot write Cursor rule: {target}") from exc
        installed.append(relative_display)
    return tuple(installed)


def _render_workflow_template(template_name: str, inputs: WorkflowInputs) -> str:
    template_path = TEMPLATES_DIR / template_name
    if not template_path.is_file():
        raise WorkflowGenError(f"Workflow template missing: {template_path}")
    text = _read_text(template_path)
    mapping = {
        "PROJECT_NAME": inputs.project_name,
        "PROJECT_ROOT": str(inputs.project_root),
        "TECH_STACK": inputs.tech_stack,
        "TEST_CMD": inputs.test_cmd,
    }
    return _substitute_placeholders(text, mapping)


def generate_workflow_files(config: WorkflowGenConfig) -> WorkflowGenResult:
    """
    Generate project-aware workflow markdown files.

    Non-destructive by default: skip customized targets unless forced.
    ``docs/index.md`` is created only when absent unless forced.
    Never writes ``current-handover-prompt.md``.
    """
    if not TEMPLATES_DIR.is_dir():
        raise WorkflowGenError(f"Bundled workflow templates missing: {TEMPLATES_DIR}")

    root = config.project_root.resolve()
    prd_path = config.prd_path
    if prd_path is None and (root / "prd.md").is_file():
        prd_path = (root / "prd.md").resolve()
    elif prd_path is not None and not prd_path.is_absolute():
        prd_path = (root / prd_path).resolve()

    inputs = resolve_workflow_inputs(
        project_root=root,
        prd_path=prd_path,
        tech_stack=config.tech_stack,
        test_cmd=config.test_cmd,
    )

    generated: list[str] = []
    skipped: list[str] = []

    for template_name, relative_path in WORKFLOW_TEMPLATE_MAP.items():
        target = root / relative_path

        if relative_path == "docs/index.md":
            should_write = _should_generate_docs_index(
                target,
                force_workflow=config.force_workflow,
                force_paths=config.force_paths,
            )
        else:
            should_write = _should_generate_workflow_file(
                target,
                relative_path=relative_path,
                force_workflow=config.force_workflow,
                force_paths=config.force_paths,
            )

        if not should_write:
            skipped.append(relative_path)
            continue

        content = _render_workflow_template(template_name, inputs)
        _write_text(target, content)
        generated.append(relative_path)

    installed_rules = install_cyclopsctl_cursor_rules(
        root,
        project_root_value=str(inputs.project_root),
        force=config.force_workflow,
    )

    return WorkflowGenResult(
        generated_paths=tuple(generated),
        skipped_paths=tuple(skipped),
        installed_rule_paths=installed_rules,
    )


def refresh_stale_workflow_files(config: WorkflowGenConfig) -> WorkflowRefreshResult:
    """
    Regenerate only workflow files that match staleness heuristics.

    Non-stale files are preserved even when customized. Never writes
    ``current-handover-prompt.md``.
    """
    if not TEMPLATES_DIR.is_dir():
        raise WorkflowGenError(f"Bundled workflow templates missing: {TEMPLATES_DIR}")

    root = config.project_root.resolve()
    prd_path = config.prd_path
    if prd_path is None and (root / "prd.md").is_file():
        prd_path = (root / "prd.md").resolve()
    elif prd_path is not None and not prd_path.is_absolute():
        prd_path = (root / prd_path).resolve()

    inputs = resolve_workflow_inputs(
        project_root=root,
        prd_path=prd_path,
        tech_stack=config.tech_stack,
        test_cmd=config.test_cmd,
    )

    updated: list[str] = []
    skipped: list[str] = []

    for template_name, relative_path in WORKFLOW_TEMPLATE_MAP.items():
        if relative_path == "current-handover-prompt.md":
            continue

        target = root / relative_path
        if not target.is_file():
            skipped.append(relative_path)
            continue

        try:
            existing = _read_text(target)
        except WorkflowGenError:
            skipped.append(relative_path)
            continue

        if not is_workflow_file_stale(existing, relative_path):
            skipped.append(relative_path)
            continue

        content = _render_workflow_template(template_name, inputs)
        _write_text(target, content)
        updated.append(relative_path)

    return WorkflowRefreshResult(
        updated_paths=tuple(updated),
        skipped_paths=tuple(skipped),
    )


def generate_workflow_from_bootstrap(
    *,
    project_root: Path,
    from_prd: Path | None = None,
    tech_stack: str | None = None,
    test_cmd: str | None = None,
    force_workflow: bool = False,
    force_paths: list[str] | None = None,
) -> WorkflowGenResult:
    """Convenience wrapper used by the bootstrap pipeline."""
    root = project_root.resolve()
    prd_path: Path | None = None
    if from_prd is not None:
        prd_path = from_prd if from_prd.is_absolute() else root / from_prd
        prd_path = prd_path.resolve()
        if not prd_path.is_file():
            raise WorkflowGenError(f"PRD file not found: {prd_path}")
    elif (root / "prd.md").is_file():
        prd_path = (root / "prd.md").resolve()

    config = WorkflowGenConfig(
        project_root=root,
        prd_path=prd_path,
        tech_stack=tech_stack,
        test_cmd=test_cmd,
        force_workflow=force_workflow,
        force_paths=normalize_force_paths(root, force_paths),
    )
    return generate_workflow_files(config)
