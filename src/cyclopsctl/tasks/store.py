"""Native task storage layout, atomic JSON persistence, and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cyclopsctl.state import write_atomic
from cyclopsctl.tasks.backend import TaskBackendKind

DEFAULT_TAG = "master"

NATIVE_TASKS_JSON_REL = Path(".cyclopsctl/tasks/tasks.json")
NATIVE_TAG_STATE_REL = Path(".cyclopsctl/tasks/state.json")
NATIVE_COMPLEXITY_REPORT_REL = Path(".cyclopsctl/reports/complexity-report.json")


def native_tasks_storage_exists(project_root: Path) -> bool:
    """Return True when native ``tasks.json`` is present."""
    return (project_root / NATIVE_TASKS_JSON_REL).is_file()


class TaskStoreError(Exception):
    """Base error for task storage operations."""


class TaskStoreNotFoundError(TaskStoreError):
    """Raised when a required storage file is missing."""


class TaskStoreCorruptError(TaskStoreError):
    """Raised when a storage file contains invalid JSON or schema."""


class TaskStoreValidationError(TaskStoreError):
    """Raised when task data fails validation before persistence."""


class CircularDependencyError(TaskStoreValidationError):
    """Raised when task dependencies contain a cycle."""


@dataclass(frozen=True)
class TagState:
    """Current tag pointer stored separately from the task queue."""

    current_tag: str
    last_switched: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"currentTag": self.current_tag}
        if self.last_switched is not None:
            payload["lastSwitched"] = self.last_switched
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TagState:
        current = data.get("currentTag")
        if not isinstance(current, str) or not current.strip():
            raise TaskStoreCorruptError("tag state missing currentTag")
        last_switched = data.get("lastSwitched")
        if last_switched is not None and not isinstance(last_switched, str):
            raise TaskStoreCorruptError("tag state lastSwitched must be a string")
        return cls(current_tag=current.strip(), last_switched=last_switched)


def resolve_tasks_json_path(
    project_root: Path,
    *,
    backend: TaskBackendKind = "native",
) -> Path:
    """Return the tasks.json path for the selected backend."""
    _ = backend
    return (project_root / NATIVE_TASKS_JSON_REL).resolve()


def resolve_tag_state_path(
    project_root: Path,
    *,
    backend: TaskBackendKind = "native",
) -> Path:
    """Return the tag state file path for the selected backend."""
    _ = backend
    return (project_root / NATIVE_TAG_STATE_REL).resolve()


def resolve_complexity_report_path(
    project_root: Path,
    *,
    backend: TaskBackendKind = "native",
    override: Path | None = None,
) -> Path:
    """Return the complexity report path for the selected backend or override."""
    if override is not None:
        candidate = override.expanduser()
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return candidate
    _ = backend
    return (project_root / NATIVE_COMPLEXITY_REPORT_REL).resolve()


def ensure_native_layout(project_root: Path) -> None:
    """Create native storage directories when missing."""
    (project_root / ".cyclopsctl" / "tasks").mkdir(parents=True, exist_ok=True)
    (project_root / ".cyclopsctl" / "reports").mkdir(parents=True, exist_ok=True)


def _read_json_file(path: Path) -> Any:
    if not path.is_file():
        raise TaskStoreNotFoundError(f"storage file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskStoreCorruptError(f"unable to read storage file: {path}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskStoreCorruptError(
            f"storage file contains invalid JSON: {path}: {exc}"
        ) from exc


def _write_json_file(path: Path, data: Any) -> None:
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    write_atomic(path, content)


def _normalize_legacy_tasks_document(data: Any) -> Any:
    """
    Migrate a flat ``{"tasks": [...]}`` document into the tag-keyed shape.

    Older flat task JSON written directly by an agent uses a single top-level
    ``tasks`` array instead of the ``{tag: {"tasks": [...]}}``
    layout. A genuine tag-keyed document never has a list under ``tasks`` (every
    top-level value is a tag object), so detecting a list is an unambiguous
    legacy signal. The flat content is wrapped under the default tag.
    """
    if not isinstance(data, dict):
        return data
    flat_tasks = data.get("tasks")
    if not isinstance(flat_tasks, list):
        return data

    tag_entry: dict[str, Any] = {"tasks": flat_tasks}
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        tag_entry["metadata"] = metadata
    return {DEFAULT_TAG: tag_entry}


def _validate_tasks_document(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TaskStoreCorruptError("tasks document must be a JSON object")
    for tag_name, tag_data in data.items():
        if not isinstance(tag_name, str) or not tag_name.strip():
            raise TaskStoreCorruptError("tasks document tag keys must be non-empty strings")
        if not isinstance(tag_data, dict):
            raise TaskStoreCorruptError(
                f"tasks document entry for tag {tag_name!r} must be an object"
            )
        tasks = tag_data.get("tasks")
        if not isinstance(tasks, list):
            raise TaskStoreCorruptError(
                f"tasks document entry for tag {tag_name!r} must include a tasks array"
            )
    return data


def _task_id_str(task: dict[str, Any]) -> str:
    task_id = task.get("id")
    if task_id is None:
        raise TaskStoreValidationError("task missing id")
    return str(task_id)


def validate_no_circular_dependencies(tasks: list[dict[str, Any]]) -> None:
    """Reject task queues that contain dependency cycles."""
    graph: dict[str, list[str]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise TaskStoreValidationError("each task must be a JSON object")
        task_id = _task_id_str(task)
        dependencies = task.get("dependencies", [])
        if dependencies is None:
            dependencies = []
        if not isinstance(dependencies, list):
            raise TaskStoreValidationError(
                f"task {task_id} dependencies must be a list"
            )
        graph[task_id] = [str(dep) for dep in dependencies]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise CircularDependencyError(
                f"circular dependency detected involving task {node}"
            )
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for task_id in graph:
        visit(task_id)


def load_tasks_document(path: Path) -> dict[str, Any]:
    """Load the full tag-keyed tasks document from disk."""
    data = _read_json_file(path)
    data = _normalize_legacy_tasks_document(data)
    return _validate_tasks_document(data)


def save_tasks_document(
    path: Path,
    data: dict[str, Any],
    *,
    validate_cycles: bool = True,
) -> None:
    """Persist the tag-keyed tasks document with atomic write and validation."""
    document = _validate_tasks_document(data)
    if validate_cycles:
        for tag_data in document.values():
            tasks = tag_data.get("tasks", [])
            if isinstance(tasks, list):
                validate_no_circular_dependencies(tasks)
    _write_json_file(path, document)


def load_tag_tasks(
    path: Path,
    tag: str = DEFAULT_TAG,
) -> list[dict[str, Any]]:
    """Load the task queue for a single tag."""
    document = load_tasks_document(path)
    tag_data = document.get(tag)
    if not isinstance(tag_data, dict):
        raise TaskStoreNotFoundError(f"tag not found in tasks document: {tag}")
    tasks = tag_data.get("tasks")
    if not isinstance(tasks, list):
        raise TaskStoreCorruptError(f"tag {tag!r} is missing a tasks array")
    return tasks


def save_tag_tasks(
    path: Path,
    tasks: list[dict[str, Any]],
    *,
    tag: str = DEFAULT_TAG,
    merge: bool = True,
    validate_cycles: bool = True,
) -> None:
    """Persist tasks for one tag, optionally merging with existing tags."""
    if merge and path.is_file():
        try:
            document = load_tasks_document(path)
        except TaskStoreNotFoundError:
            document = {}
        except TaskStoreCorruptError:
            raise
    else:
        document = {}

    if validate_cycles:
        validate_no_circular_dependencies(tasks)

    document[tag] = {"tasks": tasks}
    save_tasks_document(path, document, validate_cycles=False)


def load_tag_state(path: Path) -> TagState:
    """Load current tag state, defaulting to ``master`` when the file is missing."""
    if not path.is_file():
        return TagState(current_tag=DEFAULT_TAG)
    data = _read_json_file(path)
    if not isinstance(data, dict):
        raise TaskStoreCorruptError("tag state must be a JSON object")
    return TagState.from_dict(data)


def save_tag_state(path: Path, state: TagState) -> None:
    """Persist tag state with an atomic write."""
    _write_json_file(path, state.to_dict())


def set_current_tag(path: Path, tag: str) -> TagState:
    """Update and persist the active tag pointer."""
    normalized = tag.strip()
    if not normalized:
        raise TaskStoreValidationError("tag name must be non-empty")
    state = TagState(
        current_tag=normalized,
        last_switched=datetime.now(timezone.utc).isoformat(),
    )
    save_tag_state(path, state)
    return state


def load_complexity_report(path: Path) -> dict[str, Any]:
    """Load a complexity report JSON document."""
    data = _read_json_file(path)
    if not isinstance(data, dict):
        raise TaskStoreCorruptError("complexity report must be a JSON object")
    return data


def save_complexity_report(path: Path, data: dict[str, Any]) -> None:
    """Persist a complexity report with an atomic write."""
    if not isinstance(data, dict):
        raise TaskStoreValidationError("complexity report must be a JSON object")
    _write_json_file(path, data)


@dataclass
class TaskStore:
    """Backend-aware helpers for task queue, tag state, and report persistence."""

    project_root: Path
    backend: TaskBackendKind = "native"

    @property
    def tasks_path(self) -> Path:
        return resolve_tasks_json_path(self.project_root, backend=self.backend)

    @property
    def tag_state_path(self) -> Path:
        return resolve_tag_state_path(self.project_root, backend=self.backend)

    def complexity_report_path(self, override: Path | None = None) -> Path:
        return resolve_complexity_report_path(
            self.project_root,
            backend=self.backend,
            override=override,
        )

    def ensure_native_layout(self) -> None:
        if self.backend != "native":
            raise TaskStoreError("native layout is only available for the native backend")
        ensure_native_layout(self.project_root)

    def load_tasks_document(self) -> dict[str, Any]:
        return load_tasks_document(self.tasks_path)

    def save_tasks_document(
        self,
        data: dict[str, Any],
        *,
        validate_cycles: bool = True,
    ) -> None:
        save_tasks_document(
            self.tasks_path,
            data,
            validate_cycles=validate_cycles,
        )

    def load_tag_tasks(self, tag: str | None = None) -> list[dict[str, Any]]:
        return load_tag_tasks(self.tasks_path, tag=tag or self.current_tag())

    def save_tag_tasks(
        self,
        tasks: list[dict[str, Any]],
        *,
        tag: str | None = None,
        merge: bool = True,
        validate_cycles: bool = True,
    ) -> None:
        save_tag_tasks(
            self.tasks_path,
            tasks,
            tag=tag or self.current_tag(),
            merge=merge,
            validate_cycles=validate_cycles,
        )

    def current_tag(self) -> str:
        return load_tag_state(self.tag_state_path).current_tag

    def load_tag_state(self) -> TagState:
        return load_tag_state(self.tag_state_path)

    def save_tag_state(self, state: TagState) -> None:
        save_tag_state(self.tag_state_path, state)

    def set_current_tag(self, tag: str) -> TagState:
        return set_current_tag(self.tag_state_path, tag)

    def load_complexity_report(self, override: Path | None = None) -> dict[str, Any]:
        return load_complexity_report(self.complexity_report_path(override))

    def save_complexity_report(
        self,
        data: dict[str, Any],
        override: Path | None = None,
    ) -> None:
        save_complexity_report(self.complexity_report_path(override), data)
