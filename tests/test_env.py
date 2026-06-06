"""Tests for task 3: project-root .env loading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.cli import _startup_load_env, main
from cyclopsctl.env import EnvLoadError, load_project_env, project_env_path


def test_project_env_path_resolves_under_root(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    assert project_env_path(root) == (root / ".env").resolve()


def test_load_project_env_loads_unset_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text('CURSOR_API_KEY="from-dotenv"\n', encoding="utf-8")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    loaded = load_project_env(root)

    assert loaded == (root / ".env").resolve()
    assert __import__("os").environ["CURSOR_API_KEY"] == "from-dotenv"


def test_load_project_env_preserves_existing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text('CURSOR_API_KEY="from-dotenv"\n', encoding="utf-8")
    monkeypatch.setenv("CURSOR_API_KEY", "already-set")

    load_project_env(root)

    assert __import__("os").environ["CURSOR_API_KEY"] == "already-set"


def test_load_project_env_missing_file_is_no_op(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()

    assert load_project_env(root) is None


def test_startup_load_env_skips_when_no_env(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text('CURSOR_API_KEY="from-dotenv"\n', encoding="utf-8")

    with patch("cyclopsctl.cli.load_project_env") as mock_load:
        _startup_load_env(project_root=root, no_env=True)

    mock_load.assert_not_called()


def test_load_project_env_unreadable_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    env_file = root / ".env"
    env_file.write_text("CURSOR_API_KEY=secret\n", encoding="utf-8")

    original_read_bytes = Path.read_bytes

    def blocked_read_bytes(self: Path) -> bytes:
        if self == env_file.resolve():
            raise OSError("permission denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", blocked_read_bytes)

    with pytest.raises(EnvLoadError, match="Unable to read .env file"):
        load_project_env(root)


def test_cli_run_loads_env_before_preflight(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    (project_tree / ".env").write_text('CURSOR_API_KEY="from-dotenv"\n', encoding="utf-8")

    call_order: list[str] = []

    def track_env(*args, **kwargs):
        call_order.append("env")
        from cyclopsctl.env import load_project_env as real_load

        return real_load(*args, **kwargs)

    def track_preflight(*args, **kwargs):
        call_order.append("preflight")
        from cyclopsctl.preflight import run_preflight as real_preflight

        return real_preflight(*args, **kwargs)

    with (
        patch("cyclopsctl.cli.load_project_env", side_effect=track_env),
        patch("cyclopsctl.cli.run_preflight", side_effect=track_preflight),
        patch("cyclopsctl.cli.run_cycles") as mock_run_cycles,
    ):
        from cyclopsctl.loop import RunLoopResult

        mock_run_cycles.return_value = RunLoopResult(completed_cycles=0, empty_queue=True)
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
    assert call_order == ["env", "preflight"]


def test_cli_run_no_env_skips_loading(project_tree: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    (project_tree / ".env").write_text('CURSOR_API_KEY="from-dotenv"\n', encoding="utf-8")

    with patch("cyclopsctl.cli.load_project_env") as mock_load:
        code = main(
            [
                "run",
                "--no-env",
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

    mock_load.assert_not_called()
    assert code == 2


def test_cli_models_loads_env_before_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text('CURSOR_API_KEY="from-dotenv"\n', encoding="utf-8")

    call_order: list[str] = []

    def track_env(*args, **kwargs):
        call_order.append("env")
        from cyclopsctl.env import load_project_env as real_load

        return real_load(*args, **kwargs)

    def track_inspection(**kwargs):
        call_order.append("inspection")
        return 0

    with (
        patch("cyclopsctl.cli.load_project_env", side_effect=track_env),
        patch("cyclopsctl.cli.run_models_inspection", side_effect=track_inspection),
        patch("cyclopsctl.cli.managed_sdk_bridge"),
    ):
        code = main(["models"])

    assert code == 0
    assert call_order == ["env", "inspection"]


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "prompts" / "first.md").write_text("# First\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text("# Task ID: 1\n", encoding="utf-8")
    (root / "update-handover-prompt.md").write_text("# Update\n", encoding="utf-8")
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        "{}", encoding="utf-8"
    )
    return root.resolve()
