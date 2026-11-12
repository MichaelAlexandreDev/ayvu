"""scripts/arch_metrics.py (REF-01, #266) on small synthetic trees and on the real one."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.paths import PROJECT_ROOT as ROOT
SCRIPT = ROOT / "scripts" / "arch_metrics.py"


def load():
    spec = importlib.util.spec_from_file_location("arch_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["arch_metrics"] = module
    spec.loader.exec_module(module)
    return module


arch = load()


def write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "ayvu"
    write(root, "__init__.py")
    write(root, "alpha/__init__.py", "from ..beta import thing\n")
    write(root, "alpha/inner/deep.py", "from ...gamma import x\nimport ayvu.beta.tools\n")
    write(root, "beta/__init__.py", "thing = 1\n")
    write(root, "beta/tools.py", "from ..alpha import inner\n")
    write(root, "gamma/__init__.py", "x = 1\n")
    write(root, "content_use_cases.py", "from .alpha import y\n")  # a legacy top-level module, grouped as content
    write(root, "stray.py", "")
    return root


def test_package_files_group_by_first_segment_and_relative_imports_resolve(tree):
    report = arch.measure(tree)
    assert report.edges == {("alpha", "beta"): 2, ("alpha", "gamma"): 1, ("beta", "alpha"): 1, ("content", "alpha"): 1}


def test_groups_that_import_each_other_are_reported_once(tree):
    assert arch.measure(tree).cycles == [("alpha", "beta")]


def test_a_top_level_file_without_a_group_is_listed(tree):
    assert arch.measure(tree).ungrouped == ["stray.py"]


def test_size_thresholds_count_newlines_like_wc(tmp_path):
    root = tmp_path / "ayvu"
    write(root, "big.py", "x = 1\n" * 801)
    write(root, "edge.py", "x = 1\n" * 400)
    report = arch.measure(root)
    assert report.large_files[400] == [("big.py", 801)]
    assert report.large_files[800] == [("big.py", 801)] and report.large_files[1000] == []


def test_long_functions_and_methods_are_reported(tmp_path):
    root = tmp_path / "ayvu"
    body = "".join(f"    a{i} = {i}\n" for i in range(80))
    write(root, "mod.py", f"def long():\n{body}\n\ndef short():\n    pass\n\nclass C:\n    def method(self):\n"
                          + "".join(f"        b{i} = {i}\n" for i in range(85)))
    names = [name for name, _ in arch.measure(root).long_functions]
    assert names == ["mod.py:88:method", "mod.py:1:long"]


def test_json_output_has_the_documented_keys(tree, capsys):
    arch.main(["--root", str(tree), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"files", "lines", "large_files", "long_functions", "edges", "cycles", "ungrouped"}
    assert data["cycles"] == [["alpha", "beta"]]


def test_the_script_never_imports_ayvu():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "ayvu" not in imported


def test_every_top_level_production_file_has_a_group():
    report = arch.measure(ROOT / "src" / "ayvu")
    assert report.ungrouped == []


def test_the_script_runs_as_a_command():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0 and result.stdout.startswith("files: ")
