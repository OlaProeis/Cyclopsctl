"""Graceful Ctrl+C handling for cyclopsctl runs (task 7)."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cyclopsctl.session import CycleSession

INTERRUPT_EXIT_CODE = 130


class RunInterruptedError(RuntimeError):
    """Raised when the operator interrupts an in-progress cyclopsctl run."""

    def __init__(
        self,
        message: str = "Cyclopsctl run interrupted",
        *,
        cycle_number: int = 0,
        phase: str = "idle",
        agent_id: str | None = None,
        run_id: str | None = None,
        partial_result: object | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = INTERRUPT_EXIT_CODE
        self.cycle_number = cycle_number
        self.phase = phase
        self.agent_id = agent_id
        self.run_id = run_id
        self.partial_result = partial_result


@dataclass
class RunInterruptController:
    """
    Cooperative shutdown requested by SIGINT.

    The signal handler only sets ``stop_requested`` and runs registered cleanup
    callbacks (for example closing the active session or stopping Rich Live).
    The main loop checks ``stop_requested`` at safe points and raises
    ``RunInterruptedError`` after cleanup.
    """

    _stop_requested: threading.Event = field(default_factory=threading.Event)
    _callbacks: list[Callable[[], None]] = field(default_factory=list)
    _previous_handler: Any = field(default=None, init=False, repr=False)
    _active_session: CycleSession | None = field(default=None, init=False, repr=False)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        self._stop_requested.set()
        if self._active_session is not None:
            try:
                self._active_session.close()
            except Exception:
                pass
        for callback in self._callbacks:
            try:
                callback()
            except Exception:
                pass

    def set_active_session(self, session: CycleSession | None) -> None:
        self._active_session = session

    def add_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def register(self) -> None:
        """Install the SIGINT handler for the current thread."""
        self._previous_handler = signal.signal(signal.SIGINT, self._on_sigint)

    def restore(self) -> None:
        """Restore the previous SIGINT handler."""
        if self._previous_handler is not None:
            signal.signal(signal.SIGINT, self._previous_handler)
            self._previous_handler = None

    def _on_sigint(self, signum: int, frame: object | None) -> None:
        self.request_stop()

    def raise_if_requested(
        self,
        *,
        cycle_number: int,
        phase: str,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if not self.stop_requested:
            return
        raise RunInterruptedError(
            cycle_number=cycle_number,
            phase=phase,
            agent_id=agent_id,
            run_id=run_id,
        )


__all__ = [
    "INTERRUPT_EXIT_CODE",
    "RunInterruptController",
    "RunInterruptedError",
]
