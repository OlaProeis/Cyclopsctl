"""Windows-safe Cursor SDK bridge bootstrap.

``cursor-sdk`` auto-launches a local bridge on first use. On Windows the
discovery reader in ``cursor_sdk._bridge`` fails when polling pipe stderr
via ``selectors`` (``WinError 10038``). When ``CURSOR_SDK_BRIDGE_URL`` is
unset on Windows, start the bridge ourselves with a blocking ``readline``
discovery path and point the SDK at it via env vars.
"""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Managed bridges that have not yet been closed. Used by the atexit safety net
# so the bridge subprocess is terminated even if a code path exits without
# unwinding the ``managed_sdk_bridge`` context manager's ``finally``.
_ACTIVE_BRIDGES: list[ManagedBridge] = []

_BRIDGE_URL_ENV = "CURSOR_SDK_BRIDGE_URL"
_BRIDGE_TOKEN_ENV = "CURSOR_SDK_BRIDGE_TOKEN"
_BRIDGE_AUTH_TOKEN_ENV = "CURSOR_SDK_BRIDGE_AUTH_TOKEN"
_DEFAULT_DISCOVERY_TIMEOUT = 30.0

# Read timeout for bridge RPCs (`WaitLiveRun`, activity stream). The cursor-sdk
# defaults (60s unary / 600s stream) are far too short for long agent runs such
# as soak tests or full acceptance suites, which makes `run.wait()` fail with
# `Bridge request timed out: ReadTimeout`. Default to one hour; override with
# `CYCLOPSCTL_BRIDGE_TIMEOUT_SECONDS` (set `0` / `none` to disable entirely).
_BRIDGE_TIMEOUT_ENV = "CYCLOPSCTL_BRIDGE_TIMEOUT_SECONDS"
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 3600.0

InstallClientFn = Callable[..., Any]


def resolve_bridge_client_timeout(
    *, env: dict[str, str] | None = None
) -> float | None:
    """Resolve the bridge RPC timeout from env; ``None`` disables timeouts."""
    source = env if env is not None else os.environ
    raw = source.get(_BRIDGE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_BRIDGE_TIMEOUT_SECONDS
    if raw.lower() in {"0", "none", "off", "disabled", "disable"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %.0fs",
            _BRIDGE_TIMEOUT_ENV,
            raw,
            DEFAULT_BRIDGE_TIMEOUT_SECONDS,
        )
        return DEFAULT_BRIDGE_TIMEOUT_SECONDS
    return value if value > 0 else None


class SdkBridgeError(RuntimeError):
    """Failed to start or connect to the local ``cursor-sdk-bridge`` process."""


@dataclass(eq=False)
class ManagedBridge:
    """A bridge subprocess started by the cyclopsctl (Windows bootstrap)."""

    process: subprocess.Popen[str]
    url: str
    auth_token: str
    client: Any = None
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        """Shut down the bridge and clear SDK env overrides (idempotent)."""
        if self._closed:
            return
        self._closed = True
        _unregister_active_bridge(self)
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


def _register_active_bridge(bridge: ManagedBridge) -> None:
    _ACTIVE_BRIDGES.append(bridge)


def _unregister_active_bridge(bridge: ManagedBridge) -> None:
    try:
        _ACTIVE_BRIDGES.remove(bridge)
    except ValueError:
        pass


@atexit.register
def _close_active_bridges_atexit() -> None:
    """Safety net: close any bridge still open at interpreter exit.

    Normal runs close the bridge through the ``managed_sdk_bridge`` context
    manager. This handler only matters when an exit path skips that ``finally``
    (e.g. an unexpected ``sys.exit`` during Rich teardown), so the bridge
    subprocess and its child node/npm/test processes do not orphan.
    """
    for bridge in list(_ACTIVE_BRIDGES):
        try:
            bridge.close()
        except Exception:
            logger.debug("atexit bridge cleanup failed", exc_info=True)


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

    timeout = resolve_bridge_client_timeout()
    client = Client(
        base_url=url,
        auth_token=auth_token,
        allow_api_key_env_fallback=True,
        timeout=timeout,
    )
    with sdk_client._DEFAULT_CLIENT_LOCK:
        sdk_client._DEFAULT_CLIENT = client
    logger.info(
        "Installed env-fallback cursor-sdk default client for %s (timeout=%s)",
        url,
        "disabled" if timeout is None else f"{timeout:.0f}s",
    )
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
            bridge = ManagedBridge(
                process=process,
                url=endpoint.url,
                auth_token=endpoint.auth_token,
                client=client,
            )
            _register_active_bridge(bridge)
            return bridge

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
    # The bridge spawns child node/npm/test processes; terminating only the
    # bridge PID orphans that tree. On Windows use ``taskkill /T`` to kill the
    # whole tree; elsewhere fall back to terminate/kill of the bridge process.
    if sys.platform == "win32" and _taskkill_tree(process.pid):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.debug("Bridge process still alive after taskkill tree", exc_info=True)
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _taskkill_tree(pid: int) -> bool:
    """Kill a Windows process tree via ``taskkill /F /T``. Returns success."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        logger.debug("taskkill tree termination failed for pid %s", pid, exc_info=True)
        return False
