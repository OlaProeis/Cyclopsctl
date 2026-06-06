"""Native PRD parsing via Cursor SDK (phase-5 task 4)."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cursor_sdk import ModelSelection, SDKModel

from cyclopsctl.project_setup import write_last_parsed_prd
from cyclopsctl.tasks.bootstrap_model import (
    bootstrap_model_attempt_chain,
    should_fallback_bootstrap_model,
)
from cyclopsctl.tasks.models import (
    DEFAULT_MAX_TASKS,
    DEFAULT_PARSE_MODEL,
    resolve_parse_model,
)
from cyclopsctl.runner import (
    AgentRunError,
    CreateAgentFn,
    SendFn,
    WaitFn,
    bootstrap_progress_writer,
    create_local_agent,
    send_and_wait,
)
from cyclopsctl.tasks.cli import ALLOWED_STATUSES
from cyclopsctl.tasks.store import (
    DEFAULT_TAG,
    TaskStore,
    TaskStoreValidationError,
    ensure_native_layout,
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PARSE_PRD_TEMPLATE = PACKAGE_ROOT / "templates" / "parse-prd-prompt.md"

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


class ParsePrdError(RuntimeError):
    """PRD parse pipeline failure."""


@dataclass(frozen=True)
class ParsePrdConfig:
    """Options for a native PRD parse run."""

    max_tasks: int | None = DEFAULT_MAX_TASKS
    parse_model: str = DEFAULT_PARSE_MODEL
    api_key: str | None = None


def _log_parse_progress(message: str) -> None:
    print(f"cyclopsctl parse-prd: {message}", file=sys.stderr)


def _substitute_placeholders(text: str, mapping: dict[str, str]) -> str:
    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def load_parse_prd_template() -> str:
    """Load the bundled parse-prd prompt template."""
    if not PARSE_PRD_TEMPLATE.is_file():
        raise ParsePrdError(f"parse-prd template not found: {PARSE_PRD_TEMPLATE}")
    return PARSE_PRD_TEMPLATE.read_text(encoding="utf-8")


def render_parse_prd_prompt(
    *,
    prd_content: str,
    max_tasks: int | None = None,
) -> str:
    """Render the parse-prd template with PRD content and optional task cap."""
    template = load_parse_prd_template()
    max_suffix = ""
    if max_tasks is not None and max_tasks > 0:
        max_suffix = f" Do not produce more than **{max_tasks}** parent tasks."
    return _substitute_placeholders(
        template,
        {
            "PRD_CONTENT": prd_content.strip(),
            "MAX_TASKS_SUFFIX": max_suffix,
        },
    )


def render_repair_prompt(*, invalid_response: str, error_message: str) -> str:
    """Build a one-shot repair prompt after invalid JSON or schema failure."""
    return (
        "Your previous response could not be parsed as valid task JSON.\n\n"
        f"Validation error: {error_message}\n\n"
        "Previous response:\n"
        f"{invalid_response.strip()}\n\n"
        "Return corrected JSON only — no markdown fences or commentary. "
        "Use the same schema: a JSON object with a `tasks` array. "
        "Task ids must be sequential integers starting at 1. "
        'Every task `status` must be "pending".'
    )


def extract_json_text(response_text: str) -> str:
    """Extract a JSON payload from an agent response."""
    stripped = response_text.strip()
    if not stripped:
        raise ParsePrdError("agent returned an empty response")

    fence_match = _JSON_FENCE_RE.search(stripped)
    if fence_match:
        return fence_match.group(1).strip()

    if stripped.startswith("{") or stripped.startswith("["):
        return stripped

    object_start = stripped.find("{")
    array_start = stripped.find("[")
    starts = [index for index in (object_start, array_start) if index >= 0]
    if not starts:
        raise ParsePrdError("agent response does not contain JSON")

    start = min(starts)
    decoder = json.JSONDecoder()
    _, end = decoder.raw_decode(stripped[start:])
    return stripped[start : start + end]


def _normalize_task_record(task: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise TaskStoreValidationError(f"task at index {index} must be a JSON object")

    task_id = task.get("id")
    if task_id is None:
        raise TaskStoreValidationError(f"task at index {index} is missing id")
    try:
        numeric_id = int(task_id)
    except (TypeError, ValueError) as exc:
        raise TaskStoreValidationError(f"task id must be an integer: {task_id!r}") from exc

    title = task.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TaskStoreValidationError(f"task {numeric_id} is missing title")

    status = task.get("status")
    if not isinstance(status, str) or not status.strip():
        raise TaskStoreValidationError(f"task {numeric_id} is missing status")
    normalized_status = status.strip()
    if normalized_status not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        raise TaskStoreValidationError(
            f"task {numeric_id} has invalid status {normalized_status!r}; "
            f"allowed: {allowed}"
        )

    dependencies = task.get("dependencies", [])
    if dependencies is None:
        dependencies = []
    if not isinstance(dependencies, list):
        raise TaskStoreValidationError(
            f"task {numeric_id} dependencies must be a list"
        )

    normalized: dict[str, Any] = {
        "id": numeric_id,
        "title": title.strip(),
        "description": str(task.get("description") or "").strip(),
        "details": str(task.get("details") or "").strip(),
        "testStrategy": str(task.get("testStrategy") or "").strip(),
        "priority": str(task.get("priority") or "medium").strip(),
        "dependencies": [str(dep) for dep in dependencies],
        "status": normalized_status,
    }

    if task.get("complexity") is not None:
        try:
            normalized["complexity"] = int(task["complexity"])
        except (TypeError, ValueError):
            pass

    return normalized


def validate_parsed_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate agent-produced tasks: sequential ids from 1 and allowed statuses."""
    if not tasks:
        raise TaskStoreValidationError("parsed task list is empty")

    normalized = [_normalize_task_record(task, index=index) for index, task in enumerate(tasks)]
    ids = [task["id"] for task in normalized]
    expected = list(range(1, len(normalized) + 1))
    if ids != expected:
        raise TaskStoreValidationError(
            f"task ids must be sequential starting at 1; got {ids}"
        )
    return normalized


