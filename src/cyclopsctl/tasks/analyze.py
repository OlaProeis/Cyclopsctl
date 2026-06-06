"""Native task complexity analysis via Cursor SDK (phase-5 task 5)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cursor_sdk import ModelSelection, SDKModel

from cyclopsctl.runner import (
    AgentRunError,
    CreateAgentFn,
    SendFn,
    WaitFn,
    bootstrap_progress_writer,
    create_local_agent,
    send_and_wait,
)
from cyclopsctl.tasks.cli import DONE_STATUS, list_pending_tasks
from cyclopsctl.tasks.bootstrap_model import (
    bootstrap_model_attempt_chain,
    should_fallback_bootstrap_model,
)
from cyclopsctl.tasks.models import DEFAULT_ANALYZE_MODEL, resolve_analyze_model
from cyclopsctl.tasks.parse_prd import ParsePrdError, extract_json_text
from cyclopsctl.tasks.store import (
    DEFAULT_TAG,
    TaskStore,
    TaskStoreValidationError,
    ensure_native_layout,
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ANALYZE_TEMPLATE = PACKAGE_ROOT / "templates" / "analyze-complexity-prompt.md"

DEFAULT_BATCH_THRESHOLD = 15
DEFAULT_CHUNK_SIZE = 15

MIN_COMPLEXITY_SCORE = 1
MAX_COMPLEXITY_SCORE = 10


class AnalyzeComplexityError(RuntimeError):
    """Complexity analysis pipeline failure."""


@dataclass(frozen=True)
class AnalyzeComplexityConfig:
    """Options for a native complexity analysis run."""

    analyze_model: str = DEFAULT_ANALYZE_MODEL
    api_key: str | None = None
    skip_analyze: bool = False
    skip_if_exists: bool = True
    backfill_complexity: bool = True
    batch_threshold: int = DEFAULT_BATCH_THRESHOLD
    chunk_size: int = DEFAULT_CHUNK_SIZE


def _log_analyze_progress(message: str) -> None:
    print(f"cyclopsctl analyze-complexity: {message}", file=sys.stderr)


def _substitute_placeholders(text: str, mapping: dict[str, str]) -> str:
    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def load_analyze_template() -> str:
    """Load the bundled analyze-complexity prompt template."""
    if not ANALYZE_TEMPLATE.is_file():
        raise AnalyzeComplexityError(
            f"analyze-complexity template not found: {ANALYZE_TEMPLATE}"
        )
    return ANALYZE_TEMPLATE.read_text(encoding="utf-8")


def task_metadata_for_prompt(task: dict[str, Any]) -> dict[str, Any]:
    """Extract prompt-facing fields from a stored parent task."""
    task_id = task.get("id")
    return {
        "id": int(task_id) if task_id is not None else task_id,
        "title": str(task.get("title") or "").strip(),
        "description": str(task.get("description") or "").strip(),
        "details": str(task.get("details") or "").strip(),
    }


def render_analyze_prompt(*, tasks: Sequence[dict[str, Any]]) -> str:
    """Render the analyze-complexity template with task metadata."""
    template = load_analyze_template()
    metadata = [task_metadata_for_prompt(task) for task in tasks]
    tasks_json = json.dumps(metadata, indent=2, sort_keys=True)
    return _substitute_placeholders(template, {"TASKS_JSON": tasks_json})


def render_analyze_repair_prompt(*, invalid_response: str, error_message: str) -> str:
    """Build a one-shot repair prompt after invalid JSON or schema failure."""
    return (
        "Your previous response could not be parsed as valid complexity JSON.\n\n"
        f"Validation error: {error_message}\n\n"
        "Previous response:\n"
        f"{invalid_response.strip()}\n\n"
        "Return corrected JSON only — no markdown fences or commentary. "
        "Use the same schema: a JSON object with a `complexityAnalysis` array. "
        "Include every task from the original request with integer "
        f"`complexityScore` values between {MIN_COMPLEXITY_SCORE} and "
        f"{MAX_COMPLEXITY_SCORE}."
    )


def chunk_tasks(
    tasks: Sequence[dict[str, Any]],
    *,
    batch_threshold: int = DEFAULT_BATCH_THRESHOLD,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[list[dict[str, Any]]]:
    """
    Split tasks into agent prompt batches.

    Queues at or below ``batch_threshold`` are analyzed in one call; larger
    queues are split into ``chunk_size`` task chunks.
    """
    task_list = list(tasks)
    if not task_list:
        return []
    if len(task_list) <= batch_threshold:
        return [task_list]
    size = max(1, chunk_size)
    return [task_list[index : index + size] for index in range(0, len(task_list), size)]


def _normalize_analysis_item(item: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise TaskStoreValidationError(
            f"complexityAnalysis item at index {index} must be a JSON object"
        )

    task_id_raw = item.get("taskId")
    if task_id_raw is None:
        raise TaskStoreValidationError(
            f"complexityAnalysis item at index {index} is missing taskId"
        )
    try:
        task_id = int(task_id_raw)
    except (TypeError, ValueError) as exc:
        raise TaskStoreValidationError(
            f"complexityAnalysis taskId must be an integer: {task_id_raw!r}"
        ) from exc

    title = item.get("taskTitle")
    if not isinstance(title, str) or not title.strip():
        raise TaskStoreValidationError(
            f"complexityAnalysis item for task {task_id} is missing taskTitle"
        )

    score_raw = item.get("complexityScore")
    if score_raw is None:
        raise TaskStoreValidationError(
            f"complexityAnalysis item for task {task_id} is missing complexityScore"
        )
    try:
        score = int(score_raw)
    except (TypeError, ValueError) as exc:
        raise TaskStoreValidationError(
            f"complexityScore must be an integer for task {task_id}: {score_raw!r}"
        ) from exc
    if score < MIN_COMPLEXITY_SCORE or score > MAX_COMPLEXITY_SCORE:
        raise TaskStoreValidationError(
            f"complexityScore for task {task_id} must be between "
            f"{MIN_COMPLEXITY_SCORE} and {MAX_COMPLEXITY_SCORE}; got {score}"
        )

    reasoning = item.get("reasoning", "")
    if reasoning is None:
        reasoning = ""
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)

    return {
        "taskId": task_id,
        "taskTitle": title.strip(),
        "complexityScore": score,
        "reasoning": reasoning.strip(),
    }


def validate_analysis_items(
    items: list[dict[str, Any]],
    *,
    expected_task_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Validate agent-produced complexity analysis records."""
    if not items:
        raise TaskStoreValidationError("complexityAnalysis list is empty")

    normalized = [
        _normalize_analysis_item(item, index=index) for index, item in enumerate(items)
    ]
    seen_ids = [item["taskId"] for item in normalized]
    if len(seen_ids) != len(set(seen_ids)):
        raise TaskStoreValidationError("complexityAnalysis contains duplicate taskId values")

    if expected_task_ids is not None:
        missing = expected_task_ids - set(seen_ids)
        extra = set(seen_ids) - expected_task_ids
        if missing:
            raise TaskStoreValidationError(
                f"complexityAnalysis missing task ids: {sorted(missing)}"
            )
        if extra:
            raise TaskStoreValidationError(
                f"complexityAnalysis includes unexpected task ids: {sorted(extra)}"
            )

    normalized.sort(key=lambda item: item["taskId"])
    return normalized


