# Python package scaffold

The cyclopsctl ships as installable package `cyclopsctl` under `src/cyclopsctl/`.

## Layout

- `pyproject.toml` — Hatchling build, `cursor-sdk` dependency, console script `cyclopsctl`
- `src/cyclopsctl/` — modules per PRD (`cli`, `config`, `tasks/`, `routing`, `prompt`, `verify`, `runner`, `session`, `logging`)
- `tests/` — pytest smoke and config tests

## Install and smoke check

**Global install (no clone):** see `package-distribution.md` — run `install.ps1` (Windows) or `install.sh` (macOS/Linux), or `pip install "cyclopsctl @ git+https://github.com/OlaProeis/Cyclopsctl.git"` (git is the distribution channel; not on PyPI).

**Development (this repo):**

```bash
pip install -e ".[dev]"
cyclopsctl --help
cyclopsctl --version
python -m pytest
```

Stub modules exist for features not yet implemented; `cli` wires subcommands `run` and `models`.
