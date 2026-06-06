"""Content checks for user-facing README and technical brief."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
TECHNICAL_BRIEF = REPO_ROOT / "docs" / "guides" / "technical-brief.md"

# Modules present in src/cyclopsctl/ (Phase 3/4 layout).
EXPECTED_MODULES = {
    "cli.py",
    "config.py",
    "profiles.py",
    "init_scaffold.py",
    "env.py",
    "bootstrap.py",
    "project_setup.py",
    "workflow_gen.py",
    "task_selection.py",
    "models.py",
    "routing.py",
    "prompt.py",
    "preflight.py",
    "doctor.py",
    "launcher.py",
    "alignment.py",
    "verify.py",
    "runner.py",
    "session.py",
    "loop.py",
    "interrupt.py",
    "state.py",
    "history.py",
    "logging.py",
    "tui.py",
    "version.py",
    "installer.py",
}


def test_readme_leads_with_install_init_launch_workflow() -> None:
    text = README.read_text(encoding="utf-8")
    install_pos = text.lower().find("install")
    init_pos = text.find("cyclopsctl init")
    launch_pos = text.find("cyclopsctl launch")
    quick_start_pos = text.lower().find("quick start")

    assert quick_start_pos != -1, "README must include a Quick start section"
    assert install_pos != -1 and install_pos < quick_start_pos + 800
    assert init_pos != -1
    assert launch_pos != -1
    assert init_pos < launch_pos, "init should appear before launch in the primary workflow"


def test_readme_includes_scenario_sections() -> None:
    text = README.read_text(encoding="utf-8").lower()
    assert "new project" in text
    assert "existing project" in text or "existing repo" in text or "new prd" in text


def test_readme_links_testing_guide() -> None:
    text = README.read_text(encoding="utf-8")
    assert "docs/guides/testing-guide.md" in text


def test_readme_preserves_advanced_cli_reference() -> None:
    text = README.read_text(encoding="utf-8").lower()
    for command in ("bootstrap", "run", "doctor", "profile", "routing", "tag"):
        assert command in text, f"README must document {command} for power users"


def test_readme_install_paths_documented() -> None:
    text = README.read_text(encoding="utf-8").lower()
    assert "install.ps1" in text or "install.sh" in text
    assert "pip install" in text or "pypi" in text
    assert "pipx" in text or "git+" in text


def test_readme_default_happy_path_is_native_one_key() -> None:
    text = README.read_text(encoding="utf-8")
    quick_start_pos = text.lower().find("quick start")
    assert quick_start_pos != -1
    happy_path = text[quick_start_pos : quick_start_pos + 2500]
    assert "cursor_api_key" in happy_path.lower()
    assert "cyclopsctl init" in happy_path.lower()
    assert "node.js" not in happy_path.lower() or "not required" in happy_path.lower()


def test_readme_license_resolved() -> None:
    text = README.read_text(encoding="utf-8")
    assert "license tbd" not in text.lower()
    assert "mit" in text.lower()


def test_technical_brief_module_map_covers_phase_modules() -> None:
    text = TECHNICAL_BRIEF.read_text(encoding="utf-8")
    missing = [name for name in sorted(EXPECTED_MODULES) if name not in text]
    assert not missing, f"technical-brief missing modules: {missing}"


def test_technical_brief_describes_adoption_flow() -> None:
    text = TECHNICAL_BRIEF.read_text(encoding="utf-8").lower()
    assert "adoption" in text or "install" in text
    assert "cyclopsctl init" in text
    assert "cyclopsctl launch" in text or "cyclopsctl bootstrap" in text


def test_technical_brief_references_testing_approach() -> None:
    text = TECHNICAL_BRIEF.read_text(encoding="utf-8").lower()
    assert "pytest" in text or "python -m pytest" in text
    assert "testing-guide" in text
