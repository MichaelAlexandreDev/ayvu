"""Surface assembly remains pure, using the production resource canaries from #158."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from typer.main import get_command

from ayvu.composition.cli import project_cli_surface
from ayvu.composition.editions import EDITIONS
from ayvu.interfaces.cli.app import create_app
from tests.composition.test_cli_composition import deny_resources


@pytest.mark.parametrize("edition", EDITIONS, ids=lambda edition: edition.id)
def test_create_app_never_constructs_workflow_resources(edition, monkeypatch):
    calls = deny_resources(monkeypatch)

    def denied(*args, **kwargs):
        pytest.fail("process or user path acquired while assembling commands")

    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr("pathlib.Path.home", denied)
    surface = project_cli_surface(edition.features, execution_preset=edition.execution_preset,
                                  report_style=edition.report_style)
    application = create_app(surface)
    assert get_command(application).commands
    assert calls == []


def test_fresh_presenter_import_and_reordered_assembly_never_acquire_resources(tmp_path):
    # The audit points also cover the fresh-process test in test_cli_composition.
    code = '''
import os, sys, requests
def deny(*args, **kwargs):
    raise AssertionError('provider session constructed')
requests.Session.__init__ = deny
def audit(event, args):
    if event.startswith('socket.') or event in {'sqlite3.connect', 'subprocess.Popen'}:
        raise AssertionError(event)
    if event == 'open' and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if any(path.startswith(os.environ[name] + os.sep) for name in ('HOME', 'XDG_CONFIG_HOME')):
            raise AssertionError('user store opened')
sys.addaudithook(audit)
from ayvu.composition import cli as root
for name in ('ProjectJsonRepository', 'ConfigStore',
             'LocalLibraryFilesystem', 'ReaderLauncher'):
    setattr(root, name, deny)
root.providers.LibreTranslateTranslator = deny
root.glossary.managed_glossary_repository = deny
from ayvu.interfaces.cli.app import app, create_app
from ayvu.composition.editions import EDITIONS
from typer.main import get_command
for edition in (*EDITIONS, *reversed(EDITIONS), EDITIONS[0]):
    surface = root.project_cli_surface(edition.features, execution_preset=edition.execution_preset,
                                       report_style=edition.report_style)
    assert get_command(create_app(surface)).commands
assert get_command(app).commands
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
             "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "home").exists() and not (tmp_path / "config").exists()
