"""Native task storage backend backed by ``tasks/store.py`` and ``tasks/cli.py``."""

from __future__ import annotations

from pathlib import Path

from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult, TaskShowDetail

NATIVE_DEFAULT_TAG = "master"
NATIVE_COMPLEXITY_REPORT_REL = Path(".cyclopsctl/reports/complexity-report.json")
NATIVE_TASKS_STATE_REL = Path(".cyclopsctl/tasks/state.json")


class NativeTaskBackend:
    """Native storage backend using Cursor SDK for parse and analyze."""

    def _store(self, project_root: Path):
        from cyclopsctl.tasks.store import TaskStore

        return TaskStore(project_root.resolve(), backend="native")

    def init_project(
        self,
        project_root: Path,
        *,
        rules: tuple[str, ...] = ("cursor",),
    ) -> None:
        from cyclopsctl.tasks.store import ensure_native_layout, save_tasks_document
        from cyclopsctl.workflow_gen import (
            install_cyclopsctl_cursor_rules,
            install_cyclopsctl_skill,
        )

        root = project_root.resolve()
        ensure_native_layout(root)
        store = self._store(root)
        if not store.tasks_path.is_file():
            save_tasks_document(store.tasks_path, {})
        if not store.tag_state_path.is_file():
            store.set_current_tag(NATIVE_DEFAULT_TAG)
        if "cursor" in rules:
            install_cyclopsctl_cursor_rules(root, project_root_value=str(root))
            install_cyclopsctl_skill(root, project_root_value=str(root))

    def parse_prd(
        self,
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        parse_model: str | None = None,
        max_tasks: int | None = None,
    ) -> None:
        from cyclopsctl.tasks.models import DEFAULT_PARSE_MODEL
        from cyclopsctl.tasks.parse_prd import ParsePrdConfig, parse_prd_with_cursor

        parse_prd_with_cursor(
            project_root,
            prd,
            tag=tag,
            append=append,
            config=ParsePrdConfig(
                parse_model=parse_model or DEFAULT_PARSE_MODEL,
                max_tasks=max_tasks,
            ),
        )

    def analyze_complexity(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
        analyze_model: str | None = None,
        skip_if_exists: bool = True,
    ) -> None:
        from cyclopsctl.tasks.analyze import (
            AnalyzeComplexityConfig,
            analyze_complexity_with_cursor,
        )
        from cyclopsctl.tasks.models import DEFAULT_ANALYZE_MODEL

        analyze_complexity_with_cursor(
            project_root,
            tag=tag,
            config=AnalyzeComplexityConfig(
                analyze_model=analyze_model or DEFAULT_ANALYZE_MODEL,
                skip_if_exists=skip_if_exists,
            ),
        )

    def list_pending(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
    ) -> list[NextTaskResult]:
        from cyclopsctl.tasks.cli import list_pending_tasks, task_to_next_result

        active_tag, tasks = list_pending_tasks(project_root, tag=tag)
        return [task_to_next_result(task, tag=active_tag) for task in tasks]

    def get_next(
        self,
        project_root: Path,
        *,
        tag: str | None = None,
    ) -> NextTaskLookup:
        from cyclopsctl.tasks.cli import get_next_task

        return get_next_task(project_root, tag=tag)

    def show(
        self,
        project_root: Path,
        task_id: str,
        *,
        tag: str | None = None,
    ) -> TaskShowDetail:
        from cyclopsctl.tasks.cli import get_task_show_detail

        return get_task_show_detail(project_root, task_id, tag=tag)

    def set_status(
        self,
        project_root: Path,
        task_id: str,
        status: str,
        *,
        tag: str | None = None,
    ) -> None:
        from cyclopsctl.tasks.cli import set_task_status

        set_task_status(project_root, task_id, status, tag=tag)

    def add_tag(
        self,
        project_root: Path,
        name: str,
        *,
        copy_from: str | None = None,
    ) -> None:
        from cyclopsctl.tasks.store import load_tasks_document, save_tasks_document

        store = self._store(project_root)
        store.ensure_native_layout()
        normalized = name.strip()
        if not normalized:
            raise ValueError("tag name must be non-empty")

        if store.tasks_path.is_file():
            document = load_tasks_document(store.tasks_path)
        else:
            document = {}

        if normalized in document:
            return

        source_tag = copy_from or store.current_tag()
        source_tasks: list[dict] = []
        if source_tag in document:
            tag_data = document[source_tag]
            if isinstance(tag_data, dict):
                tasks = tag_data.get("tasks")
                if isinstance(tasks, list):
                    source_tasks = [dict(task) for task in tasks if isinstance(task, dict)]

        document[normalized] = {"tasks": source_tasks}
        save_tasks_document(store.tasks_path, document, validate_cycles=False)

    def use_tag(self, project_root: Path, name: str) -> None:
        from cyclopsctl.tasks.store import TaskStoreNotFoundError, load_tasks_document

        store = self._store(project_root)
        normalized = name.strip()
        if not normalized:
            raise ValueError("tag name must be non-empty")
        if store.tasks_path.is_file():
            document = load_tasks_document(store.tasks_path)
            if normalized not in document:
                raise TaskStoreNotFoundError(
                    f"tag not found in tasks document: {normalized}"
                )
        store.set_current_tag(normalized)

    def current_tag(self, project_root: Path) -> str:
        state_path = project_root / NATIVE_TASKS_STATE_REL
        if state_path.is_file():
            try:
                import json

                data = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return NATIVE_DEFAULT_TAG
            if isinstance(data, dict):
                current = data.get("currentTag")
                if isinstance(current, str) and current.strip():
                    return current.strip()
        return NATIVE_DEFAULT_TAG

    def complexity_report_path(self, project_root: Path) -> Path:
        return (project_root / NATIVE_COMPLEXITY_REPORT_REL).resolve()

    def tasks_exist(self, project_root: Path, *, tag: str | None = None) -> bool:
        tasks_path = project_root / ".cyclopsctl/tasks/tasks.json"
        if not tasks_path.is_file():
            return False
        try:
            import json

            data = json.loads(tasks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        tag_name = tag or NATIVE_DEFAULT_TAG
        tag_data = data.get(tag_name)
        if not isinstance(tag_data, dict):
            return False
        tasks = tag_data.get("tasks")
        return isinstance(tasks, list) and len(tasks) > 0