def parse_tasks_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse and validate tasks from decoded JSON."""
    if isinstance(payload, list):
        tasks_raw = payload
    elif isinstance(payload, dict):
        tasks_raw = payload.get("tasks")
        if not isinstance(tasks_raw, list):
            raise TaskStoreValidationError("JSON object must include a tasks array")
    else:
        raise TaskStoreValidationError("parsed JSON must be an object or array")

    return validate_parsed_tasks(tasks_raw)


def parse_tasks_response(response_text: str) -> list[dict[str, Any]]:
    """Extract JSON from an agent response and validate task records."""
    json_text = extract_json_text(response_text)
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ParsePrdError(f"invalid JSON in agent response: {exc}") from exc
    try:
        return parse_tasks_payload(payload)
    except TaskStoreValidationError as exc:
        raise ParsePrdError(str(exc)) from exc


def remap_tasks_for_append(
    existing: list[dict[str, Any]],
    new_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Offset appended task ids and in-batch dependencies after existing tasks."""
    if not existing:
        return new_tasks

    offset = max(int(task["id"]) for task in existing)
    id_map = {str(index): str(index + offset) for index in range(1, len(new_tasks) + 1)}
    remapped: list[dict[str, Any]] = []
    for task in new_tasks:
        copy = dict(task)
        copy["id"] = int(task["id"]) + offset
        remapped_deps: list[str] = []
        for dep in task.get("dependencies", []) or []:
            dep_str = str(dep)
            remapped_deps.append(id_map.get(dep_str, dep_str))
        copy["dependencies"] = remapped_deps
        remapped.append(copy)
    return existing + remapped


def persist_parsed_tasks(
    store: TaskStore,
    tasks: list[dict[str, Any]],
    *,
    tag: str,
    append: bool,
) -> None:
    """Write validated tasks to native storage (replace or append)."""
    store.ensure_native_layout()
    if append:
        try:
            existing = store.load_tag_tasks(tag)
        except Exception:
            existing = []
        merged = remap_tasks_for_append(existing, tasks)
        store.save_tag_tasks(merged, tag=tag, merge=True)
    else:
        store.save_tag_tasks(tasks, tag=tag, merge=True)


def _resolve_api_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("CURSOR_API_KEY", "").strip()
    if not key:
        raise ParsePrdError(
            "CURSOR_API_KEY is required for native PRD parsing "
            "(set it in the environment or project .env)"
        )
    return key


def _call_parse_agent_once(
    *,
    project_root: Path,
    prompt: str,
    model: ModelSelection,
    api_key: str,
    create_agent: CreateAgentFn | None,
    send_fn: SendFn | None,
    wait_fn: WaitFn | None,
) -> str:
    agent = create_local_agent(
        model=model,
        project_root=project_root,
        api_key=api_key,
        create_agent=create_agent,
    )
    progress = bootstrap_progress_writer(_log_parse_progress)
    try:
        result = send_and_wait(
            agent,
            prompt,
            phase="parse-prd",
            send_fn=send_fn,
            wait_fn=wait_fn,
            on_activity=progress,
        )
    except AgentRunError:
        raise
    finally:
        progress.flush()
        close = getattr(agent, "close", None)
        if callable(close):
            close()

    response = (result.result or "").strip()
    if not response:
        raise ParsePrdError("agent completed without returning task JSON")
    return response


