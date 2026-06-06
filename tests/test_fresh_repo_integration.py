"""Integration tests for task 10: fresh-repo runbook and fixture-based init → launch."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from cyclopsctl.cli import main
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINIMAL_SMOKE_PRD = FIXTURES / "minimal-smoke-prd.md"
FRESH_REPO_RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "guides" / "testing-guide.md"


@pytest.fixture
def smoke_repo(tmp_path: Path) -> Path:
    root = tmp_path / "smoke-project"
    root.mkdir()
    shutil.copy(MINIMAL_SMOKE_PRD, root / "prd.md")
    (root / ".env").write_text("CURSOR_API_KEY=test-key\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def mocked_native_init(smoke_repo: Path, monkeypatch: pytest.MonkeyPatch):
    del smoke_repo

    def fake_parse(
        project_root: Path,
        prd: Path,
        *,
        tag: str | None = None,
        append: bool = False,
        **_kwargs: object,
    ) -> None:
        from cyclopsctl.tasks.store import save_tag_tasks

        tasks_path = project_root / ".cyclopsctl" / "tasks" / "tasks.json"
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        save_tag_tasks(
            tasks_path,
            [
                {
                    "id": task_id,
                    "title": f"Smoke task {task_id}",
                    "status": "pending",
                    "priority": "medium",
                    "dependencies": [],
                    "subtasks": [],
                }
                for task_id in (1, 2, 3)
            ],
            tag=tag or "master",
            merge=append,
        )

    def fake_analyze(project_root: Path, *, tag: str | None = None, **_kwargs: object) -> bool:
        report = project_root / ".cyclopsctl" / "reports" / "complexity-report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                {
                    "complexityAnalysis": [
                        {"taskId": 1, "complexityScore": 3},
                        {"taskId": 2, "complexityScore": 3},
                        {"taskId": 3, "complexityScore": 3},
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(
        "cyclopsctl.tasks.parse_prd.parse_prd_with_cursor",
        fake_parse,
    )
    monkeypatch.setattr(
        "cyclopsctl.tasks.analyze.analyze_complexity_with_cursor",
        fake_analyze,
    )
    yield


def test_minimal_smoke_prd_fixture_exists() -> None:
    text = MINIMAL_SMOKE_PRD.read_text(encoding="utf-8")
    assert "Cyclopsctl smoke test project" in text
    assert "test-run/output-1.txt" in text
    assert "python -m pytest" in text


def test_fresh_repo_runbook_documents_init_then_launch() -> None:
    text = FRESH_REPO_RUNBOOK.read_text(encoding="utf-8")
    assert "part a" in text.lower() or "fresh repository" in text.lower()
    init_pos = text.find("cyclopsctl init")
    launch_pos = text.find("cyclopsctl launch")
    assert init_pos != -1 and launch_pos != -1
    assert init_pos < launch_pos, "runbook should present init before launch"


def test_fresh_repo_runbook_default_path_uses_native_tasks() -> None:
    text = FRESH_REPO_RUNBOOK.read_text(encoding="utf-8")
    part_a_end = text.lower().find("part b")
    default_section = text if part_a_end == -1 else text[:part_a_end]
    assert "cyclopsctl tasks" in default_section.lower()


def test_fresh_repo_runbook_requires_only_cursor_api_key() -> None:
    text = FRESH_REPO_RUNBOOK.read_text(encoding="utf-8")
    part_a_end = text.lower().find("part b")
    default_section = text if part_a_end == -1 else text[:part_a_end]
    assert "CURSOR_API_KEY" in default_section
    assert "ANTHROPIC_API_KEY" not in default_section


def test_fresh_repo_init_launch_chain_mocked(
    smoke_repo: Path,
    mocked_native_init,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    dispatched: list[list[str]] = []

    def fake_run_command(args, parser):
        dispatched.append(
            [
                "run",
                "--cycles",
                str(args.cycles),
                "--project-root",
                str(args.project_root),
            ]
        )
        return 0

    with patch(
        "cyclopsctl.doctor.needs_windows_bridge_bootstrap",
        return_value=False,
    ), patch(
        "cyclopsctl.doctor.bridge_env_configured",
        return_value=True,
    ), patch(
        "cyclopsctl.cli._run_command",
        side_effect=fake_run_command,
    ):
        init_code = main(["init", "--project-root", str(smoke_repo)])
        assert init_code == 0

        launch_code = main(
            [
                "launch",
                "--project-root",
                str(smoke_repo),
                "--plain",
                "--yes",
                "--action",
                "run",
                "--cycles",
                "1",
            ]
        )
        assert launch_code == 0

    captured = capsys.readouterr()
    assert "cyclopsctl launch" in captured.err

    assert (smoke_repo / "cyclopsctl.toml").is_file()
    assert (smoke_repo / "ai-context.md").is_file()
    assert (smoke_repo / "current-handover-prompt.md").is_file()
    assert "# Task ID: 1" in (
        smoke_repo / "current-handover-prompt.md"
    ).read_text(encoding="utf-8")
    assert (
        smoke_repo / ".cyclopsctl" / "reports" / "complexity-report.json"
    ).is_file()

    assert len(dispatched) == 1
    assert dispatched[0][0] == "run"
    assert dispatched[0][2] == "1"
    assert str(smoke_repo) in dispatched[0]
