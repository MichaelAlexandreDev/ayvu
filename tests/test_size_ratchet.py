"""D10 size ratchet (REF-03, #271): files up to 400 lines and functions up to 80, tests included."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location("size_ratchet", ROOT / "scripts" / "size_ratchet.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["size_ratchet"] = module
    spec.loader.exec_module(module)
    return module


ratchet = load_script()


def test_the_code_matches_the_ratchet():
    problems = ratchet.differences(ratchet.load(), ratchet.measure())
    assert not problems, "\n".join(problems)


def test_the_ledger_is_sorted_and_only_lists_oversized_entries():
    data = json.loads(ratchet.LEDGER.read_text(encoding="utf-8"))
    for kind, limit in (("files", ratchet.FILE_LIMIT), ("functions", ratchet.FUNCTION_LIMIT)):
        assert list(data[kind]) == sorted(data[kind])
        assert all(size > limit for size in data[kind].values())


def make_tree(tmp_path: Path, lines: int, body: int) -> Path:
    for scope in ratchet.SCOPES:
        (tmp_path / scope).mkdir(parents=True, exist_ok=True)
    function = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(body - 1))
    filler = "".join(f"y{i} = {i}\n" for i in range(max(0, lines - function.count("\n"))))
    (tmp_path / "src" / "ayvu" / "big.py").write_text(function + filler, encoding="utf-8")
    return tmp_path


def test_growth_new_entries_shrinkage_and_finished_entries_are_reported(tmp_path):
    tree = make_tree(tmp_path, lines=450, body=90)
    actual = ratchet.measure(tree)
    assert actual == {"files": {"src/ayvu/big.py": 450}, "functions": {"src/ayvu/big.py::f": 90}}
    assert ratchet.differences({"files": {}, "functions": {}}, actual) == [
        "new file over the 400-line limit: src/ayvu/big.py has 450 lines; split it (D10)",
        "new function over the 80-line limit: src/ayvu/big.py::f has 90 lines; split it (D10)",
    ]
    assert any("grew from 440 to 450" in p for p in ratchet.differences(
        {"files": {"src/ayvu/big.py": 440}, "functions": {"src/ayvu/big.py::f": 90}}, actual))
    assert any("shrank from 460 to 450" in p for p in ratchet.differences(
        {"files": {"src/ayvu/big.py": 460}, "functions": {"src/ayvu/big.py::f": 90}}, actual))
    finished = ratchet.differences({"files": {"src/ayvu/big.py": 450, "src/ayvu/old.py": 500},
                                    "functions": {"src/ayvu/big.py::f": 90}}, actual)
    assert finished == ["src/ayvu/old.py is now within 400 lines or gone; run scripts/size_ratchet.py --update"]


def test_update_lowers_and_drops_but_never_raises_or_adds(tmp_path):
    actual = ratchet.measure(make_tree(tmp_path, lines=450, body=90))
    ledger = {"files": {"src/ayvu/big.py": 500, "src/ayvu/gone.py": 900}, "functions": {"src/ayvu/big.py::f": 85}}
    assert ratchet.update(ledger, actual) == {"files": {"src/ayvu/big.py": 450}, "functions": {"src/ayvu/big.py::f": 85}}
    assert ratchet.update({"files": {}, "functions": {}}, actual) == {"files": {}, "functions": {}}


def test_move_carries_entries_with_their_sizes_and_never_overwrites():
    ledger = {"files": {"src/ayvu/old.py": 500, "src/ayvu/other.py": 450},
              "functions": {"src/ayvu/old.py::f": 90, "src/ayvu/old.pyx::g": 85}}
    assert ratchet.move(ledger, "src/ayvu/old.py", "src/ayvu/pkg/new.py") == {
        "files": {"src/ayvu/pkg/new.py": 500, "src/ayvu/other.py": 450},
        "functions": {"src/ayvu/pkg/new.py::f": 90, "src/ayvu/old.pyx::g": 85}}
    with pytest.raises(ValueError, match="already in the ledger"):
        ratchet.move(ledger, "src/ayvu/old.py", "src/ayvu/other.py")


def test_methods_and_nested_functions_are_named_by_qualified_name(tmp_path):
    for scope in ratchet.SCOPES:
        (tmp_path / scope).mkdir(parents=True, exist_ok=True)
    body = "".join(f"            z{i} = {i}\n" for i in range(85))
    (tmp_path / "tests" / "t.py").write_text(f"class C:\n    def m(self):\n        def inner():\n{body}", encoding="utf-8")
    assert set(ratchet.measure(tmp_path)["functions"]) == {"tests/t.py::C.m", "tests/t.py::C.m.inner"}
