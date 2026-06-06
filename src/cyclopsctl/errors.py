"""Known cyclopsctl errors and PRD exit-code mapping (task 12)."""

from __future__ import annotations

from cyclopsctl.bootstrap import BootstrapError
from cyclopsctl.config import ConfigError
from cyclopsctl.preflight import PreflightError
from cyclopsctl.prompt import PromptError
from cyclopsctl.interrupt import INTERRUPT_EXIT_CODE, RunInterruptedError
from cyclopsctl.runner import AgentRunError, RUN_FAILURE_EXIT_CODE, STARTUP_EXIT_CODE
from cyclopsctl.sdk_bridge import SdkBridgeError
from cyclopsctl.session import SessionError
from cyclopsctl.task_selection import TaskSelectionError
from cyclopsctl.tasks.cli import TasksCliError
from cyclopsctl.alignment import HandoverAlignmentError
from cyclopsctl.verify import HandoverVerificationError, ImplementationHandoverViolationError

GENERAL_EXIT_CODE = RUN_FAILURE_EXIT_CODE

KNOWN_RUN_ERRORS = (
    AgentRunError,
    BootstrapError,
    ConfigError,
    HandoverAlignmentError,
    HandoverVerificationError,
    ImplementationHandoverViolationError,
    PreflightError,
    PromptError,
    RunInterruptedError,
    SdkBridgeError,
    SessionError,
    TaskSelectionError,
    TasksCliError,
)

__all__ = [
    "GENERAL_EXIT_CODE",
    "INTERRUPT_EXIT_CODE",
    "KNOWN_RUN_ERRORS",
    "STARTUP_EXIT_CODE",
    "exit_code_for",
]


def exit_code_for(exc: BaseException) -> int:
    """Map a known cyclopsctl failure to a process exit code."""
    if isinstance(exc, AgentRunError):
        return exc.exit_code
    if isinstance(exc, RunInterruptedError):
        return exc.exit_code
    return GENERAL_EXIT_CODE
