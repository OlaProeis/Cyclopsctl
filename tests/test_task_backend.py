"""Tests for TaskBackend protocol and factory (native only)."""

from __future__ import annotations

import pytest

from cyclopsctl.tasks import (
    NextTaskLookup,
    NextTaskResult,
    TaskBackend,
    TaskBackendConfig,
    TaskShowDetail,
    get_task_backend,
    normalize_task_backend,
    resolve_task_backend,
)
from cyclopsctl.tasks.backend import (
    DEFAULT_TASK_BACKEND,
    TASK_BACKEND_PROTOCOL_METHODS,
    TaskBackendError,
    backend_satisfies_protocol,
    detect_task_backend_from_storage,
    task_backend_explicitly_configured,
    task_backend_fallback_notice,
)
from cyclopsctl.tasks.native_backend import NativeTaskBackend
from cyclopsctl.tasks.types import (
    NextTaskLookup as TypesNextTaskLookup,
    NextTaskResult as TypesNextTaskResult,
    TaskShowDetail as TypesTaskShowDetail,
)


def test_shared_dataclasses_import_from_tasks_package():
    assert NextTaskResult is TypesNextTaskResult
    assert TaskShowDetail is TypesTaskShowDetail
    assert NextTaskLookup is TypesNextTaskLookup


def test_normalize_task_backend_accepts_native():
    assert normalize_task_backend("native") == "native"
    assert normalize_task_backend("Native") == "native"


def test_normalize_task_backend_rejects_unknown_value():
    with pytest.raises(TaskBackendError, match="task-backend"):
        normalize_task_backend("external")
    with pytest.raises(TaskBackendError, match="task-backend"):
        normalize_task_backend("sqlite")


def test_resolve_task_backend_always_native():
    assert resolve_task_backend({}, {}) == DEFAULT_TASK_BACKEND
    assert resolve_task_backend({"task_backend": "legacy"}, {}) == "native"
    file_cfg = {"tasks": {"backend": "legacy"}}
    assert resolve_task_backend({}, file_cfg) == "native"


def test_get_task_backend_returns_native_implementation():
    backend = get_task_backend(TaskBackendConfig())
    assert isinstance(backend, NativeTaskBackend)


def test_backend_contract_exposes_required_methods():
    backend = get_task_backend(TaskBackendConfig())
    assert backend_satisfies_protocol(backend)
    for method_name in TASK_BACKEND_PROTOCOL_METHODS:
        assert hasattr(backend, method_name)
        assert callable(getattr(backend, method_name))


def test_backend_satisfies_runtime_protocol():
    backend = get_task_backend(TaskBackendConfig())
    assert isinstance(backend, TaskBackend)


def test_native_backend_init_installs_orchestrator_rules(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    NativeTaskBackend().init_project(root, rules=("cursor",))

    orchestrator_rule = (
        root / ".cursor" / "rules" / "cyclopsctl" / "agent-workflow.mdc"
    )
    assert orchestrator_rule.is_file()
    rule_text = orchestrator_rule.read_text(encoding="utf-8")
    assert "cyclopsctl tasks set-status" in rule_text


def test_native_backend_complexity_report_path(tmp_path):
    backend = NativeTaskBackend()
    report = backend.complexity_report_path(tmp_path)
    assert report == (tmp_path / ".cyclopsctl/reports/complexity-report.json").resolve()


def test_task_backend_explicitly_configured_detects_cli_and_file_keys():
    assert task_backend_explicitly_configured({"task_backend": "native"}, {}) is True
    assert task_backend_explicitly_configured(
        {},
        {"tasks": {"backend": "legacy"}},
    ) is True
    assert task_backend_explicitly_configured({}, {"task_backend": "native"}) is True
    assert task_backend_explicitly_configured({}, {}) is False


def test_detect_task_backend_from_storage_always_native(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert detect_task_backend_from_storage(root) == DEFAULT_TASK_BACKEND


def test_task_backend_fallback_notice_always_none(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert (
        task_backend_fallback_notice(root, "native", explicitly_configured=False)
        is None
    )
