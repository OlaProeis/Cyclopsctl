"""Tests for task 2: project-aware workflow file generation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cyclopsctl.bootstrap import (
    copy_workflow_templates,
    resolve_bootstrap_config,
    run_bootstrap,
)
from cyclopsctl.init_scaffold import run_init_scaffold
from cyclopsctl.workflow_gen import (
    CYCLOPSCTL_CURSOR_RULES_DIR,
    TEMPLATES_DIR,
    WorkflowGenConfig,
    detect_stale_workflow_files,
    generate_workflow_files,
    install_cyclopsctl_cursor_rules,
    is_generic_workflow_file,
    is_workflow_file_stale,
    cyclopsctl_cursor_rules_present,
    refresh_stale_workflow_files,
    resolve_workflow_inputs,
    workflow_file_staleness_reasons,
    _extract_tech_stack_from_prd,
    _extract_test_cmd_from_prd,
    _project_name_from_prd,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workflow_gen"


@pytest.fixture
def python_project(tmp_path: Path) -> Path:
    root = tmp_path / "python-api"
    root.mkdir()
    shutil.copy(FIXTURES / "python-api-prd.md", root / "prd.md")
    (root / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def node_project(tmp_path: Path) -> Path:
    root = tmp_path / "node-dashboard"
    root.mkdir()
    shutil.copy(FIXTURES / "node-app-prd.md", root / "prd.md")
    (root / "package.json").write_text('{"name":"dashboard"}', encoding="utf-8")
    return root.resolve()


@pytest.fixture
def python_readme_project(tmp_path: Path) -> Path:
    root = tmp_path / "python-readme-api"
    root.mkdir()
    shutil.copy(FIXTURES / "python-readme.md", root / "README.md")
    (root / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def node_readme_project(tmp_path: Path) -> Path:
    root = tmp_path / "node-readme-dashboard"
    root.mkdir()
    shutil.copy(FIXTURES / "node-readme.md", root / "README.md")
    (root / "package.json").write_text('{"name":"dashboard"}', encoding="utf-8")
    return root.resolve()


@pytest.fixture
def minimal_readme_project(tmp_path: Path) -> Path:
    root = tmp_path / "tiny-utility"
    root.mkdir()
    shutil.copy(FIXTURES / "minimal-readme.md", root / "README.md")
    return root.resolve()


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fixture_with_root(name: str, project_root: Path) -> str:
    return _read_fixture(name).replace("{ROOT}", str(project_root))


def test_project_name_from_prd_title():
    prd = "# PRD: Widget Builder\n\nBody."
    assert _project_name_from_prd(prd, Path("/tmp/widget")) == "Widget Builder"


def test_project_name_fallback_to_repo_name():
    prd = "No heading here."
    root = Path("/tmp/my-repo")
    assert _project_name_from_prd(prd, root) == "my-repo"


def test_extract_tech_stack_from_heading():
    prd = _read_fixture("python-api-prd.md")
    stack = _extract_tech_stack_from_prd(prd)
    assert stack is not None
    assert "Python 3.11+" in stack
    assert "FastAPI" in stack


def test_extract_tech_stack_from_technology_stack_heading():
    prd = _read_fixture("node-app-prd.md")
    stack = _extract_tech_stack_from_prd(prd)
    assert stack == "TypeScript, Node.js, React, Vite"


def test_extract_test_cmd_from_prd_fenced_block():
    prd = _read_fixture("python-api-prd.md")
    assert _extract_test_cmd_from_prd(prd) == "python -m pytest"


def test_extract_test_cmd_from_node_prd():
    prd = _read_fixture("node-app-prd.md")
    assert _extract_test_cmd_from_prd(prd) == "npm test"


def test_resolve_workflow_inputs_prefers_repo_signals(python_project: Path):
    inputs = resolve_workflow_inputs(
        project_root=python_project,
        prd_path=python_project / "prd.md",
    )
    assert inputs.project_name == "Python API Service"
    assert "FastAPI" in inputs.tech_stack
    assert inputs.test_cmd == "python -m pytest"


def test_resolve_workflow_inputs_detects_npm_test(node_project: Path):
    inputs = resolve_workflow_inputs(
        project_root=node_project,
        prd_path=node_project / "prd.md",
    )
    assert inputs.project_name == "Node Dashboard"
    assert inputs.test_cmd == "npm test"


def test_resolve_workflow_inputs_from_readme_python(python_readme_project: Path):
    inputs = resolve_workflow_inputs(
        project_root=python_readme_project,
        prd_path=None,
    )
    assert inputs.context_source == "readme"
    assert inputs.project_name == "Python API Service"
    assert "FastAPI" in inputs.tech_stack
    assert "See prd.md" not in inputs.tech_stack
    assert inputs.test_cmd == "python -m pytest"


def test_resolve_workflow_inputs_from_readme_node(node_readme_project: Path):
    inputs = resolve_workflow_inputs(
        project_root=node_readme_project,
        prd_path=None,
    )
    assert inputs.context_source == "readme"
    assert inputs.project_name == "Node Dashboard"
    assert inputs.tech_stack == "TypeScript, Node.js, React, Vite"
    assert inputs.test_cmd == "npm test"


def test_resolve_workflow_inputs_minimal_readme_falls_back_to_repo(
    minimal_readme_project: Path,
):
    inputs = resolve_workflow_inputs(
        project_root=minimal_readme_project,
        prd_path=None,
    )
    assert inputs.context_source == "readme"
    assert inputs.project_name == "Tiny Utility"
    assert inputs.tech_stack == ""
    assert inputs.test_cmd == "python -m pytest"


def test_resolve_workflow_inputs_missing_readme_uses_repo_signals(tmp_path: Path):
    root = tmp_path / "repo-only"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='svc'\n", encoding="utf-8")

    inputs = resolve_workflow_inputs(project_root=root, prd_path=None)

    assert inputs.context_source == "repo"
    assert inputs.project_name == "repo-only"
    assert inputs.tech_stack == "Python"
    assert inputs.test_cmd == "python -m pytest"
    assert "See prd.md" not in inputs.tech_stack


def test_resolve_workflow_inputs_prefers_prd_over_readme(python_readme_project: Path):
    prd = python_readme_project / "prd.md"
    shutil.copy(FIXTURES / "python-api-prd.md", prd)

    inputs = resolve_workflow_inputs(
        project_root=python_readme_project,
        prd_path=prd,
    )

    assert inputs.context_source == "prd"
    assert inputs.project_name == "Python API Service"
    assert "pytest for testing" in inputs.tech_stack


def test_generate_workflow_files_from_readme_golden_ai_context(
    python_readme_project: Path,
):
    result = generate_workflow_files(
        WorkflowGenConfig(project_root=python_readme_project, prd_path=None)
    )

    generated = (python_readme_project / "ai-context.md").read_text(encoding="utf-8")
    expected = _fixture_with_root(
        "expected/readme-python-ai-context.md",
        python_readme_project,
    )
    assert generated == expected
    assert "See prd.md" not in generated.split("## Tech Stack", 1)[1].split("##", 1)[0]
    assert "ai-context.md" in result.generated_paths


def test_generate_workflow_files_golden_python_ai_context(python_project: Path):
    config = WorkflowGenConfig(project_root=python_project)
    result = generate_workflow_files(config)

    generated = (python_project / "ai-context.md").read_text(encoding="utf-8")
    expected = _fixture_with_root("expected/python-ai-context.md", python_project)
    assert generated == expected
    assert "cyclopsctl tasks" in generated
    assert "CLI only" in generated
    assert "ai-context.md" in result.generated_paths
    assert "update-handover-prompt.md" in result.generated_paths
    assert "docs/index.md" in result.generated_paths


def test_generate_workflow_files_golden_update_handover(python_project: Path):
    generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    generated = (python_project / "update-handover-prompt.md").read_text(encoding="utf-8")
    expected = _fixture_with_root(
        "expected/python-update-handover-prompt.md",
        python_project,
    )
    assert generated == expected
    assert "cyclopsctl tasks set-status" in generated


def test_generate_workflow_files_installs_cyclopsctl_cursor_rules(
    python_project: Path,
):
    result = generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    rule_path = (
        python_project / CYCLOPSCTL_CURSOR_RULES_DIR / "agent-workflow.mdc"
    )
    assert rule_path.is_file()
    generated = rule_path.read_text(encoding="utf-8")
    expected = _fixture_with_root("expected/agent-workflow.mdc", python_project)
    assert generated == expected
    assert "cyclopsctl tasks set-status" in generated
    assert cyclopsctl_cursor_rules_present(python_project)
    assert result.installed_rule_paths == (
        f"{CYCLOPSCTL_CURSOR_RULES_DIR.as_posix()}/agent-workflow.mdc",
    )


def test_install_cyclopsctl_cursor_rules_is_idempotent(python_project: Path):
    first = install_cyclopsctl_cursor_rules(
        python_project,
        project_root_value=str(python_project),
    )
    second = install_cyclopsctl_cursor_rules(
        python_project,
        project_root_value=str(python_project),
    )
    assert first == (f"{CYCLOPSCTL_CURSOR_RULES_DIR.as_posix()}/agent-workflow.mdc",)
    assert second == ()


def test_generate_workflow_files_includes_project_root_in_update_handover(
    python_project: Path,
):
    generate_workflow_files(WorkflowGenConfig(project_root=python_project))
    update = (python_project / "update-handover-prompt.md").read_text(encoding="utf-8")
    root_display = str(python_project)
    assert root_display in update
    assert f"--project-root {root_display}" in update
    assert "python -m pytest" in update


def test_generate_workflow_files_docs_index_only_when_absent(python_project: Path):
    docs_index = python_project / "docs" / "index.md"
    docs_index.parent.mkdir(parents=True)
    docs_index.write_text("# Custom Index\n", encoding="utf-8")

    result = generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    assert docs_index.read_text(encoding="utf-8") == "# Custom Index\n"
    assert "docs/index.md" in result.skipped_paths
    assert "ai-context.md" in result.generated_paths


def test_generate_workflow_files_skips_customized_ai_context(python_project: Path):
    ai_context = python_project / "ai-context.md"
    ai_context.write_text("# My Custom Context\n\nProject-specific notes.\n", encoding="utf-8")

    result = generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    assert ai_context.read_text(encoding="utf-8").startswith("# My Custom Context")
    assert "ai-context.md" in result.skipped_paths


def test_is_generic_workflow_file_detects_bundled_stub(python_project: Path):
    copy_workflow_templates(python_project)
    ai_context = python_project / "ai-context.md"
    assert is_generic_workflow_file(ai_context, relative_path="ai-context.md") is True


def test_generate_workflow_files_overwrites_generic_stub(python_project: Path):
    copy_workflow_templates(python_project)
    assert "Run your project test command" in (
        python_project / "ai-context.md"
    ).read_text(encoding="utf-8")

    result = generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    generated = (python_project / "ai-context.md").read_text(encoding="utf-8")
    assert "Python API Service - AI Context" in generated
    assert "Run `python -m pytest`" in generated
    assert "ai-context.md" in result.generated_paths


def test_generate_workflow_files_force_workflow_overwrites_customized(
    python_project: Path,
):
    ai_context = python_project / "ai-context.md"
    ai_context.write_text("# Custom\n", encoding="utf-8")

    generate_workflow_files(
        WorkflowGenConfig(project_root=python_project, force_workflow=True)
    )

    assert "Python API Service - AI Context" in ai_context.read_text(encoding="utf-8")


def test_generate_workflow_files_per_file_force(python_project: Path):
    ai_context = python_project / "ai-context.md"
    update = python_project / "update-handover-prompt.md"
    ai_context.write_text("# Custom AI\n", encoding="utf-8")
    update.write_text("# Custom Update\n", encoding="utf-8")

    result = generate_workflow_files(
        WorkflowGenConfig(
            project_root=python_project,
            force_paths=frozenset({"ai-context.md"}),
        )
    )

    assert "Python API Service - AI Context" in ai_context.read_text(encoding="utf-8")
    assert update.read_text(encoding="utf-8") == "# Custom Update\n"
    assert result.generated_paths == ("ai-context.md", "docs/index.md")
    assert "update-handover-prompt.md" in result.skipped_paths


def test_generate_workflow_files_never_writes_current_handover(python_project: Path):
    handover = python_project / "current-handover-prompt.md"
    handover.write_text("# Task ID: 5\n", encoding="utf-8")

    generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    assert handover.read_text(encoding="utf-8") == "# Task ID: 5\n"


def test_init_scaffold_generic_files_are_regenerated_by_workflow_gen(
    python_project: Path,
):
    run_init_scaffold(project_root=python_project)
    assert is_generic_workflow_file(
        python_project / "ai-context.md",
        relative_path="ai-context.md",
    )

    generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    assert "Python API Service - AI Context" in (
        python_project / "ai-context.md"
    ).read_text(encoding="utf-8")


def test_run_bootstrap_with_workflow_generates_project_aware_files(
    python_project: Path,
):
    from cyclopsctl.tasks.store import save_tag_tasks

    tasks_path = python_project / ".cyclopsctl" / "tasks" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    save_tag_tasks(
        tasks_path,
        [
            {
                "id": 1,
                "title": "Workflow task",
                "status": "pending",
                "priority": "high",
                "dependencies": [],
                "subtasks": [],
            }
        ],
        tag="master",
        merge=False,
    )

    config = resolve_bootstrap_config(
        project_root=python_project,
        sync_handover_only=True,
        with_workflow=True,
    )
    result = run_bootstrap(config)

    ai_context = (python_project / "ai-context.md").read_text(encoding="utf-8")
    assert "Python API Service - AI Context" in ai_context
    assert result.copied_templates == (
        "ai-context.md",
        "update-handover-prompt.md",
        "docs/index.md",
    )


def test_templates_dir_has_workflow_templates():
    assert (TEMPLATES_DIR / "workflow-ai-context.md").is_file()
    assert (TEMPLATES_DIR / "workflow-update-handover-prompt.md").is_file()
    assert (TEMPLATES_DIR / "cursor-rules/cyclopsctl/agent-workflow.mdc").is_file()


def test_workflow_file_staleness_detects_missing_native_ai_context_markers():
    content = "# Custom AI Context\n\nUse your own workflow.\n"
    reasons = workflow_file_staleness_reasons(content, "ai-context.md")
    assert "missing cyclopsctl tasks CLI references" in reasons
    assert "missing Implementation Phase Rules" in reasons
    assert "missing Update Phase Rules" in reasons


def test_workflow_file_staleness_detects_missing_native_update_handover_markers():
    content = "# Update\n\nMark tasks done manually.\n"
    reasons = workflow_file_staleness_reasons(content, "update-handover-prompt.md")
    assert "missing cyclopsctl tasks CLI references" in reasons
    assert "missing cyclopsctl tasks set-status command" in reasons


def test_workflow_file_staleness_accepts_native_workflow_files(python_project: Path):
    generate_workflow_files(WorkflowGenConfig(project_root=python_project))

    ai_context = (python_project / "ai-context.md").read_text(encoding="utf-8")
    update = (python_project / "update-handover-prompt.md").read_text(encoding="utf-8")

    assert is_workflow_file_stale(ai_context, "ai-context.md") is False
    assert is_workflow_file_stale(update, "update-handover-prompt.md") is False


def test_detect_stale_workflow_files_reports_only_stale_paths(python_project: Path):
    generate_workflow_files(WorkflowGenConfig(project_root=python_project))
    (python_project / "update-handover-prompt.md").write_text(
        "Mark tasks done manually.\n",
        encoding="utf-8",
    )

    stale = detect_stale_workflow_files(python_project)
    assert len(stale) == 1
    assert stale[0].relative_path == "update-handover-prompt.md"
    assert "missing cyclopsctl tasks set-status command" in stale[0].reasons


def test_refresh_stale_workflow_files_updates_only_stale_paths(python_project: Path):
    generate_workflow_files(WorkflowGenConfig(project_root=python_project))
    ai_context = python_project / "ai-context.md"
    update = python_project / "update-handover-prompt.md"
    update.write_text("Mark tasks done manually.\n", encoding="utf-8")
    original_ai = ai_context.read_text(encoding="utf-8")

    result = refresh_stale_workflow_files(WorkflowGenConfig(project_root=python_project))

    refreshed_update = update.read_text(encoding="utf-8")
    assert "cyclopsctl tasks set-status" in refreshed_update
    assert ai_context.read_text(encoding="utf-8") == original_ai
    assert result.updated_paths == ("update-handover-prompt.md",)
    assert "ai-context.md" in result.skipped_paths


def test_refresh_stale_workflow_files_preserves_customized_non_stale_files(
    python_project: Path,
):
    custom_ai = "# My Custom Context\n\nProject-specific cyclopsctl tasks notes.\n"
    custom_ai += "## Implementation Phase Rules\n## Update Phase Rules\n"
    custom_ai += "Use cyclopsctl tasks set-status when needed.\n"
    (python_project / "ai-context.md").write_text(custom_ai, encoding="utf-8")
    (python_project / "update-handover-prompt.md").write_text(
        "# Custom Update\n\ncyclopsctl tasks set-status --id=1 --status=done\n",
        encoding="utf-8",
    )

    result = refresh_stale_workflow_files(WorkflowGenConfig(project_root=python_project))

    assert result.updated_paths == ()
    assert "ai-context.md" in result.skipped_paths
    assert "update-handover-prompt.md" in result.skipped_paths
    assert (python_project / "ai-context.md").read_text(encoding="utf-8") == custom_ai


def test_refresh_stale_workflow_files_overwrites_stale_customized_content(
    python_project: Path,
):
    stale_ai = "# Legacy Context\n\nCustom notes without cyclopsctl tasks references.\n"
    (python_project / "ai-context.md").write_text(stale_ai, encoding="utf-8")

    result = refresh_stale_workflow_files(WorkflowGenConfig(project_root=python_project))

    generated = (python_project / "ai-context.md").read_text(encoding="utf-8")
    assert "Python API Service - AI Context" in generated
    assert "cyclopsctl tasks" in generated
    assert result.updated_paths == ("ai-context.md",)
