# Package distribution and global install

The cyclopsctl ships as **`cyclopsctl`** on PyPI (or from git/local via `pip`). Users can install globally without cloning the dev repository.

## Version reporting

- `cyclopsctl --version` prints the installed distribution version via `importlib.metadata` (`src/cyclopsctl/version.py`).
- `cyclopsctl.version.get_package_version()` falls back to `0.1.0` when the package is not installed (editable dev without metadata).

## Shell installers

| Script | Platform | Purpose |
|--------|----------|---------|
| `install.ps1` | Windows (primary) | Validate Python 3.10+, `pip install`, verify + PATH remediation |
| `install.sh` | macOS/Linux | Same flow with `--source`, `--git-url`, `--local-path` flags |

Both scripts:

1. Require Python 3.10+.
2. Install from **pypi** (default), **git**, or **local** path.
3. Delegate verification to `python -m cyclopsctl.installer --verify-only`.

### Examples

```powershell
.\install.ps1
.\install.ps1 -Source local
.\install.ps1 -Source git -GitUrl "git+https://github.com/OlaProeis/Cyclopsctl.git@v0.1.0"
```

```bash
./install.sh --source pypi
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

## PyPI publish workflow

`.github/workflows/publish.yml` runs on `v*` tags:

1. Build wheel and sdist with `python -m build`
2. Verify wheel metadata
3. Upload to PyPI via `PYPI_API_TOKEN` secret

Manual publish (without CI): build with `python -m build`, then `twine upload dist/*`.

## Tests

- `tests/test_installer.py` — mocked subprocess/PATH remediation paths
- `tests/test_distribution.py` — `--version` output and wheel metadata
