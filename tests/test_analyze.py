"""Tests for native complexity analysis via Cursor SDK (phase-5 task 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cursor_sdk import ModelSelection, RunResult, SDKModel

from cyclopsctl.routing import parse_complexity_payload
from cyclopsctl.tasks.analyze import (
    AnalyzeComplexityConfig,
    AnalyzeComplexityError,
    backfill_task_complexity,
    build_complexity_report,
    chunk_tasks,
    load_analyze_template,
    merge_analysis_chunks,
    parse_analysis_payload,
    parse_analysis_response,
    render_analyze_prompt,
    render_analyze_repair_prompt,
    resolve_analyze_model,
    should_skip_analyze,
    analyze_complexity_with_cursor,
)
from cyclopsctl.tasks.native_backend import NativeTaskBackend
from cyclopsctl.tasks.store import TaskStore, ensure_native_layout


def _sample_task(task_id: int, *, status: str = "pending") -> dict:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": f"Description {task_id}",
        "details": f"Details {task_id}",
        "testStrategy": "pytest",
        "priority": "high",
        "dependencies": [] if task_id == 1 else [str(task_id - 1)],
        "status": status,
    }


def _analysis_item(task_id: int, *, score: int = 5) -> dict:
    return {
        "taskId": task_id,
        "taskTitle": f"Task {task_id}",
        "complexityScore": score,
        "reasoning": f"Score {score} for task {task_id}.",
    }


def _analysis_json(items: list[dict]) -> str:
    return json.dumps({"complexityAnalysis": items})


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
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


def test_load_analyze_template_contains_placeholders():
    template = load_analyze_template()
    assert "{{TASKS_JSON}}" in template
    assert "complexityAnalysis" in template


def test_render_analyze_prompt_includes_task_metadata():
    tasks = [_sample_task(1), _sample_task(2)]
    prompt = render_analyze_prompt(tasks=tasks)
    assert '"id": 1' in prompt
    assert "Description 2" in prompt
    assert "Details 2" in prompt
    assert "{{TASKS_JSON}}" not in prompt


def test_render_analyze_repair_prompt_includes_error_and_response():
    repair = render_analyze_repair_prompt(
        invalid_response='{"broken": true}',
        error_message="missing complexityAnalysis array",
    )
    assert "missing complexityAnalysis array" in repair
    assert '{"broken": true}' in repair


def test_chunk_tasks_single_batch_when_small():
    tasks = [_sample_task(index) for index in range(1, 11)]
    batches = chunk_tasks(tasks, batch_threshold=15, chunk_size=15)
    assert batches == [tasks]


def test_chunk_tasks_splits_large_queue():
    tasks = [_sample_task(index) for index in range(1, 21)]
    batches = chunk_tasks(tasks, batch_threshold=15, chunk_size=10)
    assert len(batches) == 2
    assert len(batches[0]) == 10
    assert len(batches[1]) == 10


def test_parse_analysis_response_success():
    payload = _analysis_json([_analysis_item(1), _analysis_item(2, score=8)])
    parsed = parse_analysis_response(f"```json\n{payload}\n```")
    assert len(parsed) == 2
    assert parsed[0]["taskId"] == 1
    assert parsed[1]["complexityScore"] == 8


def test_parse_analysis_payload_rejects_invalid_score():
    items = [{**_analysis_item(1), "complexityScore": 11}]
    with pytest.raises(Exception, match="between 1 and 10"):
        parse_analysis_payload({"complexityAnalysis": items})


def test_merge_analysis_chunks_combines_batches():
    chunk_a = [_analysis_item(1), _analysis_item(2)]
    chunk_b = [_analysis_item(3)]
    merged = merge_analysis_chunks([chunk_a, chunk_b])
    assert [item["taskId"] for item in merged] == [1, 2, 3]


def test_build_complexity_report_schema():
    analysis = [_analysis_item(1), _analysis_item(2, score=7)]
    report = build_complexity_report(analysis)
    assert report["meta"]["tasksAnalyzed"] == 2
    assert report["meta"]["usedResearch"] is False
    assert "generatedAt" in report["meta"]
    assert len(report["complexityAnalysis"]) == 2
    scores = parse_complexity_payload(report)
    assert scores == {1: 5, 2: 7}


def test_backfill_task_complexity_updates_records():
    tasks = [_sample_task(1), _sample_task(2)]
    analysis = [_analysis_item(1, score=4), _analysis_item(2, score=9)]
    updated = backfill_task_complexity(tasks, analysis)
    assert updated[0]["complexity"] == 4
    assert updated[1]["complexity"] == 9


def test_should_skip_analyze_when_report_exists(tmp_path: Path):
    report = tmp_path / "complexity-report.json"
    report.write_text("{}", encoding="utf-8")
    assert should_skip_analyze(report, skip_analyze=False, skip_if_exists=True) is True


def test_should_skip_analyze_when_flag_set(tmp_path: Path):
    report = tmp_path / "missing.json"
    assert should_skip_analyze(report, skip_analyze=True, skip_if_exists=False) is True


def test_should_not_skip_when_forced(tmp_path: Path):
    report = tmp_path / "complexity-report.json"
    report.write_text("{}", encoding="utf-8")
    assert should_skip_analyze(report, skip_analyze=False, skip_if_exists=False) is False


def test_resolve_analyze_model_explicit_id():
    selection = resolve_analyze_model("my-analyze-model")
    assert selection == ModelSelection(id="my-analyze-model")


def test_resolve_analyze_model_auto_uses_composer_listing():
    models = [
        SDKModel(id="composer-2.5", display_name="Composer 2.5", description="", variants=[]),
    ]

    def list_models(**_kwargs: object) -> list[SDKModel]:
        return models

    selection = resolve_analyze_model("auto", list_models=list_models)
    assert selection.id == "composer-2.5"


def test_analyze_complexity_with_cursor_writes_report_and_backfills(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1), _sample_task(2)], tag="master", merge=True)

    payload = _analysis_json([_analysis_item(1, score=3), _analysis_item(2, score=8)])
    fake_create, fake_send, agents, _prompts = _make_sdk_mocks([payload])

    ran = analyze_complexity_with_cursor(
        project_root,
        tag="master",
        config=AnalyzeComplexityConfig(analyze_model="composer-2.5"),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    assert ran is True
    report = store.load_complexity_report()
    assert report["meta"]["tasksAnalyzed"] == 2
    assert parse_complexity_payload(report) == {1: 3, 2: 8}

    tasks = store.load_tag_tasks("master")
    assert tasks[0]["complexity"] == 3
    assert tasks[1]["complexity"] == 8
    assert len(agents) == 1
    assert agents[0].closed is True


def test_analyze_complexity_skips_when_report_exists(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1)], tag="master", merge=True)
    existing = build_complexity_report([_analysis_item(1, score=6)])
    store.save_complexity_report(existing)

    fake_create, fake_send, agents, _prompts = _make_sdk_mocks([])

    ran = analyze_complexity_with_cursor(
        project_root,
        tag="master",
        config=AnalyzeComplexityConfig(analyze_model="composer-2.5"),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    assert ran is False
    assert agents == []
    assert store.load_complexity_report() == existing


def test_analyze_complexity_skip_analyze_flag(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1)], tag="master", merge=True)

    fake_create, fake_send, agents, _prompts = _make_sdk_mocks(
        [_analysis_json([_analysis_item(1)])]
    )

    ran = analyze_complexity_with_cursor(
        project_root,
        tag="master",
        config=AnalyzeComplexityConfig(
            analyze_model="composer-2.5",
            skip_analyze=True,
            skip_if_exists=False,
        ),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    assert ran is False
    assert agents == []
    assert not store.complexity_report_path().is_file()


def test_analyze_complexity_chunks_large_queue(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    tasks = [_sample_task(index) for index in range(1, 17)]
    store.save_tag_tasks(tasks, tag="master", merge=True)

    chunk_one = _analysis_json([_analysis_item(index) for index in range(1, 11)])
    chunk_two = _analysis_json([_analysis_item(index) for index in range(11, 17)])
    fake_create, fake_send, agents, prompts = _make_sdk_mocks([chunk_one, chunk_two])

    ran = analyze_complexity_with_cursor(
        project_root,
        tag="master",
        config=AnalyzeComplexityConfig(
            analyze_model="composer-2.5",
            batch_threshold=15,
            chunk_size=10,
            backfill_complexity=False,
        ),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    assert ran is True
    assert len(agents) == 2
    assert len(prompts) == 2
    report = store.load_complexity_report()
    assert report["meta"]["tasksAnalyzed"] == 16
    assert len(report["complexityAnalysis"]) == 16


def test_analyze_complexity_retries_once_on_invalid_json(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1)], tag="master", merge=True)

    bad = "not json"
    good = _analysis_json([_analysis_item(1)])
    fake_create, fake_send, agents, prompts = _make_sdk_mocks([bad, good])

    analyze_complexity_with_cursor(
        project_root,
        tag="master",
        config=AnalyzeComplexityConfig(analyze_model="composer-2.5"),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    assert len(agents) == 2
    assert "Validation error" in prompts[1] or "repair" in prompts[1].lower()
    assert store.complexity_report_path().is_file()


def test_analyze_complexity_fails_after_second_invalid_response(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1)], tag="master", merge=True)

    fake_create, fake_send, _agents, _prompts = _make_sdk_mocks(["still bad", "also bad"])

    with pytest.raises(AnalyzeComplexityError, match="repair retry"):
        analyze_complexity_with_cursor(
            project_root,
            tag="master",
            config=AnalyzeComplexityConfig(analyze_model="composer-2.5"),
            create_agent=fake_create,
            send_fn=fake_send,
        )

    assert not store.complexity_report_path().is_file()


def test_analyze_complexity_no_backfill_when_disabled(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    ensure_native_layout(project_root)
    store = TaskStore(project_root, backend="native")
    store.save_tag_tasks([_sample_task(1)], tag="master", merge=True)

    payload = _analysis_json([_analysis_item(1, score=7)])
    fake_create, fake_send, _agents, _prompts = _make_sdk_mocks([payload])

    analyze_complexity_with_cursor(
        project_root,
        tag="master",
        config=AnalyzeComplexityConfig(
            analyze_model="composer-2.5",
            backfill_complexity=False,
        ),
        create_agent=fake_create,
        send_fn=fake_send,
    )

    tasks = store.load_tag_tasks("master")
    assert "complexity" not in tasks[0]


def test_native_backend_analyze_complexity_delegates(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {"invoked": False}

    def stub_analyze(
        root: Path,
        *,
        tag: str | None = None,
        **_kwargs: object,
    ) -> bool:
        captured["invoked"] = True
        captured["tag"] = tag
        ensure_native_layout(root)
        store = TaskStore(root, backend="native")
        store.save_complexity_report(build_complexity_report([_analysis_item(1)]))
        return True

    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        stub_analyze,
    )

    backend = NativeTaskBackend()
    backend.analyze_complexity(project_root, tag="phase-5")

    assert captured["invoked"] is True
    assert captured["tag"] == "phase-5"
    assert TaskStore(project_root, backend="native").complexity_report_path().is_file()
