"""Cursor SDK ``Agent.create`` / ``send`` / ``wait`` wrapper (task 7)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions, ModelSelection, RunResult

logger = logging.getLogger(__name__)

DEFAULT_ACTIVITY_FILE_MAX_LEN = 48
DEFAULT_ACTIVITY_SUMMARY_MAX_LEN = 60
DEFAULT_ACTIVITY_PROSE_DISPLAY_MAX_LEN = 240
ACTIVITY_TOOL_SEPARATOR = " · "

ActivityCallback = Callable[[str], None]
PlanUpdateCallback = Callable[[Sequence[Mapping[str, Any]], bool], None]

_TODO_WRITE_TOOL_NAMES = frozenset({"TodoWrite", "todo_write"})

_FILE_PATH_KEYS = (
    "path",
    "file",
    "file_path",
    "filepath",
    "target_file",
    "relative_path",
)
_SUMMARY_KEYS = (
    "summary",
    "description",
    "command",
    "query",
    "pattern",
    "text",
    "content",
    "message",
)

DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 15)

_TRANSIENT_MESSAGE_HINTS: frozenset[str] = frozenset(
    {
        "network",
        "timeout",
        "timed out",
        "connection",
        "connectivity",
        "unavailable",
        "temporarily",
        "reset",
        "socket",
        "rpc",
        "econnreset",
        "econnrefused",
        "dns",
        "502",
        "503",
        "504",
        "startup",
    }
)

_NON_TRANSIENT_MESSAGE_HINTS: frozenset[str] = frozenset(
    {
        "auth",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "api key",
        "permission",
    }
)

_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

STARTUP_EXIT_CODE = 1
RUN_FAILURE_EXIT_CODE = 2


class RunFailureKind(str, Enum):
    """Distinguish SDK startup failures from completed runs that errored."""

    STARTUP = "startup"
    RUN = "run"


_OPUS_BILLING_FAILURE_HINTS: frozenset[str] = frozenset(
    {
        "credit balance is too low",
        "insufficient credits",
        "out of credits",
        "no credits",
        "billing",
        "payment required",
        "quota exceeded",
        "usage limit",
        "limit exceeded",
        "spend limit",
    }
)


class AgentRunError(RuntimeError):
    """Raised when agent creation, send, or wait fails per PRD stop rules."""

    def __init__(
        self,
        message: str,
        *,
        kind: RunFailureKind,
        exit_code: int,
        agent_id: str | None = None,
        run_id: str | None = None,
        result_detail: str | None = None,
        diagnostic_detail: str | None = None,
        phase: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code
        self.agent_id = agent_id
        self.run_id = run_id
        self.result_detail = result_detail
        # Best-effort context recovered from the run conversation when the SDK
        # returned no ``result`` detail. Display-only: never used for retry or
        # billing-failure classification.
        self.diagnostic_detail = diagnostic_detail
        self.phase = phase
        self.cause = cause


@dataclass(frozen=True)
class SendRunResult:
    """Metadata captured after a synchronous ``send`` + ``wait`` cycle."""

    agent_id: str
    run_id: str
    status: str
    result: str = ""


CreateAgentFn = Callable[..., Agent]
SendFn = Callable[[Agent, str], Any]
WaitFn = Callable[[Any], RunResult]


def truncate_activity_text(
    value: str,
    *,
    max_len: int,
) -> str:
    """Truncate long activity fragments with an ellipsis."""
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"
    return text[: max_len - 1] + "…"


def is_prose_activity_line(line: str) -> bool:
    """Return whether ``line`` is summary-only prose rather than a tool row."""
    return ACTIVITY_TOOL_SEPARATOR not in line


_BOOTSTRAP_STATUS_LINES = frozenset(
    {"RUNNING", "FINISHED", "ERROR", "COMPLETED", "CANCELLED", "FAILED"}
)
_DEFAULT_BOOTSTRAP_WRAP_WIDTH = 96
_DEFAULT_BOOTSTRAP_EMIT_THRESHOLD = 120


def _wrap_bootstrap_text(text: str, *, width: int) -> list[str]:
    """Wrap coalesced agent prose for readable bootstrap stderr output."""
    normalized = " ".join(text.split())
    if not normalized:
        return []

    lines: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= width:
            lines.append(remaining)
            break
        chunk = remaining[:width]
        break_at = chunk.rfind(" ")
        if break_at <= width // 3:
            break_at = width
        else:
            chunk = remaining[:break_at]
        lines.append(chunk.rstrip())
        remaining = remaining[break_at:].lstrip()
    return lines


class BootstrapProgressWriter:
    """
    Coalesce streaming assistant fragments into readable wrapped lines.

    The Cursor SDK often emits one token (or a few characters) per activity
    event during JSON generation. This writer merges them with
    ``merge_prose_activity`` and prints wrapped chunks instead of one fragment
    per line.
    """

    def __init__(
        self,
        log_fn: ActivityCallback,
        *,
        wrap_width: int = _DEFAULT_BOOTSTRAP_WRAP_WIDTH,
        emit_threshold: int = _DEFAULT_BOOTSTRAP_EMIT_THRESHOLD,
    ) -> None:
        self._log_fn = log_fn
        self._wrap_width = max(40, wrap_width)
        self._emit_threshold = max(40, emit_threshold)
        self._buffer = ""
        self._emitted_len = 0

    def __call__(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped in _BOOTSTRAP_STATUS_LINES:
            if stripped != "RUNNING":
                self.flush()
            self._log_fn(stripped)
            return
        if ACTIVITY_TOOL_SEPARATOR in line:
            self.flush()
            self._log_fn(stripped)
            return
        if not is_prose_activity_line(line):
            self.flush()
            self._log_fn(stripped)
            return

        self._buffer = merge_prose_activity(self._buffer, stripped)
        if len(self._buffer) - self._emitted_len >= self._emit_threshold:
            self._emit_pending()

    def flush(self) -> None:
        """Emit any coalesced prose not yet written."""
        self._emit_pending(final=True)

    def _emit_pending(self, *, final: bool = False) -> None:
        pending = self._buffer[self._emitted_len :].strip()
        if not pending:
            return
        if not final and len(pending) < self._emit_threshold:
            return
        for chunk in _wrap_bootstrap_text(pending, width=self._wrap_width):
            self._log_fn(chunk)
        self._emitted_len = len(self._buffer)


def bootstrap_progress_writer(
    log_fn: ActivityCallback,
    *,
    wrap_width: int = _DEFAULT_BOOTSTRAP_WRAP_WIDTH,
    emit_threshold: int = _DEFAULT_BOOTSTRAP_EMIT_THRESHOLD,
) -> BootstrapProgressWriter:
    """Create a bootstrap progress writer; call ``flush()`` after the agent run."""
    return BootstrapProgressWriter(
        log_fn,
        wrap_width=wrap_width,
        emit_threshold=emit_threshold,
    )


def bootstrap_progress_callback(
    log_fn: ActivityCallback,
) -> ActivityCallback:
    """Backward-compatible callback without automatic flush (prefer the writer)."""
    return bootstrap_progress_writer(log_fn)


def merge_prose_activity(existing: str, delta: str) -> str:
    """Merge consecutive streaming prose fragments into one logical line."""
    if not existing:
        return delta.strip()
    if not delta:
        return existing

    if delta.startswith(existing):
        return delta
    if existing.startswith(delta):
        return existing
    if existing.endswith(delta):
        return existing

    if delta.startswith((" ", "\n", "\t")):
        return existing + delta
    if existing[-1].isspace():
        return existing + delta.lstrip()
    if delta[0] in ",.;:!?)]}":
        return existing + delta
    if existing[-1] in "({[":
        return existing + delta
    return existing + " " + delta


def format_activity_line(
    *,
    tool_name: str = "",
    file_path: str = "",
    summary: str = "",
    file_max_len: int = DEFAULT_ACTIVITY_FILE_MAX_LEN,
    summary_max_len: int | None = DEFAULT_ACTIVITY_SUMMARY_MAX_LEN,
) -> str:
    """Build a single dashboard activity line."""
    parts: list[str] = []
    if tool_name:
        parts.append(tool_name.strip())
    if file_path:
        parts.append(
            truncate_activity_text(file_path, max_len=file_max_len),
        )
    if summary:
        if summary_max_len is None:
            parts.append(" ".join(summary.split()))
        else:
            parts.append(
                truncate_activity_text(summary, max_len=summary_max_len),
            )
    return ACTIVITY_TOOL_SEPARATOR.join(parts)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _string_field(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        raw = mapping.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _extract_file_path(payload: Mapping[str, Any]) -> str:
    for key in _FILE_PATH_KEYS:
        value = _string_field(payload, key)
        if value:
            return value
    for nested_key in ("args", "arguments", "input", "params", "parameters"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            value = _extract_file_path(nested)
            if value:
                return value
    return ""


def _extract_summary(payload: Mapping[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        value = _string_field(payload, key)
        if value:
            return value
    for nested_key in ("args", "arguments", "input", "result", "output"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            value = _extract_summary(nested)
            if value:
                return value
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _format_activity_from_mapping(payload: Mapping[str, Any]) -> str | None:
    tool_name = _string_field(
        payload,
        "name",
        "tool",
        "tool_name",
        "toolName",
        "type",
    )
    file_path = _extract_file_path(payload)
    summary = _extract_summary(payload)
    line = format_activity_line(
        tool_name=tool_name,
        file_path=file_path,
        summary=summary,
    )
    return line or None


def _is_todo_write_tool_name(name: str) -> bool:
    return name in _TODO_WRITE_TOOL_NAMES


def _extract_todo_write_args(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    for nested_key in ("args", "arguments", "input", "params", "parameters"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            return dict(nested)
    if "todos" in payload:
        return dict(payload)
    return None


def _tool_call_mapping_from_event(event: Any) -> dict[str, Any] | None:
    """Return a normalized tool-call mapping when ``event`` is a tool invocation."""
    sdk_message = getattr(event, "sdk_message", None)
    if sdk_message is not None:
        return _tool_call_mapping_from_event(sdk_message)

    interaction_update = getattr(event, "interaction_update", None)
    if interaction_update is not None:
        update_type = str(getattr(interaction_update, "type", ""))
        if "tool-call" in update_type or update_type == "partial-tool-call":
            tool_call = _coerce_mapping(getattr(interaction_update, "tool_call", {}))
            if tool_call:
                return tool_call

    if isinstance(event, Mapping):
        message_type = _string_field(event, "type")
        if message_type == "tool_call":
            return _coerce_mapping(event)
        return None

    message_type = str(getattr(event, "type", ""))
    if message_type == "tool_call":
        return {
            "name": getattr(event, "name", ""),
            "args": getattr(event, "args", None),
        }
    return None


def parse_todo_write_from_event(event: Any) -> tuple[list[dict[str, Any]], bool] | None:
    """Extract ``TodoWrite`` / ``todo_write`` todos and merge flag from one stream event."""
    tool_call = _tool_call_mapping_from_event(event)
    if not tool_call:
        return None

    tool_name = _string_field(
        tool_call,
        "name",
        "tool",
        "tool_name",
        "toolName",
    )
    if not _is_todo_write_tool_name(tool_name):
        return None

    args = _extract_todo_write_args(tool_call)
    if args is None:
        return None

    raw_todos = args.get("todos")
    if not isinstance(raw_todos, Sequence) or isinstance(raw_todos, (str, bytes)):
        return None

    todos: list[dict[str, Any]] = []
    for entry in raw_todos:
        if isinstance(entry, Mapping):
            todos.append(dict(entry))

    merge = bool(args.get("merge", False))
    return todos, merge


def format_activity_from_event(event: Any) -> str | None:
    """Defensively map one SDK stream event to a dashboard activity line."""
    sdk_message = getattr(event, "sdk_message", None)
    if sdk_message is not None:
        return format_activity_from_event(sdk_message)

    interaction_update = getattr(event, "interaction_update", None)
    if interaction_update is not None:
        update_type = str(getattr(interaction_update, "type", ""))
        if "tool-call" in update_type or update_type == "partial-tool-call":
            tool_call = _coerce_mapping(getattr(interaction_update, "tool_call", {}))
            return _format_activity_from_mapping(tool_call)

    if isinstance(event, Mapping):
        message_type = _string_field(event, "type")
        if message_type == "tool_call":
            return _format_activity_from_mapping(event)
        if message_type == "assistant":
            text_blocks = event.get("message", {})
            if isinstance(text_blocks, Mapping):
                content = text_blocks.get("content", ())
                if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
                    for block in content:
                        if isinstance(block, Mapping):
                            text = _string_field(block, "text")
                            if text:
                                return format_activity_line(
                                    summary=text,
                                    summary_max_len=None,
                                )
        if message_type in {"status", "task", "thinking"}:
            summary = _string_field(event, "message", "text", "status")
            if summary:
                return format_activity_line(
                    summary=summary,
                    summary_max_len=None,
                )
        return _format_activity_from_mapping(event)

    message_type = str(getattr(event, "type", ""))
    if message_type == "tool_call":
        return _format_activity_from_mapping(
            {
                "name": getattr(event, "name", ""),
                "args": getattr(event, "args", None),
                "result": getattr(event, "result", None),
            }
        )

    if message_type == "assistant":
        message = getattr(event, "message", None)
        if isinstance(message, Mapping):
            content = message.get("content", ())
        else:
            content = getattr(message, "content", ())
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            for block in content:
                if isinstance(block, Mapping):
                    text = _string_field(block, "text")
                else:
                    text = getattr(block, "text", "")
                if text:
                    return format_activity_line(
                        summary=str(text),
                        summary_max_len=None,
                    )

    if message_type in {"status", "task", "thinking"}:
        summary = _string_field(
            _coerce_mapping(getattr(event, "__dict__", {})),
            "message",
            "text",
            "status",
        )
        if summary:
            return format_activity_line(
                summary=summary,
                summary_max_len=None,
            )

    return None


def run_has_activity_stream(run: Any) -> bool:
    """Return whether ``run`` exposes a cursor-sdk streaming surface."""
    for attr in ("stream", "messages", "observe", "events"):
        if callable(getattr(run, attr, None)):
            return True
    return False


def iter_run_activity_events(run: Any) -> Iterator[Any]:
    """
    Yield activity-relevant events from the first supported stream API.

    Falls back through ``stream``, ``messages``, ``observe``, and ``events``.
    """
    for attr in ("stream", "messages", "observe", "events"):
        stream_fn = getattr(run, attr, None)
        if not callable(stream_fn):
            continue
        try:
            yield from stream_fn()
            return
        except Exception:
            logger.debug("run.%s() unavailable for activity stream", attr, exc_info=True)
    return
    yield  # pragma: no cover - makes this an Iterator for type checkers


def consume_run_activity(
    run: Any,
    on_activity: ActivityCallback,
    *,
    on_plan_update: PlanUpdateCallback | None = None,
) -> None:
    """Push formatted activity lines and optional plan updates from ``run``."""
    if not run_has_activity_stream(run):
        return
    try:
        for event in iter_run_activity_events(run):
            if on_plan_update is not None:
                plan_update = parse_todo_write_from_event(event)
                if plan_update is not None:
                    todos, merge = plan_update
                    on_plan_update(todos, merge)
            line = format_activity_from_event(event)
            if line:
                on_activity(line)
    except Exception:
        logger.debug("activity stream consumption failed", exc_info=True)


def create_local_agent(
    *,
    model: ModelSelection,
    project_root: Path,
    api_key: str | None = None,
    create_agent: CreateAgentFn | None = None,
) -> Agent:
    """Create a fresh local Cursor agent scoped to ``project_root``."""
    create_fn = create_agent or Agent.create
    kwargs: dict[str, Any] = {
        "model": model,
        "local": LocalAgentOptions(cwd=str(project_root)),
    }
    if api_key is not None:
        kwargs["api_key"] = api_key

    try:
        agent = create_fn(**kwargs)
    except CursorAgentError as exc:
        raise AgentRunError(
            f"Agent startup failed: {exc}",
            kind=RunFailureKind.STARTUP,
            exit_code=STARTUP_EXIT_CODE,
            phase="startup",
            cause=exc,
        ) from exc

    logger.info("Created agent %s for %s", agent.agent_id, project_root)
    return agent


def send_and_wait(
    agent: Agent,
    prompt: str,
    *,
    phase: str = "prompt",
    send_fn: SendFn | None = None,
    wait_fn: WaitFn | None = None,
    on_activity: ActivityCallback | None = None,
    on_plan_update: PlanUpdateCallback | None = None,
) -> SendRunResult:
    """
    Send ``prompt`` unchanged and wait synchronously for terminal status.

    Raises ``AgentRunError`` with ``RunFailureKind.STARTUP`` for
    ``CursorAgentError`` (auth/config/network) and ``RunFailureKind.RUN``
    when ``result.status == "error"``.
    """
    try:
        run = send_fn(agent, prompt) if send_fn is not None else agent.send(prompt)
    except CursorAgentError as exc:
        raise AgentRunError(
            f"{phase} send failed for agent {agent.agent_id}: {exc}",
            kind=RunFailureKind.STARTUP,
            exit_code=STARTUP_EXIT_CODE,
            agent_id=agent.agent_id,
            phase=phase,
            cause=exc,
        ) from exc

    run_id = getattr(run, "id", None) or getattr(run, "run_id", "unknown")
    if on_activity is not None or on_plan_update is not None:
        consume_run_activity(
            run,
            on_activity or (lambda _line: None),
            on_plan_update=on_plan_update,
        )
    try:
        result = wait_fn(run) if wait_fn is not None else run.wait()
    except CursorAgentError as exc:
        raise AgentRunError(
            f"{phase} wait failed for run {run_id}: {exc}",
            kind=RunFailureKind.STARTUP,
            exit_code=STARTUP_EXIT_CODE,
            agent_id=agent.agent_id,
            run_id=run_id,
            phase=phase,
            cause=exc,
        ) from exc

    status = str(result.status)
    logger.info(
        "Completed %s: agent=%s run=%s status=%s",
        phase,
        agent.agent_id,
        result.id,
        status,
    )

    if status == "error":
        detail = str(result.result).strip() if result.result else ""
        # The SDK frequently returns an empty result for errored runs; pull
        # best-effort context from the run conversation so the failure report
        # is actionable. Kept separate from ``result_detail`` because retry
        # classification keys off whether the SDK itself reported a reason.
        diagnostic = "" if detail else extract_run_error_detail(run)
        message = f"{phase} run failed: agent={agent.agent_id} run={result.id}"
        if detail:
            message = f"{message}: {detail}"
        elif diagnostic:
            message = f"{message} ({diagnostic})"
        raise AgentRunError(
            message,
            kind=RunFailureKind.RUN,
            exit_code=RUN_FAILURE_EXIT_CODE,
            agent_id=agent.agent_id,
            run_id=result.id,
            result_detail=detail or None,
            diagnostic_detail=diagnostic or None,
            phase=phase,
        )

    return SendRunResult(
        agent_id=agent.agent_id,
        run_id=result.id,
        status=status,
        result=result.result,
    )


_ERROR_DETAIL_MAX_LEN = 300


def _collect_error_and_text_strings(
    node: Any,
    errors: list[str],
    texts: list[str],
) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(value, str) and value.strip():
                stripped = value.strip()
                if stripped == "[REDACTED]":
                    continue
                if "error" in str(key).lower():
                    errors.append(stripped)
                elif str(key).lower() == "text":
                    texts.append(stripped)
            else:
                _collect_error_and_text_strings(value, errors, texts)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for item in node:
            _collect_error_and_text_strings(item, errors, texts)


def extract_run_error_detail(run: Any) -> str:
    """Best-effort error context for an errored run with an empty SDK result.

    Fetches the run conversation JSON (when the run handle exposes it) and
    returns the most recent error-keyed string, falling back to the last
    assistant prose. Never raises: any failure simply yields an empty string.
    """
    conversation_json = getattr(run, "conversation_json", None)
    if not callable(conversation_json):
        return ""
    try:
        raw = conversation_json()
    except Exception:
        logger.debug("conversation_json unavailable for error detail", exc_info=True)
        return ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ""

    errors: list[str] = []
    texts: list[str] = []
    _collect_error_and_text_strings(parsed, errors, texts)
    if errors:
        return truncate_activity_text(errors[-1], max_len=_ERROR_DETAIL_MAX_LEN)
    if texts:
        return "last agent output: " + truncate_activity_text(
            texts[-1], max_len=_ERROR_DETAIL_MAX_LEN
        )
    return ""


def is_opus_billing_failure(message: str) -> bool:
    """Return whether ``message`` looks like an Opus billing or credits failure."""
    lower = message.lower()
    return any(hint in lower for hint in _OPUS_BILLING_FAILURE_HINTS)


def should_retry_with_composer_after_opus_failure(exc: AgentRunError) -> bool:
    """
    Return whether to retry the implementation phase with Composer after Opus.

    Only applies to completed runs that errored, not SDK startup failures.
    """
    if exc.kind != RunFailureKind.RUN:
        return False
    detail = exc.result_detail or str(exc)
    return is_opus_billing_failure(detail)


def is_transient_cursor_agent_error(exc: CursorAgentError) -> bool:
    """Return whether a ``CursorAgentError`` looks like a short-lived SDK/network blip."""
    if getattr(exc, "is_retryable", False):
        return True

    message = str(exc).lower()
    if any(hint in message for hint in _NON_TRANSIENT_MESSAGE_HINTS):
        return False
    if any(hint in message for hint in _TRANSIENT_MESSAGE_HINTS):
        return True

    status = getattr(exc, "status", None)
    return status in _TRANSIENT_HTTP_STATUSES


def is_transient_agent_failure(exc: BaseException) -> bool:
    """
    Return whether ``exc`` is a transient agent failure worth retrying.

    Covers two cases:

    - SDK startup/send/wait failures (``RunFailureKind.STARTUP``) whose
      ``CursorAgentError`` cause looks like a short-lived network/SDK blip.
    - Runs that completed with ``status == "error"`` but **no SDK detail**
      (``RunFailureKind.RUN`` with empty ``result_detail``). In practice an
      errored run with an empty result is an upstream infrastructure blip
      (model/server/connection), not an agent logical failure; re-running the
      cycle with a fresh agent is the same recovery as a manual relaunch.

    Run failures that carry an SDK detail (e.g. billing/credits messages) and
    non-agent errors are never transient.
    """
    if not isinstance(exc, AgentRunError):
        return False
    if exc.kind == RunFailureKind.RUN:
        return not (exc.result_detail or "").strip()
    if exc.kind != RunFailureKind.STARTUP:
        return False
    cause = exc.cause
    if isinstance(cause, CursorAgentError):
        return is_transient_cursor_agent_error(cause)
    return False


def retry_delay_seconds(
    backoff_seconds: Sequence[int],
    *,
    failed_attempt: int,
) -> int:
    """Seconds to wait after failed attempt ``failed_attempt`` (1-based)."""
    if not backoff_seconds:
        return 0
    index = failed_attempt - 1
    if index < len(backoff_seconds):
        return int(backoff_seconds[index])
    return int(backoff_seconds[-1])


def should_retry_transient_failure(
    exc: BaseException,
    *,
    retry_on_transient: bool,
    failed_attempt: int,
    max_attempts: int,
) -> bool:
    """Whether to sleep and retry after ``exc`` on the current cycle."""
    if not retry_on_transient:
        return False
    if failed_attempt >= max_attempts:
        return False
    return is_transient_agent_failure(exc)
