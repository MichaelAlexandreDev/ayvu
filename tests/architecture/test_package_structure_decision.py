"""The package-structure decision (REF-02, #267) stays consistent with the source tree."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from tests.support.paths import PROJECT_ROOT as ROOT
DECISION = ROOT / "docs" / "architecture" / "decisions" / "package-structure.md"
SOURCE = ROOT / "src" / "ayvu"
ROW = re.compile(r"^\| `([^`]+\.py)` \| (?:`([^`]+)`|—) \| .* \|$")


def text() -> str:
    return DECISION.read_text(encoding="utf-8")


def mapping() -> dict[str, str | None]:
    rows = [ROW.match(line) for line in text().splitlines()]
    entries = [(match[1], match[2]) for match in rows if match]
    assert len({current for current, _ in entries}) == len(entries), "a file is mapped twice"
    return dict(entries)


def source_files() -> set[str]:
    return {path.relative_to(SOURCE).as_posix() for path in SOURCE.rglob("*.py") if "__pycache__" not in path.parts}


def test_the_decision_is_accepted_with_a_record_and_a_gate():
    assert re.search(r"^Status: Accepted$", text(), re.M)
    assert "## Acceptance record" in text() and "## Acceptance gate" in text()


def test_every_file_is_either_still_mapped_or_inside_a_target_package():
    table = mapping()
    targets = {target.split("/")[0] for target in table.values() if target and "/" in target}
    for path in source_files():
        if path in table:
            continue
        top = path.split("/")[0]
        assert "/" in path and top in targets, f"{path} is neither in the mapping nor in a decided package"


def test_targets_are_unique_except_the_documented_merge():
    targets = [target for target in mapping().values() if target]
    duplicates = {target for target in targets if targets.count(target) > 1}
    assert duplicates == {"preferences/model.py"}


def test_every_current_cycle_has_an_issue_that_removes_it():
    spec = importlib.util.spec_from_file_location("arch_metrics", ROOT / "scripts" / "arch_metrics.py")
    arch = importlib.util.module_from_spec(spec)
    sys.modules["arch_metrics"] = arch
    spec.loader.exec_module(arch)
    for a, b in arch.measure(SOURCE).cycles:
        assert f"| {a} ↔ {b} |" in text() or f"| {b} ↔ {a} |" in text(), f"{a} ↔ {b} has no issue assigned"