def parse_analysis_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse and validate complexity analysis from decoded JSON."""
    if isinstance(payload, list):
        items_raw = payload
    elif isinstance(payload, dict):
        items_raw = payload.get("complexityAnalysis")
        if not isinstance(items_raw, list):
            raise TaskStoreValidationError(
                "JSON object must include a complexityAnalysis array"
            )
    else:
        raise TaskStoreValidationError("parsed JSON must be an object or array")

    return validate_analysis_items(items_raw)


def parse_analysis_response(response_text: str) -> list[dict[str, Any]]:
    """Extract JSON from an agent response and validate analysis records."""
    try:
        json_text = extract_json_text(response_text)
    except ParsePrdError as exc:
        raise AnalyzeComplexityError(str(exc)) from exc
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise AnalyzeComplexityError(f"invalid JSON in agent response: {exc}") from exc
    try:
        return parse_analysis_payload(payload)
    except TaskStoreValidationError as exc:
        raise AnalyzeComplexityError(str(exc)) from exc


def merge_analysis_chunks(chunks: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge chunk analysis results into one sorted list."""
    merged: list[dict[str, Any]] = []
    for chunk in chunks:
        merged.extend(chunk)
    return validate_analysis_items(merged)


def build_complexity_report(
    analysis: Sequence[dict[str, Any]],
    *,
    used_research: bool = False,
) -> dict[str, Any]:
    """Build the report document written to disk."""
    items = list(analysis)
    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "tasksAnalyzed": len(items),
            "usedResearch": used_research,
        },
        "complexityAnalysis": items,
    }


