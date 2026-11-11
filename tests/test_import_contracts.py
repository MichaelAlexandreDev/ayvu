"""D3 and D11 (REF-03, #271): the import contracts in pyproject.toml hold, and every exception is owned."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
ISSUE = re.compile(r'^\s*"ayvu\.[\w.]+ -> ayvu\.[\w.]+",\s*# REF-\d\d \(#\d+\): .+$')


def lint_imports() -> subprocess.CompletedProcess:
    command = shutil.which("lint-imports", path=str(Path(sys.executable).parent)) or "lint-imports"
    return subprocess.run([command, "--no-logo", "--no-cache"], cwd=ROOT, capture_output=True, text=True,
                          timeout=300, check=False)


def test_the_import_contracts_hold():
    result = lint_imports()
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert re.search(r"Contracts: \d+ kept, 0 broken\.", result.stdout)


def test_the_layers_contract_follows_the_decision_and_is_exhaustive():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["importlinter"]
    layers = next(c for c in config["contracts"] if c["id"] == "layers")
    assert layers["type"] == "layers" and layers["exhaustive"] is True and layers["containers"] == ["ayvu"]
    first = [layer.split(" : ")[0] for layer in layers["layers"]]
    assert first == ["interfaces", "composition", "v1", "recovery", "review", "translation", "projects", "preferences",
                     "knowledge", "artifacts", "provenance", "jobs", "content", "kernel"]


def test_every_ignored_import_names_the_issue_that_removes_it():
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    inside, checked = False, 0
    for line in lines:
        if line.startswith("ignore_imports = ["):
            inside = True
        elif inside and line.startswith("]"):
            inside = False
        elif inside:
            assert ISSUE.match(line), f"an ignored import must name its REF issue: {line.strip()}"
            checked += 1
    assert checked > 0
