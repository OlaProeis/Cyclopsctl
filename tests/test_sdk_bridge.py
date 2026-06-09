"""Tests for Windows-safe Cursor SDK bridge bootstrap."""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cyclopsctl.sdk_bridge import (
    ManagedBridge,
    SdkBridgeError,
    bridge_env_configured,
    ensure_sdk_bridge,
    launch_bridge_for_windows,
    managed_sdk_bridge,
    needs_windows_bridge_bootstrap,
)


READY_LINE = (
    'cursor-sdk-bridge ready {"schemaVersion":1,"serverVersion":"1.0.0",'
    '"pid":1,"transport":"tcp","protocol":"connect","host":"127.0.0.1",'
    '"port":8765,"url":"http://127.0.0.1:8765","authToken":"bridge-token",'
    '"workspaceRef":"/tmp/repo","stateRoot":"/tmp/state"}\n'
)


class _FakeStderr(StringIO):
    def fileno(self) -> int:
        return 2


class _FakeProcess:
    def __init__(self, *, stderr_text: str = READY_LINE, returncode: int | None = None) -> None:
        self.stderr = _FakeStderr(stderr_text)
        self._returncode = returncode
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 1
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode or 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = 1
        self.returncode = 1


def test_bridge_env_configured_requires_url_and_token(monkeypatch):
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_URL", raising=False)
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_TOKEN", raising=False)
    assert bridge_env_configured() is False

    monkeypatch.setenv("CURSOR_SDK_BRIDGE_URL", "http://127.0.0.1:8765")
    assert bridge_env_configured() is False

    monkeypatch.setenv("CURSOR_SDK_BRIDGE_TOKEN", "token")
    assert bridge_env_configured() is True


def test_needs_windows_bridge_bootstrap_only_on_windows(monkeypatch):
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_URL", raising=False)
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_TOKEN", raising=False)

    with patch("cyclopsctl.sdk_bridge.sys.platform", "win32"):
        assert needs_windows_bridge_bootstrap() is True

    with patch("cyclopsctl.sdk_bridge.sys.platform", "linux"):
        assert needs_windows_bridge_bootstrap() is False


def test_launch_bridge_for_windows_sets_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_URL", raising=False)
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_TOKEN", raising=False)
    process = _FakeProcess()
    install_calls: list[dict[str, str]] = []

    def _fake_install(**kwargs: str) -> object:
        install_calls.append(kwargs)
        return object()

    bridge = launch_bridge_for_windows(
        tmp_path,
        resolve_bridge_path=lambda: "/bin/cursor-sdk-bridge",
        popen=lambda *_args, **_kwargs: process,
        install_client=_fake_install,
    )

    assert bridge.url == "http://127.0.0.1:8765"
    assert bridge.auth_token == "bridge-token"
    assert os.environ["CURSOR_SDK_BRIDGE_URL"] == bridge.url
    assert os.environ["CURSOR_SDK_BRIDGE_TOKEN"] == bridge.auth_token
    assert install_calls == [
        {"url": "http://127.0.0.1:8765", "auth_token": "bridge-token"}
    ]
    assert bridge.client is not None


def test_install_env_fallback_default_client_uses_env_fallback(monkeypatch):
    import cursor_sdk
    from cursor_sdk import _client as sdk_client

    import cyclopsctl.sdk_bridge as sdk_bridge

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(cursor_sdk, "Client", _FakeClient)
    monkeypatch.setattr(sdk_client, "_DEFAULT_CLIENT", None, raising=False)

    client = sdk_bridge.install_env_fallback_default_client(
        url="http://127.0.0.1:8765",
        auth_token="bridge-token",
    )

    assert captured == {
        "base_url": "http://127.0.0.1:8765",
        "auth_token": "bridge-token",
        "allow_api_key_env_fallback": True,
    }
    assert sdk_client._DEFAULT_CLIENT is client


def test_launch_bridge_for_windows_raises_when_process_exits_early(tmp_path: Path):
    process = _FakeProcess(stderr_text="startup failed\n", returncode=1)

    with pytest.raises(SdkBridgeError, match="Bridge exited before discovery"):
        launch_bridge_for_windows(
            tmp_path,
            resolve_bridge_path=lambda: "/bin/cursor-sdk-bridge",
            popen=lambda *_args, **_kwargs: process,
        )


