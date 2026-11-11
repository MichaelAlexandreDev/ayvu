import json
import os
from pathlib import Path
import pytest
from typer.testing import CliRunner
from ayvu.interfaces.cli import app as cli, report as cli_report, run as cli_run
import ayvu.composition.cli as composition
from tests.support.workflow_characterization_support import FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _write_epub

@pytest.fixture(autouse=True)
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


def _run(first, second, output, *extra):
    return CliRunner().invoke(cli.app, [
        "--mode", "developer", "translate", str(first), str(second),
        "--output-dir", str(output), *extra,
    ])
