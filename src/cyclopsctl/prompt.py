"""Read handover Markdown, parse Task ID markers, and content hashes (task 3)."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from cyclopsctl.tasks.types import TaskShowDetail

logger = logging.getLogger(__name__)

AI_CONTEXT_SECTION_HEADER = "# AI Context"
PROMPT_SECTION_SEPARATOR = "---"

# Task IDs are integers only; decimal IDs like 7.1 must not match.
TASK_ID_PATTERN = re.compile(
    r"^#\s*Task\s+ID\s*:\s*(\d+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


class PromptError(ValueError):
    """Invalid prompt content or missing required handover markers."""


class PromptComposeError(ValueError):
    """Invalid agent prompt composition (e.g. required ai-context missing)."""


@dataclass(frozen=True)
class AiContextAttachment:
    """Loaded ai-context payload attached to an agent prompt."""

    path: Path
    missing: bool
    content: str
    content_hash: str
    truncated: bool = False


_missing_ai_context_warned: set[Path] = set()


@dataclass(frozen=True)
class PromptContent:
    """Raw handover text plus normalized form and hash for verification."""

    raw: str
    normalized: str
    content_hash: str


@dataclass(frozen=True)
class HandoverSnapshot:
    """Parsed handover state for snapshot/compare (used by verify in task 9)."""

    path: Path
    missing: bool
    task_id: int | None
    raw: str
    normalized: str
    content_hash: str


def read_prompt_text(path: Path) -> str:
    """Read a prompt file exactly as authored (UTF-8, preserves line endings)."""
    if not path.is_file():
        raise PromptError(f"Prompt file not found: {path}")
    return path.read_bytes().decode("utf-8")


def normalize_whitespace(text: str) -> str:
    """Collapse insignificant whitespace for stable content comparison."""
    return re.sub(r"\s+", " ", text.strip())


def sha256_content_hash(normalized_text: str) -> str:
    """Return the SHA-256 hex digest of normalized UTF-8 text."""
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def prompt_content_from_raw(raw: str) -> PromptContent:
    """Build normalized text and content hash from raw prompt bytes."""
    normalized = normalize_whitespace(raw)
    return PromptContent(
        raw=raw,
        normalized=normalized,
        content_hash=sha256_content_hash(normalized),
    )


def parse_task_id(text: str, *, required: bool = True) -> int | None:
    """Extract the parent Task ID from a ``# Task ID: <n>`` marker line."""
    match = TASK_ID_PATTERN.search(text)
    if match is None:
        if required:
            raise PromptError(
                "Missing or invalid Task ID marker. "
                "Expected a line like: # Task ID: 7"
            )
        return None
    return int(match.group(1))


def reset_ai_context_warnings() -> None:
    """Clear warn-once state (for tests)."""
    _missing_ai_context_warned.clear()


def load_ai_context(
    path: Path,
    *,
    require: bool = False,
    max_chars: int | None = None,
) -> AiContextAttachment:
    """Load ai-context file for prompt injection; warn once if missing."""
    if not path.is_file():
        if require:
            raise PromptComposeError(f"ai-context not found (required): {path}")
        resolved = path.resolve()
        if resolved not in _missing_ai_context_warned:
            _missing_ai_context_warned.add(resolved)
            logger.warning(
                "ai-context file not found: %s; sending prompt without AI Context section",
                path,
            )
        return AiContextAttachment(
            path=path,
            missing=True,
            content="",
            content_hash=sha256_content_hash(""),
        )

    raw = read_prompt_text(path)
    truncated = False
    content = raw
    if max_chars is not None and len(raw) > max_chars:
        truncated = True
        content = raw[:max_chars] + "\n\n[... ai-context truncated ...]"
        logger.warning(
            "ai-context exceeds %d characters; truncating attachment from %s",
            max_chars,
            path,
        )

    normalized = normalize_whitespace(content)
    return AiContextAttachment(
        path=path,
        missing=False,
        content=content,
        content_hash=sha256_content_hash(normalized),
        truncated=truncated,
    )


