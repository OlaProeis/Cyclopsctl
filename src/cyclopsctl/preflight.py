"""Preflight validation before cyclopsctl cycles (task 12)."""

from __future__ import annotations

import os
from collections.abc import Mapping


class PreflightError(RuntimeError):
    """Required environment or tooling is missing before run start."""


def check_cursor_api_key(*, env: Mapping[str, str] | None = None) -> str:
    """Require a non-empty ``CURSOR_API_KEY`` in the environment."""
    source = env if env is not None else os.environ
    key = source.get("CURSOR_API_KEY", "").strip()
    if not key:
        raise PreflightError(
            "CURSOR_API_KEY is not set. Export it in the environment before running "
            "cyclopsctl."
        )
    return key


def run_preflight(
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Validate runtime prerequisites; return the resolved API key."""
    return check_cursor_api_key(env=env)
