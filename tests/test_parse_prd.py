"""Tests for native PRD parsing via Cursor SDK (phase-5 task 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cursor_sdk import ModelSelection, RunResult, SDKModel

from cyclopsctl.project_setup import LAST_PARSED_PRD_REL, load_last_parsed_prd
from cyclopsctl.tasks.native_backend import NativeTaskBackend
from cyclopsctl.tasks.models import detect_sonnet_model, resolve_parse_model
from cyclopsctl.tasks.parse_prd import (
    ParsePrdConfig,
    ParsePrdError,
    extract_json_text,
    load_parse_prd_template,
    parse_prd_with_cursor,
    parse_tasks_payload,
    parse_tasks_response,
    remap_tasks_for_append,
    render_parse_prd_prompt,
    render_repair_prompt,
    validate_parsed_tasks,
)
from cyclopsctl.tasks.store import TaskStore, ensure_native_layout


def _sample_task(task_id: int, *, status: str = "pending") -> dict:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": "desc",
        "details": "details",
        "testStrategy": "pytest",
        "priority": "high",
        "dependencies": [] if task_id == 1 else [str(task_id - 1)],
        "status": status,
    }


def _tasks_json(tasks: list[dict]) -> str:
    return json.dumps({"tasks": tasks})


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prd.md").write_text("# Sample PRD\n\nBuild a widget.\n", encoding="utf-8")
    return root.resolve()


class _FakeRun:
    def __init__(self, *, result: str) -> None:
        self.id = "run-1"
        self._result = result

    def wait(self) -> RunResult:
        return RunResult(
            id=self.id,
            agent_id="agent-1",
            status="finished",
            result=self._result,
        )


class _FakeAgent:
    def __init__(self) -> None:
        self.agent_id = "agent-1"
        self.sent: list[str] = []
        self.closed = False

    def send(self, prompt: str) -> _FakeRun:
        self.sent.append(prompt)
        raise AssertionError("use fake_send_fn in tests")

    def close(self) -> None:
        self.closed = True


def _make_sdk_mocks(responses: list[str]):
    remaining = list(responses)
    agents: list[_FakeAgent] = []
    prompts: list[str] = []

    def fake_create(**_kwargs: object) -> _FakeAgent:
        agent = _FakeAgent()
        agents.append(agent)
        return agent

    def fake_send(agent: _FakeAgent, prompt: str) -> _FakeRun:
        agent.sent.append(prompt)
        prompts.append(prompt)
        if not remaining:
            raise AssertionError("no more fake responses")
        return _FakeRun(result=remaining.pop(0))

    return fake_create, fake_send, agents, prompts


def test_load_parse_prd_template_contains_placeholders():
    template = load_parse_prd_template()
    assert "{{PRD_CONTENT}}" in template
    assert "{{MAX_TASKS_SUFFIX}}" in template
    assert "What ONE task is" in template
    assert "JSON" in template


def test_render_parse_prd_prompt_without_cap(project_root: Path):
    prd = project_root / "prd.md"
    prompt = render_parse_prd_prompt(prd_content=prd.read_text(encoding="utf-8"))
    assert "Build a widget." in prompt
    assert "Do not produce more than" not in prompt
    assert "{{PRD_CONTENT}}" not in prompt
    assert "{{MAX_TASKS_SUFFIX}}" not in prompt


def test_render_parse_prd_prompt_with_max_cap(project_root: Path):
    prd = project_root / "prd.md"
    prompt = render_parse_prd_prompt(
        prd_content=prd.read_text(encoding="utf-8"),
        max_tasks=7,
    )
    assert "Build a widget." in prompt
    assert "Do not produce more than **7** parent tasks" in prompt
    assert "{{PRD_CONTENT}}" not in prompt
    assert "{{MAX_TASKS_SUFFIX}}" not in prompt


def test_render_repair_prompt_includes_error_and_response():
    repair = render_repair_prompt(
        invalid_response='{"broken": true}',
        error_message="missing tasks array",
    )
    assert "missing tasks array" in repair
    assert '{"broken": true}' in repair
    assert "Return corrected JSON only" in repair


def test_extract_json_text_from_markdown_fence():
    payload = _tasks_json([_sample_task(1)])
    wrapped = f"Here is the output:\n```json\n{payload}\n```\n"
    assert extract_json_text(wrapped) == payload


def test_extract_json_text_from_bare_object():
    payload = _tasks_json([_sample_task(1), _sample_task(2)])
    assert extract_json_text(payload) == payload


def test_validate_parsed_tasks_requires_sequential_ids():
    tasks = [_sample_task(1), {**_sample_task(2), "id": 3}]
    with pytest.raises(Exception, match="sequential"):
        validate_parsed_tasks(tasks)


def test_validate_parsed_tasks_rejects_invalid_status():
    tasks = [_sample_task(1, status="invalid")]
    with pytest.raises(Exception, match="invalid status"):
        validate_parsed_tasks(tasks)


def test_parse_tasks_payload_accepts_array_or_object():
    tasks = [_sample_task(1), _sample_task(2)]
    assert parse_tasks_payload({"tasks": tasks}) == validate_parsed_tasks(tasks)
    assert parse_tasks_payload(tasks) == validate_parsed_tasks(tasks)


def test_parse_tasks_response_success():
    payload = _tasks_json([_sample_task(1), _sample_task(2)])
    parsed = parse_tasks_response(f"```json\n{payload}\n```")
    assert len(parsed) == 2
    assert parsed[0]["id"] == 1


def test_detect_sonnet_model_prefers_claude_sonnet():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer", description="", variants=[]),
        SDKModel(
            id="claude-sonnet-4",
            display_name="Claude Sonnet 4",
            description="",
            variants=[],
        ),
    ]
    selection = detect_sonnet_model(models)
    assert selection is not None
    assert selection.id == "claude-sonnet-4"


def test_resolve_parse_model_explicit_id():
    selection = resolve_parse_model("my-custom-model")
    assert selection == ModelSelection(id="my-custom-model")


def test_resolve_parse_model_auto_uses_composer_when_listed():
    models = [
        SDKModel(
            id="composer-2.5",
            display_name="Composer 2.5",
            description="",
            variants=[],
        ),
        SDKModel(
            id="claude-sonnet-4",
            display_name="Claude Sonnet",
            description="",
            variants=[],
        ),
    ]

    def list_models(**_kwargs: object) -> list[SDKModel]:
        return models

    selection = resolve_parse_model("auto", list_models=list_models)
    assert selection.id == "composer-2.5"


def test_resolve_parse_model_auto_falls_back_without_composer():
    models = [
        SDKModel(
            id="claude-sonnet-4",
            display_name="Claude Sonnet",
            description="",
            variants=[],
        )
    ]

    def list_models(**_kwargs: object) -> list[SDKModel]:
        return models

    selection = resolve_parse_model("auto", list_models=list_models)
    assert selection.id == "composer-2.5"


def test_remap_tasks_for_append_offsets_ids_and_dependencies():
    existing = [_sample_task(1), _sample_task(2)]
    new_tasks = [_sample_task(1), {**_sample_task(2), "dependencies": ["1"]}]
    merged = remap_tasks_for_append(existing, new_tasks)
    assert [task["id"] for task in merged] == [1, 2, 3, 4]
    assert merged[3]["dependencies"] == ["3"]


def test_parse_prd_with_cursor_persists_tasks_and_last_parsed(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    payload = _tasks_json([_sample_task(1), _sample_task(2)])
    fake_create, fake_send, agents, _prompts = _make_sdk_mocks([payload])

    parse_prd_with_cursor(
        project_root,
        project_root / "prd.md",
        tag="master",
        config=ParsePrdConfig(parse_model="claude-sonnet-4"),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    store = TaskStore(project_root, backend="native")
    tasks = store.load_tag_tasks("master")
    assert len(tasks) == 2
    assert tasks[0]["title"] == "Task 1"

    last_parsed = load_last_parsed_prd(project_root)
    assert last_parsed is not None
    assert last_parsed.tag == "master"
    assert last_parsed.path == "prd.md"
    assert (project_root / LAST_PARSED_PRD_REL).is_file()
    assert len(agents) == 1
    assert agents[0].closed is True


def test_parse_prd_with_cursor_append_merges_tasks(project_root: Path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1)], tag="master", merge=True)

    payload = _tasks_json([_sample_task(1)])
    fake_create, fake_send, _agents, _prompts = _make_sdk_mocks([payload])

    parse_prd_with_cursor(
        project_root,
        project_root / "prd.md",
        tag="master",
        append=True,
        config=ParsePrdConfig(parse_model="claude-sonnet-4"),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    tasks = store.load_tag_tasks("master")
    assert len(tasks) == 2
    assert tasks[1]["id"] == 2


def test_parse_prd_retries_once_on_invalid_json(project_root: Path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    bad = "not json at all"
    good = _tasks_json([_sample_task(1)])
    fake_create, fake_send, agents, prompts = _make_sdk_mocks([bad, good])

    parse_prd_with_cursor(
        project_root,
        project_root / "prd.md",
        config=ParsePrdConfig(parse_model="claude-sonnet-4"),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    assert len(agents) == 2
    assert "Validation error" in prompts[1] or "repair" in prompts[1].lower()
    store = TaskStore(project_root, backend="native")
    assert len(store.load_tag_tasks("master")) == 1


def test_parse_prd_fails_after_second_invalid_response(project_root: Path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    fake_create, fake_send, _agents, _prompts = _make_sdk_mocks(["still bad", "also bad"])

    with pytest.raises(ParsePrdError, match="repair retry"):
        parse_prd_with_cursor(
            project_root,
            project_root / "prd.md",
            config=ParsePrdConfig(parse_model="claude-sonnet-4"),
            create_agent=fake_create,
            send_fn=fake_send,
        )

    assert not (project_root / LAST_PARSED_PRD_REL).is_file()


def test_parse_prd_does_not_write_last_parsed_on_validation_failure(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    invalid = json.dumps({"tasks": [{**_sample_task(1), "id": 5}]})
    fake_create, fake_send, _agents, _prompts = _make_sdk_mocks([invalid, invalid])

    with pytest.raises(ParsePrdError):
        parse_prd_with_cursor(
            project_root,
            project_root / "prd.md",
            config=ParsePrdConfig(parse_model="claude-sonnet-4"),
            create_agent=fake_create,
            send_fn=fake_send,
        )

    assert not (project_root / LAST_PARSED_PRD_REL).is_file()


def test_native_backend_parse_prd_delegates(project_root: Path, monkeypatch):
    captured: dict[str, object] = {"invoked": False}

    def stub_parse(
        root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        **_kwargs: object,
    ) -> None:
        captured["invoked"] = True
        captured["tag"] = tag
        captured["append"] = append
        ensure_native_layout(root)
        TaskStore(root, backend="native").save_tag_tasks(
            [_sample_task(1)],
            tag=tag or "master",
            merge=True,
        )

    monkeypatch.setattr(
        "cyclopsctl.tasks.parse_prd.parse_prd_with_cursor",
        stub_parse,
    )

    backend = NativeTaskBackend()
    backend.parse_prd(
        project_root,
        project_root / "prd.md",
        tag="master",
        append=True,
    )

    assert captured["invoked"] is True
    assert captured["tag"] == "master"
    assert captured["append"] is True
    store = TaskStore(project_root, backend="native")
    assert len(store.load_tag_tasks("master")) == 1
