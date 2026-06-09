"""Package version resolved from installed distribution metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "cyclopsctl"
_FALLBACK_VERSION = "0.1.1"


def get_package_version() -> str:
    """Return the installed package version from metadata, with a dev fallback."""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return _FALLBACK_VERSION
