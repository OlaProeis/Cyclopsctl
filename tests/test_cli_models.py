"""Tests for task 11: cyclopsctl models diagnostic subcommand."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cursor_sdk import ModelParameterValue, ModelVariant, SDKModel

from cyclopsctl.cli import main, run_models_inspection
from cyclopsctl.models import fetch_model_inventory, format_models_diagnostic


def _sample_models() -> list[SDKModel]:
    return [
        SDKModel(
            id="claude-opus-4-8-thinking-high",
            display_name="Opus 4.8 High Thinking",
            variants=(
                ModelVariant(
                    display_name="High thinking",
                    params=(ModelParameterValue(id="reasoning", value="high"),),
                ),
            ),
        ),
        SDKModel(id="composer-2.5", display_name="Composer 2.5"),
    ]


def test_run_models_inspection_prints_diagnostic(capsys):
    code = run_models_inspection(list_models=lambda **_kw: _sample_models())
    captured = capsys.readouterr()

    assert code == 0
    assert "Cursor model inventory (2 models)" in captured.out
    assert "Opus route available: yes" in captured.out
    assert captured.err == ""


def test_run_models_inspection_does_not_touch_task_queue():
    with patch("cyclopsctl.tasks.cli.get_next_task") as mock_next:
        code = run_models_inspection(list_models=lambda **_kw: _sample_models())

    assert code == 0
    mock_next.assert_not_called()


def test_cli_models_subcommand_delegates_to_inspection(capsys):
    with patch(
        "cyclopsctl.cli.fetch_model_inventory",
        return_value=fetch_model_inventory(list_models=lambda **_kw: _sample_models()),
    ):
        code = main(["models"])

    captured = capsys.readouterr()
    assert code == 0
    assert "composer-2.5" in captured.out


def test_cli_models_reports_listing_failure(capsys):
    from cyclopsctl.models import ModelListingError

    with patch(
        "cyclopsctl.cli.fetch_model_inventory",
        side_effect=ModelListingError("Cursor.models.list() failed: auth"),
    ):
        code = main(["models"])

    captured = capsys.readouterr()
    assert code == 1
    assert "cyclopsctl: error:" in captured.err
    assert "auth" in captured.err
