"""Documentation validation for the #166 assistance authority decision. No assistance runtime exists to test."""
from __future__ import annotations

import re
from pathlib import Path

from tests.support.paths import PROJECT_ROOT as ROOT
DECISION = ROOT / "docs" / "architecture" / "decisions" / "ai-assistance-authority.md"
# Transitions and the adversarial review moved to a companion record of the same contract (REF-20).
TRANSITIONS = ROOT / "docs" / "architecture" / "decisions" / "ai-assistance-authority-transitions.md"
ROLES = ("TerminologyCandidateGenerator", "QualityFindingAssistant", "StyleSuggestionAssistant",
         "LayoutSuggestionAssistant")


def _text() -> str:
    return DECISION.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + len(heading))
    return text[start:] if following == -1 else text[start:following]


def test_record_stays_proposed_with_its_identity_fields():
    text = _text()
    assert re.findall(r"^Status: (\w+)$", text, flags=re.MULTILINE) == ["Proposed"]
    for field in ("Decision-Date: `2026-11-10`", "Contract-ID: `ayvu-assistance-authority-v1`",
                  "Vocabulary-Version: `1`", "Owners:"):
        assert field in text


def test_role_matrix_has_exactly_the_four_closed_roles():
    matrix = _section(_text(), "## Closed role and authority matrix v1")
    rows = [line for line in matrix.splitlines() if line.startswith("| `")]
    assert [re.match(r"\| `(\w+)`", row).group(1) for row in rows] == list(ROLES)
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == 7 and all(cells)
        assert "`candidate_only`" in cells[4] and "class `none`" in cells[5] and "P1" in cells[6]


def test_generic_abstractions_are_excluded_and_assigned_owners():
    exclusions = _section(_text(), "## Vocabulary, names and compatibility")
    for name in ("`TranslationAssistant`", "`ContextBuilder`", "`ModelRuntime`", "MCP transport"):
        assert name in exclusions
    assert "Wave I" in exclusions and "TranslationContextBuilder" in exclusions


def test_adversarial_review_covers_a01_to_a22_once_each():
    cases = re.findall(r"^\| (A\d\d) \|", TRANSITIONS.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert cases == [f"A{index:02d}" for index in range(1, 23)]


def test_role_states_and_human_gate_are_explicit():
    text = _text()
    both = text + TRANSITIONS.read_text(encoding="utf-8")
    for state in ("absent", "disabled", "denied", "unavailable", "failed", "cancelled", "successful"):
        assert f"| `{state}` |" in both
    for transition in ("T0", "T1", "T2", "T3", "D0"):
        assert f"| {transition} — " in both
    gate = _section(text, "## Binary human gate and acceptance checklist")
    assert "`Status: Proposed` to `Status: Accepted`" in gate
    assert gate.count("- [ ] ") == 9 and "- [x]" not in gate


def test_decision_adds_no_runtime_dependency():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for name in ("openai", "anthropic", "mcp", "langchain", "transformers", "llama"):
        assert not re.search(rf'"{name}[^"]*"', pyproject)
    assert not list((ROOT / "src" / "ayvu").glob("*assist*"))
