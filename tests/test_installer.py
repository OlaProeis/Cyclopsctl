"""Tests for global install helpers and PATH remediation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cyclopsctl.installer import (
    DEFAULT_GIT_URL,
    PACKAGE_NAME,
    InstallVerifyResult,
    build_pip_install_argv,
    command_on_path,
    format_path_remediation,
    missing_commands,
    run_install,
    validate_python_version,
    verify_installation,
)


def test_validate_python_version_accepts_310():
    assert validate_python_version((3, 10, 0)) is None


def test_validate_python_version_rejects_39():
    message = validate_python_version((3, 9, 18))
    assert message is not None
    assert "3.10" in message
    assert "3.9.18" in message


def test_command_on_path_finds_executable(tmp_path):
    script = tmp_path / "cyclopsctl.cmd"
    script.write_text("@echo off\n", encoding="utf-8")
    assert command_on_path("cyclopsctl", [str(tmp_path)], is_windows=True)


def test_command_on_path_missing_command(tmp_path):
    assert not command_on_path("cyclopsctl", [str(tmp_path)], is_windows=True)


def test_missing_commands_reports_orchestrator():
    missing = missing_commands(
        ["cyclopsctl"],
        ["/empty"],
        is_windows=False,
    )
    assert missing == ["cyclopsctl"]


def test_format_path_remediation_windows():
    text = format_path_remediation(
        ["cyclopsctl"],
        python_scripts_dir=r"C:\Users\dev\AppData\Roaming\Python\Python311\Scripts",
        is_windows=True,
    )
    assert "cyclopsctl" in text
    assert "setx PATH" in text
    assert r"Python311\Scripts" in text


def test_format_path_remediation_unix():
    text = format_path_remediation(
        ["cyclopsctl"],
        python_scripts_dir="/home/dev/.local/bin",
        is_windows=False,
    )
    assert "export PATH=" in text
    assert "/home/dev/.local/bin" in text


def test_build_pip_install_argv_git_default_url():
    argv = build_pip_install_argv(source="git")
    assert argv[-1] == DEFAULT_GIT_URL
    assert "pip" in argv[2]


def test_build_pip_install_argv_git():
    argv = build_pip_install_argv(source="git", git_url="git+https://example.com/repo.git")
    assert argv[-1] == "git+https://example.com/repo.git"


def test_build_pip_install_argv_local(tmp_path):
    argv = build_pip_install_argv(source="local", local_path=tmp_path)
    assert argv[-1] == str(tmp_path.resolve())


def test_build_pip_install_argv_local_requires_path():
    with pytest.raises(ValueError, match="local_path"):
        build_pip_install_argv(source="local")


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_verify_installation_success():
    def fake_run(argv, **kwargs):
        if argv[0] == "cyclopsctl":
            return _FakeCompletedProcess(0, stdout="usage")
        raise AssertionError(f"unexpected argv: {argv}")

    result = verify_installation(run=fake_run)
    assert result == InstallVerifyResult(
        cyclopsctl_ok=True,
        cyclopsctl_error="usage",
    )


def test_verify_installation_orchestrator_failure():
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(1, stderr="not found")

    result = verify_installation(run=fake_run)
    assert result.cyclopsctl_ok is False
    assert "not found" in result.cyclopsctl_error


def test_run_install_success_with_remediation(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["python", "-m", "pip"] or argv[2:4] == ["-m", "pip"]:
            return _FakeCompletedProcess(0, stdout="installed")
        if argv[0] == "cyclopsctl":
            return _FakeCompletedProcess(1, stderr="missing")
        if "-c" in argv:
            return _FakeCompletedProcess(0, stdout=r"C:\Scripts")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr("cyclopsctl.installer.sys.version_info", (3, 11, 0))
    monkeypatch.setattr(
        "cyclopsctl.installer.build_pip_install_argv",
        lambda **kwargs: ["python", "-m", "pip", "install", PACKAGE_NAME],
    )

    result = run_install(
        source="git",
        run=fake_run,
        path_entries=[r"C:\Windows"],
        is_windows=True,
    )

    assert result.exit_code == 2
    assert any("cyclopsctl package installed" in message for message in result.messages)
    assert any("setx PATH" in message for message in result.messages)
    assert result.verify is not None
    assert result.verify.cyclopsctl_ok is False


def test_run_install_pip_failure(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(1, stderr="pip exploded")

    monkeypatch.setattr("cyclopsctl.installer.sys.version_info", (3, 11, 0))
    monkeypatch.setattr(
        "cyclopsctl.installer.build_pip_install_argv",
        lambda **kwargs: ["python", "-m", "pip", "install", PACKAGE_NAME],
    )

    result = run_install(source="git", run=fake_run, path_entries=[])
    assert result.exit_code == 1
    assert any("pip install failed" in message for message in result.messages)


def test_run_install_rejects_old_python(monkeypatch):
    monkeypatch.setattr("cyclopsctl.installer.sys.version_info", (3, 9, 0))
    result = run_install(source="git", run=lambda *a, **k: None, path_entries=[])
    assert result.exit_code == 1
    assert "3.10" in result.messages[0]


def test_installer_module_main_verify_only(monkeypatch, capsys):
    monkeypatch.setattr(
        "cyclopsctl.installer.verify_installation",
        lambda **kwargs: InstallVerifyResult(cyclopsctl_ok=True),
    )
    monkeypatch.setattr("cyclopsctl.installer.missing_commands", lambda *a, **k: [])

    from cyclopsctl.installer import main

    assert main(["--verify-only"]) == 0
    out = capsys.readouterr().out
    assert "cyclopsctl --help: OK" in out


def test_default_git_url_is_https():
    assert DEFAULT_GIT_URL.startswith("git+https://")
