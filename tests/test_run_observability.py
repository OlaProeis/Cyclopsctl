"""Integration tests for run observability: dry-run, git summary, transcript export (task 16)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from cursor_sdk import ModelSelection

from cyclopsctl.config import build_run_config, load_run_config
from cyclopsctl.git_summary import capture_cycle_git_diff_summary, capture_git_head
from cyclopsctl.logging import CycleLogger
from cyclopsctl.loop import CycleOutcome, run_cycles
from cyclopsctl.models import COMPOSER_MODEL_ID, ModelCapabilities
from cyclopsctl.routing import ComplexityReport, ModelRouter
from cyclopsctl.session import CycleSession
from cyclopsctl.tasks.types import NextTaskLookup, NextTaskResult
from cyclopsctl.tui import format_plain_summary
from tests.test_loop import _FakeAgent, _make_session_factory


@pytest.fixture(autouse=True)
def _reset_agent_counter():
    _FakeAgent._counter = 0
    yield
    _FakeAgent._counter = 0


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompts").mkdir()
    (root / ".cyclopsctl" / "reports").mkdir(parents=True)
    (root / "prompts" / "first.md").write_text("# First-run prompt\n", encoding="utf-8")
    (root / "current-handover-prompt.md").write_text(
        "# Task ID: 8\n\nImplement task 8.\n",
        encoding="utf-8",
    )
    (root / "update-handover-prompt.md").write_text(
        "# Update\n\nMark done and advance handover.\n",
        encoding="utf-8",
    )
    (root / ".cyclopsctl" / "reports" / "complexity-report.json").write_text(
        '{"complexityAnalysis": [{"taskId": 8, "complexityScore": 8}]}',
        encoding="utf-8",
    )
    from tests.conftest import seed_native_tasks
    seed_native_tasks(root)
    return root.resolve()


def _config(project_tree: Path, *, cycles: int = 1, **extra):
    return build_run_config(
        {
            "cycles": cycles,
            "project_root": project_tree,
            "first_prompt": project_tree / "prompts" / "first.md",
            "current_handover": project_tree / "current-handover-prompt.md",
            "update_handover": project_tree / "update-handover-prompt.md",
            "task_source": "handover",
            **extra,
        }
    )


def _router() -> ModelRouter:
    return ModelRouter(
        default_model=COMPOSER_MODEL_ID,
        complexity_report=ComplexityReport(scores={8: 8}),
        capabilities=ModelCapabilities(
            composer=ModelSelection(id=COMPOSER_MODEL_ID),
            composer_standard=ModelSelection(id=COMPOSER_MODEL_ID),
            opus_available=False,
            opus=None,
        ),
    )


def _next_task(task_id: int = 8) -> NextTaskLookup:
    return NextTaskLookup.from_task(
        NextTaskResult(
            task_id=str(task_id),
            title=f"Task {task_id}",
            status="pending",
            priority="high",
            complexity=8,
            tag="master",
        )
    )


def _advance_handover(project_tree: Path, task_id: int) -> None:
    (project_tree / "current-handover-prompt.md").write_text(
        f"# Task ID: {task_id + 1}\n\nNext task.\n",
        encoding="utf-8",
    )


def test_dry_run_resolves_task_without_agent(project_tree: Path):
    cfg = _config(project_tree, dry_run=True)
    agent_created = {"value": False}

    class _TrackSession(CycleSession):
        def start_implementation(self, prompt: str):
            agent_created["value"] = True
            return super().start_implementation(prompt)

    def session_factory(**kwargs) -> CycleSession:
        session = _make_session_factory(project_tree)(**kwargs)

        def tracked_start(prompt: str):
            agent_created["value"] = True
            return CycleSession.start_implementation(session, prompt)

        session.start_implementation = tracked_start  # type: ignore[method-assign]
        return session

    result = run_cycles(
        cfg,
        router=_router(),
        get_next_task_fn=lambda *_a, **_k: _next_task(),
        session_factory=session_factory,
    )

    assert result.dry_run is True
    assert result.completed_cycles == 1
    assert agent_created["value"] is False
    assert len(result.outcomes) == 1
    assert result.outcomes[0].verification_result == "dry-run"
    assert result.outcomes[0].agent_id == ""


def test_dry_run_logs_dry_run_plan_event(project_tree: Path, tmp_path: Path):
    jsonl_path = tmp_path / "cycles.jsonl"
    logger = CycleLogger(jsonl_path=jsonl_path)
    cfg = _config(project_tree, dry_run=True)

    run_cycles(
        cfg,
        router=_router(),
        get_next_task_fn=lambda *_a, **_k: _next_task(),
        session_factory=_make_session_factory(project_tree),
        cycle_logger=logger,
    )

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "dry_run_plan" in events
    assert "cycle_complete" in events
    complete = json.loads(lines[-1])
    assert complete["verification_result"] == "dry-run"


def test_git_summary_captured_with_mocked_subprocess(project_tree: Path, tmp_path: Path):
    jsonl_path = tmp_path / "cycles.jsonl"
    logger = CycleLogger(jsonl_path=jsonl_path)
    cfg = _config(project_tree, git_summary=True)

    with patch("cyclopsctl.loop.capture_git_head", return_value="abc123"):
        with patch(
            "cyclopsctl.loop.capture_cycle_git_diff_summary",
            return_value=" src/foo.py | 2 +-\n 1 file changed",
        ):
            result = run_cycles(
                cfg,
                router=_router(),
                get_next_task_fn=lambda *_a, **_k: _next_task(),
                session_factory=_make_session_factory(
                    project_tree,
                    on_update=lambda _prompt: _advance_handover(project_tree, 8),
                ),
                cycle_logger=logger,
            )

    assert result.outcomes[0].git_diff_summary is not None
    assert "foo.py" in result.outcomes[0].git_diff_summary
    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    complete = json.loads(lines[-1])
    assert complete.get("git_diff_summary") is not None


def test_git_summary_skips_gracefully_without_git(project_tree: Path):
    cfg = _config(project_tree, git_summary=True, dry_run=True)

    with patch("cyclopsctl.loop.capture_git_head", return_value=None):
        result = run_cycles(
            cfg,
            router=_router(),
            get_next_task_fn=lambda *_a, **_k: _next_task(),
            session_factory=_make_session_factory(project_tree),
        )

    assert result.outcomes[0].git_diff_summary is None


def test_export_transcript_dir_writes_sidecar(project_tree: Path, tmp_path: Path):
    export_dir = tmp_path / "transcripts"
    cfg = _config(project_tree, export_transcript_dir=export_dir)

    run_cycles(
        cfg,
        router=_router(),
        get_next_task_fn=lambda *_a, **_k: _next_task(),
        session_factory=_make_session_factory(
            project_tree,
            on_update=lambda _prompt: _advance_handover(project_tree, 8),
        ),
    )

    sidecar_path = export_dir / "cycle-1.json"
    assert sidecar_path.is_file()
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["cycle"] == 1
    assert payload["task_id"] == 8
    assert payload["model"] == COMPOSER_MODEL_ID
    assert payload["status"] == "passed"
    assert payload["agent_id"]
    assert payload["run_id"]


def test_dry_run_skips_transcript_export(project_tree: Path, tmp_path: Path):
    export_dir = tmp_path / "transcripts"
    cfg = _config(project_tree, dry_run=True, export_transcript_dir=export_dir)

    run_cycles(
        cfg,
        router=_router(),
        get_next_task_fn=lambda *_a, **_k: _next_task(),
        session_factory=_make_session_factory(project_tree),
    )

    assert not export_dir.exists() or not list(export_dir.glob("*.json"))


def test_post_run_summary_includes_run_ids_and_git_summary():
    outcomes = [
        CycleOutcome(
            cycle_number=1,
            task_id=8,
            task_title="Observability",
            model_id=COMPOSER_MODEL_ID,
            agent_id="agent-1",
            impl_run_id="run-impl-1",
            update_run_id="run-update-1",
            verification_result="passed",
            duration_seconds=12.0,
            git_diff_summary=" src/foo.py | 1 +\n 1 file changed, 1 insertion(+)",
        )
    ]
    text = format_plain_summary(outcomes)
    assert "Agent ID" in text
    assert "Run ID" in text
    assert "Git changes" in text
    assert "agent-1" in text
    assert "run-impl-1" in text


def test_capture_git_head_returns_none_on_failure(project_tree: Path):
    def fail_run(*_args, **_kwargs):
        raise OSError("git missing")

    assert capture_git_head(project_tree, run_fn=fail_run) is None


def test_capture_cycle_git_diff_summary_returns_none_without_repo(project_tree: Path):
    def fail_run(*_args, **_kwargs):
        raise OSError("git missing")

    assert capture_cycle_git_diff_summary(project_tree, None, run_fn=fail_run) is None


def test_config_loads_run_section_from_toml(project_tree: Path, tmp_path: Path):
    toml_path = tmp_path / "cyclopsctl.toml"
    toml_path.write_text(
        f"""
project_root = "{project_tree.as_posix()}"
cycles = 1
current_handover = "current-handover-prompt.md"
update_handover = "update-handover-prompt.md"

[run]
git_summary = true
export_transcript_dir = ".cyclopsctl/transcripts"
""",
        encoding="utf-8",
    )

    cfg = load_run_config(config_path=toml_path, project_root=project_tree)
    assert cfg.git_summary is True
    assert cfg.export_transcript_dir == (project_tree / ".cyclopsctl/transcripts").resolve()