def test_ensure_sdk_bridge_skips_when_env_already_set(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CURSOR_SDK_BRIDGE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CURSOR_SDK_BRIDGE_TOKEN", "existing")

    with patch("cyclopsctl.sdk_bridge.sys.platform", "win32"):
        assert ensure_sdk_bridge(tmp_path) is None


def test_ensure_sdk_bridge_skips_on_non_windows(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_URL", raising=False)
    monkeypatch.delenv("CURSOR_SDK_BRIDGE_TOKEN", raising=False)

    with patch("cyclopsctl.sdk_bridge.sys.platform", "linux"):
        assert ensure_sdk_bridge(tmp_path) is None


def test_managed_sdk_bridge_closes_launched_bridge(tmp_path: Path):
    process = _FakeProcess()
    closed: list[ManagedBridge] = []

    class _Bridge(ManagedBridge):
        def close(self) -> None:
            closed.append(self)

    with patch(
        "cyclopsctl.sdk_bridge.ensure_sdk_bridge",
        return_value=_Bridge(process=process, url="http://127.0.0.1:8765", auth_token="t"),
    ):
        with managed_sdk_bridge(tmp_path) as bridge:
            assert bridge is not None

    assert len(closed) == 1


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "prompts" / "first.md").write_text("# First-run prompt\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 8\n\nImplement task 8.\n",
        encoding="utf-8",
    )
    (root / "update-handover-prompt.md").write_text(
        "# Update\n\nMark done and advance handover.\n",
        encoding="utf-8",
    )
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 8, "complexityScore": 8}]}',
        encoding="utf-8",
    )
    return root.resolve()


def test_cli_run_uses_managed_bridge(project_tree: Path, monkeypatch):
    from cyclopsctl.cli import main
    from cyclopsctl.loop import RunLoopResult

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch("cyclopsctl.cli.run_cycles") as mock_run:
            mock_run.return_value = RunLoopResult(completed_cycles=1)
            code = main(
                [
                    "run",
                    "--cycles",
                    "1",
                    "--project-root",
                    str(project_tree),
                    "--first-prompt",
                    str(project_tree / "prompts" / "first.md"),
                    "--current-handover",
                    str(project_tree / "current-handover-prompt.md"),
                    "--update-handover",
                    str(project_tree / "update-handover-prompt.md"),
                ]
            )

    assert code == 0
    mock_bridge.assert_called_once_with(project_tree.resolve())


@pytest.fixture
def greenfield_root(tmp_path: Path) -> Path:
    root = tmp_path / "greenfield"
    root.mkdir()
    (root / "prd.md").write_text("# PRD\n\nPython 3.10+", encoding="utf-8")
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    return root.resolve()


def test_cli_init_uses_managed_bridge(
    greenfield_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cyclopsctl.cli import main
    from tests.test_project_setup import _mock_native_parse_analyze

    _mock_native_parse_analyze(monkeypatch)

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        code = main(["init", "--project-root", str(greenfield_root)])

    assert code == 0
    mock_bridge.assert_called_once_with(greenfield_root)


def test_cli_init_dry_run_skips_managed_bridge(greenfield_root: Path):
    from cyclopsctl.cli import main

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        code = main(
            [
                "init",
                "--project-root",
                str(greenfield_root),
                "--dry-run",
                "--no-env",
            ]
        )

    assert code == 0
    mock_bridge.assert_not_called()


def test_cli_launch_uses_managed_bridge(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    from cyclopsctl.cli import main
    from cyclopsctl.launcher import LaunchDispatch

    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch(
            "cyclopsctl.cli.run_launch",
            return_value=LaunchDispatch(action=None, argv=None, exit_code=0),
        ) as mock_launch:
            code = main(
                [
                    "launch",
                    "--project-root",
                    str(project_tree),
                    "--cycles",
                    "1",
                    "--yes",
                ]
            )

    assert code == 0
    mock_bridge.assert_called_once_with(project_tree.resolve())
    mock_launch.assert_called_once()
    assert "bridge_manager" in mock_launch.call_args.kwargs


def test_cli_bootstrap_uses_managed_bridge(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    from cyclopsctl.cli import main
    from cyclopsctl.bootstrap import BootstrapResult

    (project_tree / "prd.md").write_text("# PRD\n", encoding="utf-8")

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        mock_bridge.return_value.__enter__ = MagicMock(return_value=None)
        mock_bridge.return_value.__exit__ = MagicMock(return_value=False)
        with patch("cyclopsctl.cli.run_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = BootstrapResult(
                handover_path=project_tree / "current-handover-prompt.md",
                task_id=8,
                copied_templates=(),
            )
            code = main(["bootstrap", "--project-root", str(project_tree)])

    assert code == 0
    mock_bridge.assert_called_once_with(project_tree.resolve())
    mock_bootstrap.assert_called_once()


def test_cli_bootstrap_sync_handover_skips_managed_bridge(
    project_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cyclopsctl.cli import main
    from cyclopsctl.bootstrap import BootstrapResult

    with patch("cyclopsctl.cli.managed_sdk_bridge") as mock_bridge:
        with patch("cyclopsctl.cli.run_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = BootstrapResult(
                handover_path=project_tree / "current-handover-prompt.md",
                task_id=8,
                copied_templates=(),
            )
            code = main(
                [
                    "bootstrap",
                    "--project-root",
                    str(project_tree),
                    "--sync-handover-only",
                ]
            )

    assert code == 0
    mock_bridge.assert_not_called()
    mock_bootstrap.assert_called_once()
