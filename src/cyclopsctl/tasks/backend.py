"""Task backend protocol and factory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from cyclopsctl.tasks.native_backend import NativeTaskBackend
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult, TaskShowDetail

TaskBackendKind = Literal["native"]
DEFAULT_TASK_BACKEND: TaskBackendKind = "native"
VALID_TASK_BACKENDS: frozenset[str] = frozenset({"native"})

TASK_BACKEND_PROTOCOL_METHODS: tuple[str, ...] = (
    "init_project",
    "parse_prd",
    "analyze_complexity",
    "list_pending",
    "get_next",
    "show",
    "set_status",
    "add_tag",
    "use_tag",
    "current_tag",
    "complexity_report_path",
    "tasks_exist",
)


class TaskBackendError(ValueError):
    """Invalid task backend configuration."""


def normalize_task_backend(value: str) -> TaskBackendKind:
    """Validate and normalize a task backend setting (native only)."""
    normalized = str(value).strip().lower()
    if normalized not in VALID_TASK_BACKENDS:
        allowed = ", ".join(sorted(VALID_TASK_BACKENDS))
        raise TaskBackendError(
            f"task-backend must be one of: {allowed} (got: {value!r})"
        )
    return normalized  # type: ignore[return-value]


def task_backend_explicitly_configured(
    cli: Mapping[str, object],
    file_cfg: Mapping[str, object],
) -> bool:
    """Return True when backend is set via CLI or config file."""
    if cli.get("task_backend") is not None:
        return True

    tasks_section = file_cfg.get("tasks")
    if isinstance(tasks_section, dict) and tasks_section.get("backend") is not None:
        return True

    return file_cfg.get("task_backend") is not None


def detect_task_backend_from_storage(project_root: Path) -> TaskBackendKind:
    """Always return native; task storage lives under ``.cyclopsctl/tasks/``."""
    _ = project_root
    return DEFAULT_TASK_BACKEND


def task_backend_fallback_notice(
    project_root: Path,
    backend: TaskBackendKind,
    *,
    explicitly_configured: bool,
) -> str | None:
    """No fallback notice; native backend is the only storage path."""
    _ = (project_root, backend, explicitly_configured)
    return None


def resolve_task_backend(
    cli: Mapping[str, object],
    file_cfg: Mapping[str, object],
    *,
    project_root: Path | None = None,
) -> TaskBackendKind:
    """Resolve backend; always native."""
    _ = (cli, file_cfg, project_root)
    return DEFAULT_TASK_BACKEND


@dataclass(frozen=True)
class TaskBackendConfig:
    """Minimal configuration consumed by ``get_task_backend``."""

    task_backend: TaskBackendKind = DEFAULT_TASK_BACKEND


@runtime_checkable
class TaskBackend(Protocol):
    """Backend-agnostic task storage and CLI surface."""

    def init_project(
        self,
        project_root: Path,
        *,
        rules: tuple[str, ...] = ("cursor",),
    ) -> None: ...

    def parse_prd(
        self,
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        parse_model: str | None = None,
        max_tasks: int | None = None,
    ) -> None: ...

    def analyze_complexity(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
        analyze_model: str | None = None,
    ) -> None: ...

    def list_pending(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
    ) -> list[NextTaskResult]: ...

    def get_next(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
    ) -> NextTaskLookup: ...

    def show(
        self,
        project_root: Path,
        task_id: str,
        *,
        tag: str | None = None,
    ) -> TaskShowDetail: ...

    def set_status(
        self,
        project_root: Path,
        task_id: str,
        status: str,
        *,
        tag: str | None = None,
    ) -> None: ...

    def add_tag(
        self,
        project_root: Path,
        name: str,
        *,
        copy_from: str | None = None,
    ) -> None: ...

    def use_tag(self, project_root: Path, name: str) -> None: ...

    def current_tag(self, project_root: Path) -> str: ...

    def complexity_report_path(self, project_root: Path) -> Path: ...

    def tasks_exist(self, project_root: Path, *, tag: str | None = None) -> bool: ...


def get_task_backend(config: TaskBackendConfig) -> TaskBackend:
    """Return the native task backend implementation."""
    _ = config
    return NativeTaskBackend()


def backend_satisfies_protocol(backend: object) -> bool:
    """Return True when ``backend`` exposes every ``TaskBackend`` method."""
    return all(
        hasattr(backend, name) and callable(getattr(backend, name))
        for name in TASK_BACKEND_PROTOCOL_METHODS
    )


def resolve_run_task_hooks(
    task_backend: TaskBackendKind,
    *,
    runner: object | None = None,
) -> tuple[object, object, object]:
    """
    Return ``(get_next, list_pending, get_task_by_id)`` callables for ``run_cycles``.

    Uses native ``tasks/cli.py`` helpers.
    """
    _ = (task_backend, runner)
    from cyclopsctl.tasks.cli import (
        get_next_task as native_get_next,
        get_task_by_id as native_get_task_by_id,
        list_pending_task_results,
    )

    list_pending = list_pending_task_results

    def get_task_by_id(
        project_root: Path,
        task_id: int | str,
        *,
        tag: str | None = None,
    ) -> NextTaskLookup:
        return native_get_task_by_id(project_root, str(task_id), tag=tag)

    return native_get_next, list_pending, get_task_by_id
