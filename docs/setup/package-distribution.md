# Package distribution and global install

Install cyclopsctl globally via **`pip install`** from git (no clone required) or from a local checkout. There is no PyPI publish workflow — git is the distribution channel.

## Version reporting

- `cyclopsctl --version` prints the installed distribution version via `importlib.metadata` (`src/cyclopsctl/version.py`).
- `cyclopsctl.version.get_package_version()` falls back to `0.1.2` when the package is not installed (editable dev without metadata).

## Shell installers

| Script | Platform | Purpose |
|--------|----------|---------|
| `install.ps1` | Windows (primary) | Validate Python 3.10+, `pip install`, verify + PATH remediation |
| `install.sh` | macOS/Linux | Same flow with `--source`, `--git-url`, `--local-path` flags |

Both scripts:

1. Require Python 3.10+.
2. Install from **git** (default) or **local** path via `pip`.
3. Delegate verification to `python -m cyclopsctl.installer --verify-only`.

### Examples

```powershell
.\install.ps1
.\install.ps1 -Source local
.\install.ps1 -Source git -GitUrl "git+https://github.com/OlaProeis/Cyclopsctl.git@v0.1.2"
```

```bash
./install.sh
./install.sh --source local
```

## Install support module

`src/cyclopsctl/installer.py` holds testable install logic:

- Python version validation
- PATH discovery (`command_on_path`, `missing_commands`)
- PATH remediation messages (`format_path_remediation`)
- `pip` argv builders
- Post-install verification (`cyclopsctl --help`, `cyclopsctl --version`)
- `run_install()` pipeline for mocked tests
- `--verify-only` CLI mode used by shell scripts after `pip install`

## Packaging metadata

`pyproject.toml` includes:

- SPDX license: **MIT**
- Hatchling wheel build with bundled `cyclopsctl/templates`
- Console script: `cyclopsctl = cyclopsctl.cli:main`

Build a wheel locally:

```bash
python -m pip wheel . -w dist/ --no-deps
```

Wheel `METADATA` must include `Version` and `License` (or `License-Expression`).

## Troubleshooting installs (Windows)

### `WinError 32` — `cyclopsctl.exe` in use during `pip install`

Pip cannot replace the console script while another process holds `cyclopsctl.exe` open (an active terminal session, IDE task, or a running `cursor-sdk-bridge` child).

1. Close terminals where `cyclopsctl` is running or was last invoked.
2. Stop any process still using the script:

```powershell
Get-Process cyclopsctl -ErrorAction SilentlyContinue | Stop-Process -Force
```

If the lock persists, find the holder by path (run PowerShell **as Administrator** if `OpenFiles` is disabled):

```powershell
openfiles /query /fo table | findstr /i cyclopsctl
# then: Stop-Process -Id <PID> -Force
```

3. Retry install from the dev repo:

```powershell
pip install -e G:\DEV\CursorOrchestrator
```

**While developing:** `python -m cyclopsctl …` uses the editable source without rewriting `Scripts\cyclopsctl.exe` — useful when pip upgrade is blocked.

## Tests

- `tests/test_installer.py` — mocked subprocess/PATH remediation paths
- `tests/test_distribution.py` — `--version` output and wheel metadata
