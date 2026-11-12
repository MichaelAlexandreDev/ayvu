"""Applicable roots select plans before acquisition; desktop presentation is a stub."""
from __future__ import annotations

from dataclasses import asdict, fields, FrozenInstanceError
import os
import subprocess
import sys

import pytest

from ayvu.composition.cli import cli_surface
from ayvu.composition.desktop import DesktopOverrides, DesktopSurface, desktop_surface, start
from ayvu.composition.editions import get_edition
from tests.composition.test_desktop_composition import STAGES, setup_factories


EDITION_IDS = ("lite", "free", "ai", "cli_free", "cli_pro")
CASE_IDS = ("lite", "free", "ai", "cli-free", "cli-pro")
BASE_ACTIONS = ("test-translator", "translate", "resume", "preview", "settings", "help", "exit")
EXTENDED_ACTIONS = ("inspect", "test-translator", "translate", "resume", "extract", "apply-review",
                    "preview", "library", "settings", "glossaries", "help", "exit")


@pytest.mark.parametrize("edition_id", EDITION_IDS, ids=CASE_IDS)
def test_applicable_root_and_effective_surface(edition_id, tmp_path):
    manifest = get_edition(edition_id)
    events = []
    factories, resources = setup_factories(events)
    overrides = DesktopOverrides(tmp_path, {}, lambda: 123, object())
    if "desktop" not in manifest.interfaces:
        plan = cli_surface(edition_id)
        assert plan.providers == ("libretranslate",) and plan.input_formats == ("epub",)
        assert plan.extraction_writers == ("markdown",) and plan.assistance_roles == ()
        with pytest.raises(ValueError, match="desktop interface"):
            start(factories, overrides, edition=edition_id)
        assert events == []
        return

    with pytest.raises(ValueError, match="CLI interface"):
        cli_surface(edition_id)
    expected = DesktopSurface(BASE_ACTIONS if edition_id == "lite" else EXTENDED_ACTIONS,
        ("libretranslate",), ("epub",), () if edition_id == "lite" else ("markdown",),
        () if edition_id == "lite" else ("markdown",), ())
    # This is an acquisition plan, never evidence of existing widgets.
    expectation = {"presentation_status": "stub", "surface": expected}
    result = start(factories, overrides, edition=edition_id)
    assert result.diagnostic is None and result.graph is not None
    assert desktop_surface(edition_id) == expectation["surface"]
    assert [event[1] for event in events] == list(STAGES)
    assert events[2] == ("enter", "provider", overrides, resources["configuration"], expected)
    assert events[4] == ("enter", "application", overrides, resources["configuration"],
                         resources["storage"], resources["provider"], resources["jobs"], expected)
    assert events[2][-1] is events[4][-1]
    assert set(asdict(expected)) == {"actions", "providers", "input_formats", "extraction_writers",
                                   "report_writers", "assistance_roles"}
    result.graph.close()
    result.graph.close()
    assert events[5:] == [("exit", stage) for stage in reversed(STAGES)]


@pytest.mark.parametrize("edition_id", ("lite", "free", "ai"))
def test_selection_failure_precedes_even_configuration(edition_id, tmp_path, monkeypatch):
    from ayvu.composition import desktop
    events = []
    factories, _ = setup_factories(events)

    def invalid_plan(*args, **kwargs):
        raise ValueError("invalid recipe selection")

    monkeypatch.setattr(desktop.surface_rules, "project_surface", invalid_plan)
    with pytest.raises(ValueError, match="invalid recipe selection"):
        start(factories, DesktopOverrides(tmp_path, {}, lambda: 123, object()), edition=edition_id)
    assert events == []


def test_unknown_edition_is_refused_before_acquisition(tmp_path):
    events = []
    factories, _ = setup_factories(events)
    with pytest.raises(ValueError, match="Unknown edition id"):
        start(factories, DesktopOverrides(tmp_path, {}, lambda: 123, object()), edition="unknown")
    assert events == []


def test_surface_is_immutable_and_overrides_have_no_product_selector():
    surface = desktop_surface("free")
    assert surface == desktop_surface("ai")
    assert surface is not desktop_surface("free")
    with pytest.raises(FrozenInstanceError):
        surface.actions = ()
    assert tuple(field.name for field in fields(DesktopOverrides)) == ("data_dir", "environment", "clock", "platform")


@pytest.mark.parametrize("edition_id", ("cli_free", "cli_pro"), ids=("cli-free", "cli-pro"))
def test_cli_root_and_app_never_attempt_qt_import(edition_id):
    code = '''
import importlib.abc, sys
class NoDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'PySide2', 'PyQt6', 'PyQt5'} or fullname.startswith('ayvu.interfaces.desktop'):
            raise AssertionError('desktop imported from CLI: ' + fullname)
sys.meta_path.insert(0, NoDesktop())
from ayvu.composition.cli import cli_surface
from ayvu.interfaces.cli.app import create_app
from typer.testing import CliRunner
app = create_app(cli_surface(sys.argv[1]))
result = CliRunner().invoke(app, ['--help'])
assert result.exit_code == 0, result.output
assert not any(name.split('.')[0] in {'PySide6', 'PySide2', 'PyQt6', 'PyQt5'} for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code, edition_id], capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