def compose_agent_prompt(
    body: str,
    ai_context_path: Path,
    *,
    require_ai_context: bool = False,
    max_chars: int | None = None,
    attach_ai_context: bool = True,
) -> tuple[str, AiContextAttachment]:
    """
    Build the final agent prompt: optional AI Context section, separator, body.

    When ``attach_ai_context`` is False, returns ``body`` unchanged (used for
    the update phase in the same agent session, which already received
    ai-context on the implementation send).

    When ai-context is missing and not required, returns ``body`` unchanged.
    """
    if not attach_ai_context:
        return body, AiContextAttachment(
            path=ai_context_path,
            missing=True,
            content="",
            content_hash=sha256_content_hash(""),
        )

    attachment = load_ai_context(
        ai_context_path,
        require=require_ai_context,
        max_chars=max_chars,
    )
    if attachment.missing:
        return body, attachment

    composed = (
        f"{AI_CONTEXT_SECTION_HEADER}\n\n"
        f"{attachment.content.rstrip()}\n\n"
        f"{PROMPT_SECTION_SEPARATOR}\n\n"
        f"{body.lstrip()}"
    )
    logger.info(
        "Attached ai-context path=%s hash=%s truncated=%s",
        attachment.path,
        attachment.content_hash[:12],
        attachment.truncated,
    )
    return composed, attachment


def build_fallback_implementation_prompt(
    task_id: int,
    *,
    title: str,
    description: str | None = None,
) -> str:
    """
    Minimal implementation prompt when the handover file is unusable.

    Used when the handover points at queue-complete (Task ID 0), a missing task,
    or a non-pending task, and the task queue selects the real work item.
    """
    lines = [
        f"# Task ID: {task_id}",
        "",
        f"## Current Task: {task_id} — {title}",
        "",
    ]
    if description:
        lines.extend(["### Description", "", description.strip(), ""])
    lines.append(
        "Implement and test only this parent task. "
        "Follow ai-context.md rules for the implementation phase."
    )
    return "\n".join(lines)


def snapshot_handover(path: Path, *, allow_missing: bool = False) -> HandoverSnapshot:
    """
    Read ``current-handover-prompt.md`` and capture task id + content hash.

    When ``allow_missing`` is True (cycle 1 before the file exists), return an
    empty snapshot with ``missing=True`` instead of raising.
    """
    if not path.is_file():
        if allow_missing:
            return HandoverSnapshot(
                path=path,
                missing=True,
                task_id=None,
                raw="",
                normalized="",
                content_hash=sha256_content_hash(""),
            )
        raise PromptError(f"Handover file not found: {path}")

    raw = read_prompt_text(path)
    content = prompt_content_from_raw(raw)
    return HandoverSnapshot(
        path=path,
        missing=False,
        task_id=parse_task_id(raw, required=False),
        raw=content.raw,
        normalized=content.normalized,
        content_hash=content.content_hash,
    )


def format_model_selection(complexity: int | None) -> str:
    """Informational model line for synced handover prompts."""
    if complexity is None:
        return (
            "Complexity unknown — default to **Composer 2.5** (`composer-2.5`). "
            "Informational only; the cyclopsctl selects the runtime model from "
            "the complexity report when using automated runs."
        )
    if complexity >= 9:
        return (
            f"Complexity **{complexity}** → **Fable 5 high-thinking** "
            f"(`fable-high-thinking`). Informational only; the cyclopsctl selects "
            f"the runtime model from the complexity report when using automated runs."
        )
    if complexity >= 6:
        return (
            f"Complexity **{complexity}** → **Grok 4.5** (`grok`). "
            f"Informational only; the cyclopsctl selects the runtime model from "
            f"the complexity report when using automated runs."
        )
    return (
        f"Complexity **{complexity}** → **Composer 2.5** (`composer-2.5`). "
        f"Informational only; the cyclopsctl selects the runtime model from "
        f"the complexity report when using automated runs."
    )


