"""Git diff summary helpers for run observability (task 16)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

RunFn = Callable[..., subprocess.CompletedProcess[str]]


def _default_run_fn(args: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def capture_git_head(
    project_root: Path,
    *,
    run_fn: RunFn | None = None,
    timeout: float = 10.0,
) -> str | None:
    """Return ``HEAD`` commit hash when ``project_root`` is a git repository."""
    runner = run_fn or _default_run_fn
    try:
        completed = runner(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip()
    return head or None


def capture_git_diff_stat_since(
    project_root: Path,
    start_head: str,
    *,
    run_fn: RunFn | None = None,
    timeout: float = 10.0,
) -> str | None:
    """
    Return ``git diff --stat`` output from ``start_head`` to the working tree.

    Returns ``None`` when git is unavailable or the repository cannot be read.
    Returns an empty string when there are no changes.
    """
    runner = run_fn or _default_run_fn
    try:
        completed = runner(
            ["git", "diff", "--stat", start_head],
            cwd=project_root,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def capture_cycle_git_diff_summary(
    project_root: Path,
    start_head: str | None,
    *,
    run_fn: RunFn | None = None,
) -> str | None:
    """
    Capture a per-cycle diff summary when git is available.

    When ``start_head`` is missing, attempts to resolve ``HEAD`` at capture time.
    Returns ``None`` when git is unavailable; returns ``None`` when there are no changes.
    """
    head = start_head or capture_git_head(project_root, run_fn=run_fn)
    if head is None:
        return None
    stat = capture_git_diff_stat_since(project_root, head, run_fn=run_fn)
    if stat is None or not stat:
        return None
    return stat
