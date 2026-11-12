"""The same selected menu rows must govern display and dispatch, with stable keys."""
from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from ayvu.composition.cli import project_cli_surface
from ayvu.composition.editions import get_edition
from ayvu.interfaces.cli import app as cli
from ayvu.interfaces.cli.guided import menu
from ayvu.preferences import AyvuConfig


REDUCED_ROWS = [
    ("1", "Translate a book"), ("2", "Generate preview"), ("4", "Settings"),
    ("6", "Show command help"), ("0", "Exit"),
]
FULL_ROWS = [
    ("1", "Translate a book"), ("2", "Generate preview"), ("3", "Open library"),
    ("4", "Settings"), ("5", "Glossaries"), ("6", "Show command help"), ("0", "Exit"),
]


@pytest.fixture
def menu_calls(monkeypatch):
    config = AyvuConfig.default()
    calls = []
    monkeypatch.setattr(cli.settings, "_load_existing_config_or_default", lambda: config)
    monkeypatch.setattr(cli, "open_job_recovery", lambda *_: object())
    monkeypatch.setattr(cli, "_print_processing_translation_states", lambda *_: ())
    monkeypatch.setattr(cli, "_offer_detected_translation_resume", lambda *_, **__: False)
    monkeypatch.setattr(menu, "_load_or_init_config", lambda: config)
    for name in ("translation", "preview", "library", "settings", "glossaries"):
        def execute(*args, action=name):
            assert args == (() if action == "glossaries" else (config,))
            calls.append(action)
        monkeypatch.setattr(menu, f"_run_guided_{name}", execute)
    return calls


@pytest.mark.parametrize("edition_id,rows", [
    ("lite", REDUCED_ROWS), ("free", FULL_ROWS), ("ai", FULL_ROWS),
    ("cli_free", FULL_ROWS), ("cli_pro", FULL_ROWS),
])
@pytest.mark.parametrize("key", ["1", "2", "3", "4", "5", "6", "0", "9"])
def test_mounted_menu_displays_and_dispatches_only_selected_rows(edition_id, rows, key, menu_calls):
    edition = get_edition(edition_id)
    surface = project_cli_surface(edition.features, execution_preset=edition.execution_preset,
                                  report_style=edition.report_style)
    application = cli.create_app(surface)
    result = CliRunner().invoke(application, [], input=f"{key}\n")
    assert result.exit_code == 0, result.output
    displayed = re.findall(r"│\s*(\d)\s*│\s*([^│]+?)\s*│", result.output)
    assert displayed == rows
    actions = {"1": "translation", "2": "preview", "3": "library", "4": "settings", "5": "glossaries"}
    selected = key in dict(rows)
    assert menu_calls == ([actions[key]] if selected and key in actions else [])
    assert ("Unknown option." in result.output) == (not selected)
    assert ("Canceled." in result.output) == (key == "0")
    assert ("Usage:" in result.output) == (key == "6" or not selected)


def test_repeated_apps_keep_their_surface_and_fresh_invocation_state(monkeypatch, menu_calls):
    def application(edition_id):
        edition = get_edition(edition_id)
        return cli.create_app(project_cli_surface(edition.features))
    reduced, complete, another = application("lite"), application("cli_free"), application("lite")
    runner = CliRunner()
    contexts = []
    original = menu._run_guided_main_flow

    def observe(ctx, **kwargs):
        contexts.append(ctx.obj)
        assert set(ctx.obj) == {"mode", "network_policy"}
        ctx.obj["invocation_marker"] = True
        return original(ctx, **kwargs)

    monkeypatch.setattr(menu, "_run_guided_main_flow", observe)
    for app, rows in ((reduced, REDUCED_ROWS), (complete, FULL_ROWS), (another, REDUCED_ROWS),
                      (reduced, REDUCED_ROWS)):
        result = runner.invoke(app, [], input="0\n")
        assert result.exit_code == 0, result.output
        assert re.findall(r"│\s*(\d)\s*│\s*([^│]+?)\s*│", result.output) == rows
    assert len({id(context) for context in contexts}) == 4
