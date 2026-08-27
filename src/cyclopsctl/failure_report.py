"""Actionable failure diagnostics for ``AgentRunError`` (agent run failures).

When an agent run ends with ``result.status == "error"`` the Cursor SDK often
returns an empty ``result``. The user is then left with only an agent id and a
run id. This module turns that into an actionable report by resolving the local
Cursor transcript on disk and summarizing the known failure context.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cyclopsctl.runner import AgentRunError, looks_like_productive_run_drop

CURSOR_PROJECTS_DIRNAME = "projects"
AGENT_TRANSCRIPTS_DIRNAME = "agent-transcripts"

_NO_DETAIL = "(SDK returned no detail)"
_NO_TRANSCRIPT = "(local transcript not found)"

DEFAULT_TRANSCRIPT_TAIL = 8
_TOOL_DETAIL_KEYS = (
    "command",
    "path",
    "file_path",
    "target_file",
    "pattern",
    "glob_pattern",
    "query",
    "description",
)


def encode_project_slug(project_root: str | os.PathLike[str]) -> str:
    """Map a filesystem path to the Cursor ``.cursor/projects/<slug>`` name.

    Empirically: the absolute path has its drive ``:`` removed, path separators
    replaced with ``-``, the drive letter lowercased, and remaining case
    preserved. Examples (Windows):

    - ``g:\\DEV\\CursorOrchestrator`` -> ``g-DEV-CursorOrchestrator``
    - ``G:\\GAMEDESIGN\\test2`` -> ``g-GAMEDESIGN-test2``
    """
    abspath = os.path.abspath(os.fspath(project_root))
    drive, rest = os.path.splitdrive(abspath)
    drive_part = drive.replace(":", "").replace("\\", "-").replace("/", "-")
    rest_part = rest.replace("\\", "-").replace("/", "-")
    slug = f"{drive_part}{rest_part}"
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    if drive_part and slug:
        slug = slug[0].lower() + slug[1:]
    return slug


def _cursor_projects_dir(home: Path | None) -> Path:
    base = home if home is not None else Path.home()
    return base / ".cursor" / CURSOR_PROJECTS_DIRNAME


def _find_project_dir(projects_dir: Path, slug: str) -> Path | None:
    """Locate the project dir, tolerating drive-letter case differences."""
    direct = projects_dir / slug
    if direct.is_dir():
        return direct
    if not projects_dir.is_dir():
        return None
    slug_lower = slug.lower()
    for child in projects_dir.iterdir():
        if child.is_dir() and child.name.lower() == slug_lower:
            return child
    return None


def resolve_transcript_path(
    project_root: str | os.PathLike[str],
    agent_id: str | None,
    *,
    home: Path | None = None,
) -> Path | None:
    """Return the local Cursor JSONL transcript path for ``agent_id`` if present.

    Looks under ``<home>/.cursor/projects/<slug>/agent-transcripts/<agent_id>/
    <agent_id>.jsonl``. Returns ``None`` when ``agent_id`` is missing or no file
    exists on disk.
    """
    if not agent_id:
        return None
    projects_dir = _cursor_projects_dir(home)
    slug = encode_project_slug(project_root)
    project_dir = _find_project_dir(projects_dir, slug)
    if project_dir is None:
        return None
    candidate = (
        project_dir / AGENT_TRANSCRIPTS_DIRNAME / agent_id / f"{agent_id}.jsonl"
    )
    return candidate if candidate.is_file() else None


def _truncate(text: str, max_len: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


def _tool_use_detail(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _TOOL_DETAIL_KEYS:
        value = payload.get(key)
        if value:
            return _truncate(str(value), 100)
    return ""


def summarize_transcript_tail(
    transcript_path: str | os.PathLike[str],
    *,
    max_entries: int = DEFAULT_TRANSCRIPT_TAIL,
) -> list[str]:
    """Return human-readable lines describing the agent's last actions.

    Reads the local Cursor JSONL transcript and extracts the most recent
    assistant tool invocations (and any non-redacted prose) so a failure with
    an empty SDK ``result`` still shows what the agent was doing. Tool *results*
    are not present in the export, so this is a best-effort action trail.
    """
    try:
        raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    entries: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("role") == "user":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                name = str(block.get("name") or "tool")
                detail = _tool_use_detail(block.get("input"))
                entries.append(f"{name}{(': ' + detail) if detail else ''}")
            elif block_type == "text":
                text = " ".join(str(block.get("text") or "").split())
                if text and text != "[REDACTED]":
                    entries.append(f'"{_truncate(text, 160)}"')

    return entries[-max_entries:]


def format_failure_report(
    exc: AgentRunError,
    *,
    project_root: str | os.PathLike[str],
    home: Path | None = None,
) -> str:
    """Build a multi-line, actionable failure report for stderr.

    Includes the phase, agent/run ids, SDK detail (or an explicit "no detail"),
    the resolved local transcript path when it exists on disk, and the agent's
    last actions parsed from that transcript.
    """
    phase = exc.phase or "agent"
    header = f"Agent run failed during {phase} (kind={exc.kind.value}, exit {exc.exit_code})"
    lines = [header]

    if exc.agent_id:
        lines.append(f"  agent:      {exc.agent_id}")
    if exc.run_id:
        lines.append(f"  run:        {exc.run_id}")

    detail = (exc.result_detail or "").strip()
    lines.append(f"  detail:     {detail or _NO_DETAIL}")

    diagnostic = (getattr(exc, "diagnostic_detail", None) or "").strip()
    if diagnostic:
        lines.append(f"  context:    {diagnostic}")

    if getattr(exc, "same_agent_continued", False):
        lines.append("  recovery:   same-agent continue already attempted")
        if looks_like_productive_run_drop(exc):
            lines.append(
                "              (dropped mid-work; not retrying a fresh agent)"
            )

    transcript = resolve_transcript_path(project_root, exc.agent_id, home=home)
    lines.append(f"  transcript: {transcript if transcript is not None else _NO_TRANSCRIPT}")

    if transcript is not None:
        tail = summarize_transcript_tail(transcript)
        if tail:
            lines.append("  last activity (from transcript):")
            lines.extend(f"    - {entry}" for entry in tail)

    return "\n".join(lines)


__all__ = [
    "encode_project_slug",
    "format_failure_report",
    "resolve_transcript_path",
    "summarize_transcript_tail",
]
