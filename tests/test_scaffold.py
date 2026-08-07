"""Smoke tests for task 1: package layout and CLI entrypoint."""

from __future__ import annotations

import subprocess
import sys

import cyclopsctl
import cyclopsctl.cli
import cyclopsctl.config
import cyclopsctl.logging as cyclopsctl_logging
import cyclopsctl.prompt
import cyclopsctl.routing
import cyclopsctl.runner
import cyclopsctl.session
import cyclopsctl.tasks
import cyclopsctl.verify
from cyclopsctl.cli import main


def test_package_version():
    assert cyclopsctl.__version__ == "0.1.7"


def test_all_modules_importable():
    modules = [
        cyclopsctl.cli,
        cyclopsctl.config,
        cyclopsctl.tasks,
        cyclopsctl.routing,
        cyclopsctl.prompt,
        cyclopsctl.verify,
        cyclopsctl.runner,
        cyclopsctl.session,
        cyclopsctl_logging,
    ]
    for mod in modules:
        assert mod.__doc__


def test_cli_help_exits_zero(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "cyclopsctl" in out
    assert "run" in out
    assert "launch" in out
    assert "models" in out


def test_cli_run_subcommand_help():
    assert main(["run", "--help"]) == 0


def test_cli_models_subcommand_help():
    assert main(["models", "--help"]) == 0


def test_orchestrator_module_help():
    result = subprocess.run(
        [sys.executable, "-m", "cyclopsctl.cli", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0
    assert "Orchestrate Cursor agent runs" in result.stdout


def test_orchestrator_console_script_resolves():
    result = subprocess.run(
        ["cyclopsctl", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0
    assert "run" in result.stdout
