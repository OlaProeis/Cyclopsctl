"""Tests for task 3: prompt reading, Task ID parsing, and content hashing."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyclopsctl.prompt import (
    AI_CONTEXT_SECTION_HEADER,
    PROMPT_SECTION_SEPARATOR,
    HandoverSnapshot,
    PromptComposeError,
    PromptError,
    compose_agent_prompt,
    load_ai_context,
    normalize_whitespace,
    parse_task_id,
    prompt_content_from_raw,
    read_prompt_text,
    reset_ai_context_warnings,
    sha256_content_hash,
    snapshot_handover,
)


def test_read_prompt_text_preserves_exact_content(tmp_path: Path):
    path = tmp_path / "handover.md"
    body = "# Task ID: 3\r\n\r\nLine two  \n"
    path.write_bytes(body.encode("utf-8"))
    assert read_prompt_text(path) == body


def test_parse_task_id_valid_markers():
    assert parse_task_id("# Task ID: 7\n\nBody") == 7
    assert parse_task_id("intro\n#  task  id :  42  \nrest", required=True) == 42
    assert parse_task_id("# TASK ID: 1\n") == 1


def test_parse_task_id_missing_when_required():
    with pytest.raises(PromptError, match="Task ID marker"):
        parse_task_id("No marker here", required=True)


def test_parse_task_id_optional_returns_none():
    assert parse_task_id("no marker", required=False) is None


def test_parse_task_id_rejects_invalid_markers():
    invalid = [
        "# Task ID: 7.1",
        "# Task ID: abc",
        "# Task ID:",
        "# TaskID: 5",
    ]
    for text in invalid:
        with pytest.raises(PromptError, match="Task ID marker"):
            parse_task_id(text, required=True)


def test_normalize_and_hash_ignore_insignificant_whitespace():
    a = "# Task ID: 3\n\n## Section\n\nBody text."
    b = "#   Task   ID:   3   \n\n##   Section   \n\n   Body   text.   "
    norm_a = normalize_whitespace(a)
    norm_b = normalize_whitespace(b)
    assert norm_a == norm_b
    assert sha256_content_hash(norm_a) == sha256_content_hash(norm_b)
    assert prompt_content_from_raw(a).content_hash == prompt_content_from_raw(b).content_hash


def test_hash_changes_when_substantive_content_changes():
    first = prompt_content_from_raw("# Task ID: 3\n\nAlpha")
    second = prompt_content_from_raw("# Task ID: 3\n\nBeta")
    assert first.content_hash != second.content_hash


def test_prompt_content_from_raw_keeps_raw_unchanged():
    raw = "# Task ID: 1\n\n  spaced  "
    content = prompt_content_from_raw(raw)
    assert content.raw == raw
    assert content.normalized == normalize_whitespace(raw)


def test_read_prompt_text_missing_file_raises(tmp_path: Path):
    with pytest.raises(PromptError, match="not found"):
        read_prompt_text(tmp_path / "missing.md")


def test_snapshot_handover_present_file(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    raw = "# Task ID: 9\n\nWork items.\n"
    path.write_bytes(raw.encode("utf-8"))
    snap = snapshot_handover(path)
    assert snap.path == path
    assert snap.missing is False
    assert snap.task_id == 9
    assert snap.raw == raw
    assert snap.normalized == normalize_whitespace(raw)
    assert snap.content_hash == prompt_content_from_raw(raw).content_hash


def test_snapshot_handover_cycle1_missing_allow_missing(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    snap = snapshot_handover(path, allow_missing=True)
    assert snap.missing is True
    assert snap.task_id is None
    assert snap.raw == ""
    assert snap.normalized == ""
    assert snap.content_hash == sha256_content_hash("")


def test_snapshot_handover_missing_without_allow_raises(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    with pytest.raises(PromptError, match="not found"):
        snapshot_handover(path, allow_missing=False)


def test_snapshot_handover_present_without_marker(tmp_path: Path):
    path = tmp_path / "current-handover-prompt.md"
    path.write_bytes(b"# Session\n\nNo task line.\n")
    snap = snapshot_handover(path)
    assert snap.missing is False
    assert snap.task_id is None
    assert snap.content_hash == prompt_content_from_raw(snap.raw).content_hash


@pytest.fixture(autouse=True)
def _reset_ai_context_warn_state():
    reset_ai_context_warnings()
    yield
    reset_ai_context_warnings()


def test_compose_agent_prompt_prepends_ai_context(tmp_path: Path):
    ai_path = tmp_path / "ai-context.md"
    ai_path.write_text("# Rules\n\nFollow conventions.\n", encoding="utf-8")
    body = "# Task ID: 15\n\nImplement task.\n"

    composed, attachment = compose_agent_prompt(body, ai_path)

    assert attachment.missing is False
    assert composed.startswith(f"{AI_CONTEXT_SECTION_HEADER}\n\n# Rules")
    assert "Follow conventions." in composed
    assert PROMPT_SECTION_SEPARATOR in composed
    assert composed.endswith(body)
    assert attachment.content_hash == sha256_content_hash(
        normalize_whitespace(attachment.content)
    )


def test_compose_agent_prompt_missing_file_returns_body_unchanged(tmp_path: Path, caplog):
    import logging

    missing = tmp_path / "ai-context.md"
    body = "# Handover\n"

    with caplog.at_level(logging.WARNING):
        composed, attachment = compose_agent_prompt(body, missing)
        composed_again, _ = compose_agent_prompt(body, missing)

    assert composed == body
    assert composed_again == body
    assert attachment.missing is True
    assert sum("ai-context file not found" in r.message for r in caplog.records) == 1


def test_compose_agent_prompt_skips_ai_context_when_disabled(tmp_path: Path):
    ai_path = tmp_path / "ai-context.md"
    ai_path.write_text("# Context\n", encoding="utf-8")
    body = "# Update handover\n"

    composed, attachment = compose_agent_prompt(
        body,
        ai_path,
        attach_ai_context=False,
    )

    assert composed == body
    assert attachment.missing is True


def test_compose_agent_prompt_missing_required_raises(tmp_path: Path):
    missing = tmp_path / "ai-context.md"
    with pytest.raises(PromptComposeError, match="required"):
        compose_agent_prompt("# Body\n", missing, require_ai_context=True)


def test_load_ai_context_truncates_with_warning(tmp_path: Path, caplog):
    import logging

    ai_path = tmp_path / "ai-context.md"
    ai_path.write_text("x" * 50, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        attachment = load_ai_context(ai_path, max_chars=20)

    assert attachment.truncated is True
    assert len(attachment.content) > 20
    assert "[... ai-context truncated ...]" in attachment.content
    assert any("truncating attachment" in r.message for r in caplog.records)


def test_compose_agent_prompt_logs_attachment_hash(tmp_path: Path, caplog):
    import logging

    ai_path = tmp_path / "ai-context.md"
    ai_path.write_text("# Context\n", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        compose_agent_prompt("# Body\n", ai_path)

    assert any("Attached ai-context path=" in r.message for r in caplog.records)
    assert any("hash=" in r.message for r in caplog.records)