def _call_parse_agent(
    *,
    project_root: Path,
    prompt: str,
    parse_model: str,
    api_key: str,
    create_agent: CreateAgentFn | None,
    send_fn: SendFn | None,
    wait_fn: WaitFn | None,
    list_models: Callable[..., list[SDKModel]] | None,
) -> str:
    candidates = bootstrap_model_attempt_chain(
        parse_model,
        api_key=api_key,
        list_models=list_models,
    )
    last_exc: AgentRunError | None = None
    for index, model in enumerate(candidates):
        try:
            return _call_parse_agent_once(
                project_root=project_root,
                prompt=prompt,
                model=model,
                api_key=api_key,
                create_agent=create_agent,
                send_fn=send_fn,
                wait_fn=wait_fn,
            )
        except AgentRunError as exc:
            last_exc = exc
            remaining = candidates[index + 1 :]
            if should_fallback_bootstrap_model(
                exc, attempted=model, remaining=remaining
            ):
                _log_parse_progress(
                    f"{model.id} unavailable; retrying with {remaining[0].id}"
                )
                continue
            raise ParsePrdError(str(exc)) from exc
    if last_exc is not None:
        raise ParsePrdError(str(last_exc)) from last_exc
    raise ParsePrdError("no bootstrap model candidates available")


def parse_prd_with_cursor(
    project_root: Path,
    prd_path: Path,
    *,
    tag: str | None = None,
    append: bool = False,
    config: ParsePrdConfig | None = None,
    create_agent: CreateAgentFn | None = None,
    send_fn: SendFn | None = None,
    wait_fn: WaitFn | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
) -> None:
    """
    Parse a PRD into native task storage using the Cursor SDK.

    On invalid JSON or schema failure, retries once with a repair prompt.
    Writes ``last-parsed-prd.json`` only after successful persistence.
    """
    cfg = config or ParsePrdConfig()
    resolved_root = project_root.resolve()
    resolved_prd = prd_path.resolve()
    if not resolved_prd.is_file():
        raise ParsePrdError(f"PRD file not found: {resolved_prd}")

    prd_content = resolved_prd.read_text(encoding="utf-8")
    if not prd_content.strip():
        raise ParsePrdError(f"PRD file is empty: {resolved_prd}")

    store = TaskStore(resolved_root, backend="native")
    active_tag = tag or store.current_tag() or DEFAULT_TAG

    api_key = _resolve_api_key(cfg.api_key)
    candidates = bootstrap_model_attempt_chain(
        cfg.parse_model,
        api_key=api_key,
        list_models=list_models,
    )
    primary = candidates[0] if candidates else resolve_parse_model(
        cfg.parse_model,
        api_key=api_key,
        list_models=list_models,
        log_fn=_log_parse_progress,
    )
    chain_note = (
        f" → {candidates[1].id} fallback"
        if len(candidates) > 1
        else ""
    )

    cap_part = (
        f", max {cfg.max_tasks} tasks"
        if cfg.max_tasks is not None and cfg.max_tasks > 0
        else ", model-chosen task count"
    )
    _log_parse_progress(
        f"parsing {resolved_prd.name} into tag {active_tag!r} "
        f"(model={primary.id}{cap_part}{chain_note})"
    )

    prompt = render_parse_prd_prompt(
        prd_content=prd_content,
        max_tasks=cfg.max_tasks,
    )
    response = _call_parse_agent(
        project_root=resolved_root,
        prompt=prompt,
        parse_model=cfg.parse_model,
        api_key=api_key,
        create_agent=create_agent,
        send_fn=send_fn,
        wait_fn=wait_fn,
        list_models=list_models,
    )

    try:
        tasks = parse_tasks_response(response)
    except ParsePrdError as first_error:
        _log_parse_progress("invalid JSON from agent; retrying once with repair prompt")
        repair_prompt = render_repair_prompt(
            invalid_response=response,
            error_message=str(first_error),
        )
        repair_response = _call_parse_agent(
            project_root=resolved_root,
            prompt=repair_prompt,
            parse_model=cfg.parse_model,
            api_key=api_key,
            create_agent=create_agent,
            send_fn=send_fn,
            wait_fn=wait_fn,
            list_models=list_models,
        )
        try:
            tasks = parse_tasks_response(repair_response)
        except ParsePrdError as second_error:
            raise ParsePrdError(
                f"PRD parse failed after repair retry: {second_error}"
            ) from second_error

    persist_parsed_tasks(store, tasks, tag=active_tag, append=append)
    write_last_parsed_prd(resolved_root, resolved_prd, tag=active_tag)
    _log_parse_progress(f"saved {len(tasks)} tasks to tag {active_tag!r}")
