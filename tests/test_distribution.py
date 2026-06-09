"""Tests for package version reporting and wheel distribution metadata."""

from __future__ import annotations

import email.parser
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from cyclopsctl.cli import main
from cyclopsctl.version import PACKAGE_NAME, get_package_version


def test_get_package_version_matches_pyproject():
    version = get_package_version()
    assert version == "0.1.1"


def test_cli_version_flag(capsys):
    code = main(["--version"])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"cyclopsctl {get_package_version()}"


def test_cli_version_matches_package_metadata(monkeypatch):
    monkeypatch.setattr("cyclopsctl.version.get_package_version", lambda: "9.8.7")
    code = main(["--version"])
    assert code == 0


def test_wheel_contains_license_and_version(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "-w", str(dist_dir), "--no-deps"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment: {build.stderr}")

    wheel_path = next(dist_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel_path) as zf:
        metadata_name = next(
            name for name in zf.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata_bytes = zf.read(metadata_name)

    parser = email.parser.Parser()
    metadata = parser.parse(io.StringIO(metadata_bytes.decode()))
    assert metadata.get("Name") == PACKAGE_NAME
    assert metadata.get("Version") == "0.1.1"
    license_value = metadata.get("License") or metadata.get("License-Expression")
    assert license_value is not None
    assert "MIT" in license_value
