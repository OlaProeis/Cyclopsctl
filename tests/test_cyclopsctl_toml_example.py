"""Repository tests for task 4: cyclopsctl.toml.example and configuration docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "cyclopsctl.toml.example"
CLI_CONFIGURATION_DOC = REPO_ROOT / "docs" / "setup" / "cli-configuration.md"
GITIGNORE = REPO_ROOT / ".gitignore"

REQUIRED_TOML_KEYS = (
    "project_root",
    "cycles",
    "first_prompt",
    "current_handover",
    "update_handover",
    "complexity_report",
    "tag",
    "default_model",
    "plain",
)


def test_orchestrator_toml_example_exists():
    assert EXAMPLE_CONFIG.is_file(), "cyclopsctl.toml.example must exist at repo root"


def test_orchestrator_toml_example_contains_required_keys():
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    for key in REQUIRED_TOML_KEYS:
        assert re.search(rf"^\s*{re.escape(key)}\s*=", text, re.MULTILINE), (
            f"cyclopsctl.toml.example must define {key!r}"
        )


def test_orchestrator_toml_example_has_explanatory_comments():
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert text.count("#") >= len(REQUIRED_TOML_KEYS)
    assert "CLI flags always override" in text or "CLI flags override" in text


def test_orchestrator_toml_example_uses_generic_placeholders():
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "/path/to/your/project" in text
    assert "G:/DEV" not in text
    assert "C:/Users" not in text
    assert "CURSOR_API_KEY" not in text


def test_cli_configuration_doc_describes_copy_and_run_workflow():
    text = CLI_CONFIGURATION_DOC.read_text(encoding="utf-8")
    assert "cyclopsctl.toml.example" in text
    assert "cyclopsctl.toml" in text
    assert "cyclopsctl run --config cyclopsctl.toml" in text
    assert "cp cyclopsctl.toml.example cyclopsctl.toml" in text


def test_gitignore_ignores_local_orchestrator_toml():
    text = GITIGNORE.read_text(encoding="utf-8")
    assert re.search(r"^cyclopsctl\.toml\s*$", text, re.MULTILINE)


def test_gitignore_ignores_local_dev_folders_at_repo_root():
    text = GITIGNORE.read_text(encoding="utf-8")
    assert re.search(r"^/\.taskmaster/\s*$", text, re.MULTILINE)
    assert re.search(r"^/\.cursor/mcp\.json\s*$", text, re.MULTILINE)
    assert re.search(r"^/\.cursor/commands/\s*$", text, re.MULTILINE)
    assert re.search(r"^/\.cursor/rules/\s*$", text, re.MULTILINE)


@pytest.mark.parametrize("key", REQUIRED_TOML_KEYS)
def test_cli_configuration_doc_mentions_toml_key(key: str):
    text = CLI_CONFIGURATION_DOC.read_text(encoding="utf-8")
    assert f"`{key}`" in text