def render_synced_handover(
    *,
    task: TaskShowDetail | None,
    project_name: str,
    project_root: Path,
    tech_stack: str,
    branch: str,
    verification_command: str = "python -m pytest",
) -> str:
    """
    Build ``current-handover-prompt.md`` from task show output.

    When ``task`` is None (empty queue), emit a completion handover with
    ``# Task ID: 0``.
    """
    root_display = str(project_root.resolve())
    env_block = "\n".join(
        [
            "## Environment",
            f"- **Project:** {project_name}",
            f"- **Project root:** `{root_display}`",
            f"- **Tech stack:** {tech_stack}",
            "- **Context file:** Cyclopsctl prepends `ai-context.md` automatically — follow its implementation rules.",
            f"- **Branch:** `{branch}`",
            f"- **Tasks CLI:** `cyclopsctl tasks ... --project-root {root_display}`",
        ]
    )
    core_rules = "\n".join(
        [
            "## Core Handover Rules",
            "- **NO HISTORY:** This file describes only the current task. Do not infer remaining work from prior handovers or git history.",
            f"- **SCOPE:** Implement task **{task.numeric_id if task else 0}** only. Do not start the next task or mark tasks done.",
            "- **IMPLEMENTATION ONLY:** Do not edit docs, `ai-context.md`, or this handover during implementation.",
        ]
    )
    implementation_rules = "\n".join(
        [
            "## Implementation Phase — Do Only This",
            "- Implement and test only the current parent task below.",
            f"- Run `{verification_command}` before finishing; meet the task test strategy.",
            "- Use Context7 MCP for library docs per `ai-context.md` (resolve library ID first; not for task queue ops).",
            "- **Do not** mark tasks done, run `cyclopsctl tasks next`, rewrite this file, or edit `update-handover-prompt.md`.",
            "- **Do not** create or update docs in `docs/` or edit `docs/index.md`.",
        ]
    )

    if task is None:
        return "\n".join(
            [
                "# Session Handover",
                "",
                "# Task ID: 0",
                "",
                env_block,
                "",
                "## Status: Task queue complete",
                "",
                "All tasks are complete. The cyclopsctl will stop on the next run when no pending work remains.",
                "",
                core_rules,
                "",
                implementation_rules,
                "",
                "## Verification",
                "",
                f"```bash",
                verification_command,
                "```",
                "",
                "## Model Selection",
                "",
                "No pending tasks — model routing does not apply.",
                "",
            ]
        )

    deps_display = ", ".join(task.dependencies) if task.dependencies else "none"
    complexity_display = str(task.complexity) if task.complexity is not None else "unknown"
    lines = [
        "# Session Handover",
        "",
        f"# Task ID: {task.numeric_id}",
        "",
        env_block,
        "",
        core_rules,
        "",
        implementation_rules,
        "",
        f"## Current Task: {task.numeric_id} — {task.title}",
        "",
        "| Field | Value |",
        "|-------|--------|",
        f"| ID | {task.numeric_id} |",
        f"| Title | {task.title} |",
        f"| Complexity | {complexity_display} |",
        f"| Priority | {task.priority or 'unknown'} |",
        f"| Dependencies | {deps_display} |",
        f"| Status | {task.status or 'unknown'} |",
        "",
    ]

    if task.description:
        lines.extend(["### Description", "", task.description.strip(), ""])

    if task.details:
        lines.extend(["### Implementation Details", "", task.details.strip(), ""])

    if task.test_strategy:
        lines.extend(["### Test Strategy", "", task.test_strategy.strip(), ""])

    lines.extend(
        [
            "## Verification",
            "",
            "```bash",
            verification_command,
            "```",
            "",
            "## Model Selection",
            "",
            format_model_selection(task.complexity),
            "",
        ]
    )
    return "\n".join(lines)
