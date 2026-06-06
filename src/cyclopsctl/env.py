"""Project-root ``.env`` loading before preflight (task 3)."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class EnvLoadError(ValueError):
    """Failed to read or parse the project ``.env`` file."""


def project_env_path(project_root: Path) -> Path:
    """Resolved path to ``<project-root>/.env``."""
    return (project_root / ".env").resolve()


def load_project_env(project_root: Path) -> Path | None:
    """Load ``<project-root>/.env`` when present without overriding existing env vars."""
    env_file = project_env_path(project_root)
    if not env_file.is_file():
        return None

    try:
        env_file.read_bytes()
    except OSError as exc:
        raise EnvLoadError(f"Unable to read .env file: {env_file}") from exc

    load_dotenv(env_file, override=False)
    logger.info("Loaded environment from %s", env_file)
    return env_file
