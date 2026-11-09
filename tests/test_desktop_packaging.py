"""Offline packaging and headless entry-point smoke tests for DESKTOP-009."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_desktop_extra_and_entry_points_are_optional() -> None:
    assert PROJECT["scripts"]["ayvu"] == "ayvu.cli:app"
    assert PROJECT["scripts"]["ayvu-desktop"] == "ayvu.desktop_bootstrap:main"
    assert PROJECT["optional-dependencies"]["desktop"] == ["PySide6-Essentials==6.11.2"]
    for requirement in PROJECT["dependencies"] + PROJECT["optional-dependencies"]["dev"]:
        assert "pyside" not in requirement.lower() and "pyqt" not in requirement.lower()


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    return {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(config),
            "PYTHONPATH": str(ROOT / "src"), "QT_QPA_PLATFORM": "offscreen",
            "PYTHONDONTWRITEBYTECODE": "1"}


def run_python(code: str, args: list[str], tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code, *args], cwd=tmp_path,
                          env=env, text=True, capture_output=True, timeout=30, check=False)


# This import hook makes the test independent of whether Qt is installed on the host.
BLOCK_QT = '''
import importlib.abc, sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PySide6" or fullname.startswith("PySide6."):
            raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
sys.meta_path.insert(0, NoQt())
'''


def test_existing_cli_and_import_work_without_qt(tmp_path: Path, isolated_env: dict[str, str]) -> None:
    result = run_python(BLOCK_QT + "\nimport ayvu\nfrom ayvu.cli import app\napp()", ["--help"], tmp_path, isolated_env)
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout


def test_desktop_help_without_qt(tmp_path: Path, isolated_env: dict[str, str]) -> None:
    result = run_python(BLOCK_QT + "\nfrom ayvu.desktop_bootstrap import main\nsys.exit(main())", ["--help"], tmp_path, isolated_env)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "traceback" not in (result.stdout + result.stderr).lower()


def test_missing_qt_has_actionable_diagnostic(tmp_path: Path, isolated_env: dict[str, str]) -> None:
    result = run_python(BLOCK_QT + "\nfrom ayvu.desktop_bootstrap import main\nsys.exit(main())", [], tmp_path, isolated_env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ayvu[desktop]" in output
    assert "traceback" not in output.lower()
    assert len(output.splitlines()) <= 5


def test_qt_that_will_not_load_does_not_ask_to_install_it_again(tmp_path: Path, isolated_env: dict[str, str]) -> None:
    code = '''
import importlib.abc, sys
class BrokenQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] == "PySide6":
            raise ImportError("libEGL.so.1: cannot open shared object file")
sys.meta_path.insert(0, BrokenQt())
from ayvu.desktop_bootstrap import main
raise SystemExit(main())
'''
    result = run_python(code, [], tmp_path, isolated_env)

    assert result.returncode == 1
    assert "bibliotecas gráficas" in result.stderr
    assert "pip install" not in result.stderr
    assert "Traceback" not in result.stderr


def test_available_qt_delegates_to_replaceable_start(tmp_path: Path, isolated_env: dict[str, str]) -> None:
    fake_qt = tmp_path / "fake" / "PySide6"
    fake_qt.mkdir(parents=True)
    (fake_qt / "__init__.py").write_text("", encoding="utf-8")
    (fake_qt / "QtWidgets.py").write_text("", encoding="utf-8")
    isolated_env["PYTHONPATH"] = os.pathsep.join((str(fake_qt.parent), isolated_env["PYTHONPATH"]))
    result = run_python('''
import sys
import ayvu.desktop_bootstrap as bootstrap
def replacement():
    print("delegated exactly once")
    return 37
bootstrap.start_desktop = replacement
sys.exit(bootstrap.main())
''', [], tmp_path, isolated_env)
    assert result.returncode == 37, result.stderr
    assert result.stdout.count("delegated exactly once") == 1
