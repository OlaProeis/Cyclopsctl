"""Tests for run-loop task hook resolution."""

from __future__ import annotations

from cyclopsctl.tasks.backend import resolve_run_task_hooks


def test_resolve_run_task_hooks_native_returns_callables():
    get_next, list_pending, get_by_id = resolve_run_task_hooks("native")

    assert get_next.__module__ == "cyclopsctl.tasks.cli"
    assert list_pending.__name__ == "list_pending_task_results"
    assert get_by_id.__name__ == "get_task_by_id"