def backfill_task_complexity(
    tasks: list[dict[str, Any]],
    analysis: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Write complexity scores from analysis back onto task records."""
    scores = {item["taskId"]: item["complexityScore"] for item in analysis}
    updated: list[dict[str, Any]] = []
    for task in tasks:
        copy = dict(task)
        task_id = copy.get("id")
        try:
            numeric_id = int(task_id)
        except (TypeError, ValueError):
            updated.append(copy)
            continue
        if numeric_id in scores:
            copy["complexity"] = scores[numeric_id]
        updated.append(copy)
    return updated


def should_skip_analyze(
    report_path: Path,
    *,
    skip_analyze: bool = False,
    skip_if_exists: bool = True,
) -> bool:
    """Return True when analysis should be skipped."""
    if skip_analyze:
        return True
    return skip_if_exists and report_path.is_file()


def _resolve_api_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("CURSOR_API_KEY", "").strip()
    if not key:
        raise AnalyzeComplexityError(
            "CURSOR_API_KEY is required for native complexity analysis "
            "(set it in the environment or project .env)"
        )
    return key


def _call_analyze_agent_once(
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
    progress = bootstrap_progress_writer(_log_analyze_progress)
    try:
        result = send_and_wait(
            agent,
            prompt,
            phase="analyze-complexity",
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
        raise AnalyzeComplexityError("agent completed without returning complexity JSON")
    return response


def _call_analyze_agent(
    *,
    project_root: Path,
    prompt: str,
    analyze_model: str,
    api_key: str,
    create_agent: CreateAgentFn | None,
    send_fn: SendFn | None,
    wait_fn: WaitFn | None,
    list_models: Callable[..., list[SDKModel]] | None,
) -> str:
    candidates = bootstrap_model_attempt_chain(
        analyze_model,
        api_key=api_key,
        list_models=list_models,
    )
    last_exc: AgentRunError | None = None
    for index, model in enumerate(candidates):
        try:
            return _call_analyze_agent_once(
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
                _log_analyze_progress(
                    f"{model.id} unavailable; retrying with {remaining[0].id}"
                )
                continue
            raise AnalyzeComplexityError(str(exc)) from exc
    if last_exc is not None:
        raise AnalyzeComplexityError(str(last_exc)) from last_exc
    raise AnalyzeComplexityError("no bootstrap model candidates available")


def _analyze_task_chunk(
    *,
    project_root: Path,
    tasks: Sequence[dict[str, Any]],
    analyze_model: str,
    api_key: str,
    create_agent: CreateAgentFn | None,
    send_fn: SendFn | None,
    wait_fn: WaitFn | None,
    list_models: Callable[..., list[SDKModel]] | None,
) -> list[dict[str, Any]]:
    expected_ids = {int(task["id"]) for task in tasks}
    prompt = render_analyze_prompt(tasks=tasks)
    response = _call_analyze_agent(
        project_root=project_root,
        prompt=prompt,
        analyze_model=analyze_model,
        api_key=api_key,
        create_agent=create_agent,
        send_fn=send_fn,
        wait_fn=wait_fn,
        list_models=list_models,
    )

    try:
        items = parse_analysis_response(response)
    except AnalyzeComplexityError as first_error:
        _log_analyze_progress("invalid JSON from agent; retrying once with repair prompt")
        repair_prompt = render_analyze_repair_prompt(
            invalid_response=response,
            error_message=str(first_error),
        )
        repair_response = _call_analyze_agent(
            project_root=project_root,
            prompt=repair_prompt,
            analyze_model=analyze_model,
            api_key=api_key,
            create_agent=create_agent,
            send_fn=send_fn,
            wait_fn=wait_fn,
            list_models=list_models,
        )
        try:
            items = parse_analysis_response(repair_response)
        except AnalyzeComplexityError as second_error:
            raise AnalyzeComplexityError(
                f"complexity analysis failed after repair retry: {second_error}"
            ) from second_error

    return validate_analysis_items(items, expected_task_ids=expected_ids)


def analyze_complexity_with_cursor(
    project_root: Path,
    *,
    tag: str | None = None,
    config: AnalyzeComplexityConfig | None = None,
    create_agent: CreateAgentFn | None = None,
    send_fn: SendFn | None = None,
    wait_fn: WaitFn | None = None,
    list_models: Callable[..., list[SDKModel]] | None = None,
) -> bool:
    """
    Analyze pending native tasks and write ``complexity-report.json``.

    Returns True when analysis ran and wrote a report; False when skipped.
    """
    cfg = config or AnalyzeComplexityConfig()
    resolved_root = project_root.resolve()
    store = TaskStore(resolved_root, backend="native")
    store.ensure_native_layout()

    report_path = store.complexity_report_path()
    if should_skip_analyze(
        report_path,
        skip_analyze=cfg.skip_analyze,
        skip_if_exists=cfg.skip_if_exists,
    ):
        _log_analyze_progress("skipping complexity analysis (report exists or --skip-analyze)")
        return False

    active_tag, pending_tasks = list_pending_tasks(resolved_root, tag=tag)
    active_tag = tag or active_tag or store.current_tag() or DEFAULT_TAG

    if not pending_tasks:
        _log_analyze_progress(f"no non-done tasks to analyze in tag {active_tag!r}")
        return False

    api_key = _resolve_api_key(cfg.api_key)
    candidates = bootstrap_model_attempt_chain(
        cfg.analyze_model,
        api_key=api_key,
        list_models=list_models,
    )
    primary = candidates[0] if candidates else resolve_analyze_model(
        cfg.analyze_model,
        api_key=api_key,
        list_models=list_models,
        log_fn=_log_analyze_progress,
    )
    chain_note = (
        f" → {candidates[1].id} fallback"
        if len(candidates) > 1
        else ""
    )

    batches = chunk_tasks(
        pending_tasks,
        batch_threshold=cfg.batch_threshold,
        chunk_size=cfg.chunk_size,
    )
    _log_analyze_progress(
        f"analyzing {len(pending_tasks)} tasks in tag {active_tag!r} "
        f"({len(batches)} agent call(s), model={primary.id}{chain_note})"
    )

    chunk_results: list[list[dict[str, Any]]] = []
    for batch in batches:
        chunk_results.append(
            _analyze_task_chunk(
                project_root=resolved_root,
                tasks=batch,
                analyze_model=cfg.analyze_model,
                api_key=api_key,
                create_agent=create_agent,
                send_fn=send_fn,
                wait_fn=wait_fn,
                list_models=list_models,
            )
        )

    analysis = merge_analysis_chunks(chunk_results)
    report = build_complexity_report(analysis)
    store.save_complexity_report(report)

    if cfg.backfill_complexity:
        all_tasks = store.load_tag_tasks(active_tag)
        updated_tasks = backfill_task_complexity(all_tasks, analysis)
        store.save_tag_tasks(updated_tasks, tag=active_tag, merge=True)

    _log_analyze_progress(
        f"saved complexity report for {len(analysis)} tasks to {report_path.name}"
    )
    return True
