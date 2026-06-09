"""Windows-safe Cursor SDK bridge bootstrap.

``cursor-sdk`` auto-launches a local bridge on first use. On Windows the
discovery reader in ``cursor_sdk._bridge`` fails when polling pipe stderr
via ``selectors`` (``WinError 10038``). When ``CURSOR_SDK_BRIDGE_URL`` is
unset on Windows, start the bridge ourselves with a blocking ``readline``
discovery path and point the SDK at it via env vars.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BRIDGE_URL_ENV = "CURSOR_SDK_BRIDGE_URL"
_BRIDGE_TOKEN_ENV = "CURSOR_SDK_BRIDGE_TOKEN"
_BRIDGE_AUTH_TOKEN_ENV = "CURSOR_SDK_BRIDGE_AUTH_TOKEN"
_DEFAULT_DISCOVERY_TIMEOUT = 30.0

InstallClientFn = Callable[..., Any]


class SdkBridgeError(RuntimeError):
    """Failed to start or connect to the local ``cursor-sdk-bridge`` process."""


@dataclass
class ManagedBridge:
    """A bridge subprocess started by the cyclopsctl (Windows bootstrap)."""

    process: subprocess.Popen[str]
    url: str
    auth_token: str
    client: Any = None

    def close(self) -> None:
        """Shut down the bridge and clear SDK env overrides."""
        try:
            from cursor_sdk._connect import post_bridge_shutdown

            post_bridge_shutdown(self.url, self.auth_token, grace_seconds=0)
        except Exception:
            logger.debug("Graceful bridge shutdown failed", exc_info=True)
        _terminate_process(self.process)
        os.environ.pop(_BRIDGE_URL_ENV, None)
        os.environ.pop(_BRIDGE_TOKEN_ENV, None)
        os.environ.pop(_BRIDGE_AUTH_TOKEN_ENV, None)
        try:
            from cursor_sdk._client import close_default_client

            close_default_client()
        except Exception:
            logger.debug("Failed to reset cursor-sdk default client", exc_info=True)


def install_env_fallback_default_client(*, url: str, auth_token: str) -> Any:
    """Install an SDK default client that allows ``CURSOR_API_KEY`` fallback.

    The cursor-sdk treats a bridge supplied through env vars as
    *caller-supplied* and constructs its default client with
    ``allow_api_key_env_fallback=False``. That makes run-scoped RPCs
    (``WaitLiveRun`` / ``ObserveRun`` / ``CancelRun``) — which never carry an
    explicit ``apiKey`` — fail with ``missing_api_key``.

    We only launch the bridge ourselves to work around a Windows bridge
    discovery bug, so we re-create the same trust model the SDK uses for a
    bridge it launched itself: env-var fallback enabled. The client is
    installed as the SDK default so every entry point (``Agent.create`` and the
    run handle it returns) uses it.
    """
    from cursor_sdk import Client
    from cursor_sdk import _client as sdk_client

    client = Client(
        base_url=url,
        auth_token=auth_token,
        allow_api_key_env_fallback=True,
    )
    with sdk_client._DEFAULT_CLIENT_LOCK:
        sdk_client._DEFAULT_CLIENT = client
    logger.info("Installed env-fallback cursor-sdk default client for %s", url)
    return client


def bridge_env_configured() -> bool:
    """Return True when the SDK can attach to an existing bridge endpoint."""
    url = os.environ.get(_BRIDGE_URL_ENV, "").strip()
    token = (
        os.environ.get(_BRIDGE_TOKEN_ENV, "").strip()
        or os.environ.get(_BRIDGE_AUTH_TOKEN_ENV, "").strip()
    )
    return bool(url and token)


def needs_windows_bridge_bootstrap() -> bool:
    """True when cyclopsctl should launch the bridge instead of cursor-sdk."""
    return sys.platform == "win32" and not bridge_env_configured()


def _configure_bridge_env(*, url: str, auth_token: str) -> None:
    os.environ[_BRIDGE_URL_ENV] = url
    os.environ[_BRIDGE_TOKEN_ENV] = auth_token


def launch_bridge_for_windows(
    workspace: Path,
    *,
    timeout: float = _DEFAULT_DISCOVERY_TIMEOUT,
    resolve_bridge_path: Callable[[], str] | None = None,
    popen: type[subprocess.Popen[str]] | None = None,
    install_client: InstallClientFn | None = None,
) -> ManagedBridge:
    """
    Start ``cursor-sdk-bridge`` and configure env vars for the Python SDK.

    Uses blocking ``readline`` on stderr, which works with Windows pipes.
    """
    install = install_client or install_env_fallback_default_client
    from cursor_sdk._bridge import BridgeEndpoint, parse_discovery_line
    from cursor_sdk._vendor import resolve_bridge_path as default_resolve

    resolve = resolve_bridge_path or default_resolve
    popen_cls = popen or subprocess.Popen

    argv = [resolve(), "--workspace", str(workspace.resolve())]
    process = popen_cls(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if process.stderr is None:
        _terminate_process(process)
        raise SdkBridgeError("Bridge process stderr is unavailable")

    deadline = time.monotonic() + timeout
    stderr_lines: list[str] = []

    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = process.stderr.read()
                if tail:
                    stderr_lines.append(tail)
                detail = "".join(stderr_lines).strip()
                raise SdkBridgeError(
                    f"Bridge exited before discovery (code {process.returncode})"
                    + (f": {detail}" if detail else "")
                )

            line = process.stderr.readline()
            if not line:
                time.sleep(0.05)
                continue

            stderr_lines.append(line)
            discovery = parse_discovery_line(line)
            if discovery is None:
                continue

            endpoint = BridgeEndpoint.from_discovery(discovery)
            _configure_bridge_env(url=endpoint.url, auth_token=endpoint.auth_token)
            logger.info("Started cursor-sdk-bridge at %s for %s", endpoint.url, workspace)
            client = install(url=endpoint.url, auth_token=endpoint.auth_token)
            return ManagedBridge(
                process=process,
                url=endpoint.url,
                auth_token=endpoint.auth_token,
                client=client,
            )

        raise SdkBridgeError("Timed out waiting for cursor-sdk-bridge discovery")
    except Exception:
        _terminate_process(process)
        raise


def ensure_sdk_bridge(
    workspace: Path,
    *,
    timeout: float = _DEFAULT_DISCOVERY_TIMEOUT,
    resolve_bridge_path: Callable[[], str] | None = None,
    popen: type[subprocess.Popen[str]] | None = None,
    install_client: InstallClientFn | None = None,
) -> ManagedBridge | None:
    """Launch a managed bridge on Windows when env vars are not already set."""
    if not needs_windows_bridge_bootstrap():
        return None
    return launch_bridge_for_windows(
        workspace,
        timeout=timeout,
        resolve_bridge_path=resolve_bridge_path,
        popen=popen,
        install_client=install_client,
    )


@contextmanager
def managed_sdk_bridge(
    workspace: Path,
    *,
    timeout: float = _DEFAULT_DISCOVERY_TIMEOUT,
    resolve_bridge_path: Callable[[], str] | None = None,
    popen: type[subprocess.Popen[str]] | None = None,
    install_client: InstallClientFn | None = None,
) -> Iterator[ManagedBridge | None]:
    """Context manager that starts and stops a Windows bridge when needed."""
    bridge = ensure_sdk_bridge(
        workspace,
        timeout=timeout,
        resolve_bridge_path=resolve_bridge_path,
        popen=popen,
        install_client=install_client,
    )
    try:
        yield bridge
    finally:
        if bridge is not None:
            bridge.close()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
