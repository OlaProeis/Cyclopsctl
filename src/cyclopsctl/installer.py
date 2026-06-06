"""Global install helpers: Python version checks, PATH remediation, verification."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "cyclopsctl"
DEFAULT_GIT_URL = "git+https://github.com/OlaProeis/Cyclopsctl.git"
MIN_PYTHON = (3, 10)

RunFn = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class InstallVerifyResult:
    """Outcome of post-install command verification."""

    cyclopsctl_ok: bool
    cyclopsctl_error: str = ""


@dataclass(frozen=True)
class InstallResult:
    """Summary of an install pipeline run."""

    exit_code: int
    messages: tuple[str, ...]
    verify: InstallVerifyResult | None = None


def validate_python_version(version_info: tuple[int, ...]) -> str | None:
    """Return an error message when Python is below 3.10, else None."""
    if version_info[:2] < MIN_PYTHON:
        current = ".".join(str(part) for part in version_info[:3])
        minimum = ".".join(str(part) for part in MIN_PYTHON)
        return f"Python {minimum}+ is required (found {current})."
    return None


def command_on_path(
    command: str,
    path_entries: Sequence[str],
    *,
    is_windows: bool | None = None,
) -> bool:
    """Return True when *command* resolves on the given PATH entries."""
    if is_windows is None:
        is_windows = sys.platform == "win32"

    extensions: list[str] = [""]
    if is_windows:
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        extensions = [""] + [ext.lower() for ext in pathext.split(";") if ext]

    for directory in path_entries:
        if not directory:
            continue
        base = Path(directory) / command
        for ext in extensions:
            candidate = base if ext == "" else Path(str(base) + ext)
            if candidate.is_file():
                return True
    return False


def missing_commands(
    commands: Sequence[str],
    path_entries: Sequence[str],
    *,
    is_windows: bool | None = None,
) -> list[str]:
    """Return command names that are not discoverable on PATH."""
    return [
        name
        for name in commands
        if not command_on_path(name, path_entries, is_windows=is_windows)
    ]


def format_path_remediation(
    missing: Sequence[str],
    *,
    python_scripts_dir: str | None = None,
    is_windows: bool | None = None,
) -> str:
    """Build human-readable PATH remediation guidance."""
    if is_windows is None:
        is_windows = sys.platform == "win32"

    if not missing:
        return ""

    lines = ["Some installed commands are not on your PATH:", ""]
    for name in missing:
        lines.append(f"  - {name}")

    lines.append("")
    if "cyclopsctl" in missing and python_scripts_dir:
        if is_windows:
            lines.append(
                "Add the Python Scripts directory to your user PATH, then restart the shell:"
            )
            lines.append(f'  setx PATH "%PATH%;{python_scripts_dir}"')
        else:
            lines.append("Add the Python user scripts directory to your PATH:")
            lines.append(f'  export PATH="{python_scripts_dir}:$PATH"')
        lines.append("")

    if is_windows:
        lines.append("Restart PowerShell or your terminal after updating PATH.")
    else:
        lines.append("Restart your shell or add the exports to your shell profile.")

    return "\n".join(lines)


def build_pip_install_argv(
    *,
    source: str,
    git_url: str | None = None,
    local_path: Path | None = None,
) -> list[str]:
    """Build ``python -m pip install`` arguments for the cyclopsctl package."""
    if source == "pypi":
        target = PACKAGE_NAME
    elif source == "git":
        target = git_url or DEFAULT_GIT_URL
    elif source == "local":
        if local_path is None:
            raise ValueError("local_path is required when source is 'local'")
        target = str(local_path.resolve())
    else:
        raise ValueError(f"Unsupported install source: {source}")

    return [sys.executable, "-m", "pip", "install", "--upgrade", target]


def discover_python_scripts_dir(run: RunFn | None = None) -> str | None:
    """Return the user-level Python scripts directory when discoverable."""
    runner = run or subprocess.run
    result = runner(
        [
            sys.executable,
            "-c",
            "import os, site, sys; print(os.path.join(site.USER_BASE, "
            "'Scripts' if sys.platform == 'win32' else 'bin'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path or None


def verify_installation(
    *,
    run: RunFn | None = None,
) -> InstallVerifyResult:
    """Run ``cyclopsctl --help`` verification."""
    runner = run or subprocess.run

    orch = runner(
        ["cyclopsctl", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    cyclopsctl_ok = orch.returncode == 0
    cyclopsctl_error = (orch.stderr or orch.stdout).strip()

    return InstallVerifyResult(
        cyclopsctl_ok=cyclopsctl_ok,
        cyclopsctl_error=cyclopsctl_error,
    )


def run_install(
    *,
    source: str = "pypi",
    git_url: str | None = None,
    local_path: Path | None = None,
    skip_verify: bool = False,
    run: RunFn | None = None,
    path_entries: Sequence[str] | None = None,
    is_windows: bool | None = None,
) -> InstallResult:
    """Execute the install pipeline (intended for tests with injected runners)."""
    runner = run or subprocess.run
    if is_windows is None:
        is_windows = sys.platform == "win32"
    if path_entries is None:
        path_entries = os.environ.get("PATH", "").split(os.pathsep)

    messages: list[str] = []

    version_error = validate_python_version(sys.version_info)
    if version_error:
        return InstallResult(exit_code=1, messages=(version_error,))

    messages.append(f"Installing {PACKAGE_NAME} from {source}...")
    pip_argv = build_pip_install_argv(
        source=source,
        git_url=git_url,
        local_path=local_path,
    )
    pip_result = runner(pip_argv, capture_output=True, text=True, check=False)
    if pip_result.returncode != 0:
        detail = (pip_result.stderr or pip_result.stdout).strip()
        return InstallResult(
            exit_code=pip_result.returncode,
            messages=tuple(messages + [f"pip install failed: {detail}"]),
        )
    messages.append("cyclopsctl package installed.")

    verify: InstallVerifyResult | None = None
    if not skip_verify:
        verify = verify_installation(run=runner)
        if verify.cyclopsctl_ok:
            messages.append("Verified: cyclopsctl --help")
        else:
            messages.append("Warning: cyclopsctl command not found after install.")

        missing = missing_commands(["cyclopsctl"], path_entries, is_windows=is_windows)
        if missing:
            remediation = format_path_remediation(
                missing,
                python_scripts_dir=discover_python_scripts_dir(run=runner),
                is_windows=is_windows,
            )
            if remediation:
                messages.append(remediation)

    exit_code = 0
    if verify is not None and not verify.cyclopsctl_ok:
        exit_code = 2

    return InstallResult(exit_code=exit_code, messages=tuple(messages), verify=verify)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cyclopsctl.installer",
        description="Install cyclopsctl globally.",
    )
    parser.add_argument(
        "--source",
        choices=["pypi", "git", "local"],
        default="pypi",
        help="Install source (default: pypi)",
    )
    parser.add_argument(
        "--git-url",
        default=DEFAULT_GIT_URL,
        help="Git URL when --source git",
    )
    parser.add_argument(
        "--local-path",
        type=Path,
        default=None,
        help="Local project path when --source local",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip install; only verify commands and print PATH remediation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry for install.ps1 / install.sh delegation."""
    parser = _build_cli_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verify_only:
        verify = verify_installation()
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        missing = missing_commands(["cyclopsctl"], path_entries)
        lines = [
            "cyclopsctl --help: OK"
            if verify.cyclopsctl_ok
            else "cyclopsctl --help: FAILED",
        ]
        for line in lines:
            print(line)
        if missing:
            print(
                format_path_remediation(
                    missing,
                    python_scripts_dir=discover_python_scripts_dir(),
                )
            )
            return 2
        return 0

    result = run_install(
        source=args.source,
        git_url=args.git_url,
        local_path=args.local_path,
    )
    for message in result.messages:
        print(message)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
