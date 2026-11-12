"""REF-20 batch A: normalize metadata without accepting or redating any decision."""

from __future__ import annotations

from collections import Counter
from datetime import date
import re

import pytest

from tests.support.paths import PROJECT_ROOT

ARCHITECTURE = PROJECT_ROOT / "docs" / "architecture"
STATUSES = {"Proposed", "Accepted", "Superseded", "Deprecated", "Rejected"}
# Transitional inventory: keep root records here until their planned moves.
# Values freeze the original status, record date (not acceptance date) and issue.
RECORDS = {
    "decisions/adr-desktop-stack.md": ("Accepted", "2026-10-02", "124"),
    "decisions/adr-local-worker-isolation.md": ("Accepted", "2026-11-06", "167"),
    "decisions/adr-secret-store-adapters.md": ("Proposed", "2026-11-06", "160"),
    "decisions/content-model.md": ("Accepted", "2026-11-03", "140"),
    "decisions/ai-assistance-authority.md": ("Proposed", "2026-11-10", "166"),
    "decisions/package-structure.md": ("Accepted", "2026-11-10", "267"),
    "decisions/rights-purpose-governance.md": ("Proposed", "2026-11-05", "165"),
    "decisions/v1-retirement.md": ("Accepted", "2026-11-12", "286"),
    "decisions/epub-format.md": ("Accepted", "2026-11-04", "144"),
    "decisions/format-adapter.md": ("Accepted", "2026-11-03", "127"),
    "decisions/html-structure.md": ("Accepted", "2026-11-04", "143"),
    "decisions/job-artifacts.md": ("Accepted", "2026-11-03", "137"),
    "decisions/job-lifecycle.md": ("Accepted", "2026-11-03", "136"),
    "decisions/local-project-storage.md": ("Accepted", "2026-11-03", "135"),
    "decisions/modular-monolith-boundaries.md": ("Accepted", "2026-09-03", "125"),
    "decisions/product-threat-model.md": ("Accepted", "2026-09-29", "129"),
    "decisions/project-lifecycle.md": ("Accepted", "2026-11-03", "134"),
    "decisions/provenance.md": ("Accepted", "2026-11-03", "138"),
    "decisions/provider-capabilities.md": ("Accepted", "2026-11-03", "103"),
    "decisions/provider-contracts.md": ("Accepted", "2026-11-03", "141"),
    "decisions/translation-pipeline.md": ("Accepted", "2026-11-03", "142"),
}
HEADER = re.compile(
    r"Status: (?P<status>\w+)\n"
    r"Decision-Date: `(?P<date>\d{4}-\d{2}-\d{2})`\n"
    r"Owners: (?P<owners>[^\n]+)\n"
    r"Issue: \[#(?P<issue>[1-9]\d*)[^\]\n]*\]"
    r"\(https://github\.com/(?P<repository>DevStarrk1137|MichaelAlexandreDev)"
    r"/ayvu/issues/(?P<target>[1-9]\d*)\)\.?"
)
VALID = (
    "# Example decision\n\nStatus: Accepted\nDecision-Date: `2026-11-03`\n"
    "Owners: Project maintainer (acceptance); Content (contract ownership)\n"
    "Issue: [#140 — Content model](https://github.com/MichaelAlexandreDev/ayvu/issues/140)\n\n"
    "## Scope\n\nExample body.\n"
)


def parse_header(text: str) -> dict[str, str]:
    title, block, body = text.split("\n\n", 2)
    assert re.fullmatch(r"# \S[^\n]*", title), "expected one H1 followed by a blank line"
    match = HEADER.fullmatch(block)
    assert match is not None, "expected exactly four ordered, nonempty metadata lines"
    fields = match.groupdict()
    assert fields["status"] in STATUSES
    date.fromisoformat(fields["date"])
    assert fields["owners"].strip()
    assert fields["issue"] == fields["target"], "issue label and URL disagree"
    # Metadata before the first section belongs to the header area, not a second block.
    prefix = re.split(r"^## ", body, maxsplit=1, flags=re.MULTILINE)[0]
    assert not re.search(r"^(Status|Decision-Date|Owners|Issue):", prefix, re.MULTILINE)
    return fields


# Companion records split from a decision (REF-20 C5): same header as their parent, not separate decisions.
COMPANIONS = {
    "decisions/ai-assistance-authority-transitions.md": "decisions/ai-assistance-authority.md",
    "decisions/rights-purpose-grants.md": "decisions/rights-purpose-governance.md",
    "decisions/rights-purpose-retention.md": "decisions/rights-purpose-governance.md",
    "decisions/rights-purpose-examples.md": "decisions/rights-purpose-governance.md",
}


@pytest.mark.parametrize("companion, parent", COMPANIONS.items())
def test_a_companion_record_carries_its_parent_header(companion: str, parent: str):
    assert parse_header((ARCHITECTURE / companion).read_text(encoding="utf-8")) == parse_header(
        (ARCHITECTURE / parent).read_text(encoding="utf-8"))
    assert f"]({parent.split('/')[-1]})" in (ARCHITECTURE / companion).read_text(encoding="utf-8")


def test_transitional_inventory_includes_every_decision():
    discovered = set(ARCHITECTURE.joinpath("decisions").rglob("*.md"))
    for path in ARCHITECTURE.glob("*.md"):
        prefix = re.split(r"^## ", path.read_text(encoding="utf-8"), maxsplit=1, flags=re.MULTILINE)[0]
        if prefix.startswith("# ADR:") or re.search(r"^Status:", prefix, re.MULTILINE):
            discovered.add(path)
    discovered = {path for path in discovered if path.relative_to(ARCHITECTURE).as_posix() not in COMPANIONS}
    assert {path.relative_to(ARCHITECTURE).as_posix() for path in discovered} == set(RECORDS)
    actual = Counter(parse_header(path.read_text(encoding="utf-8"))["status"] for path in discovered)
    assert actual == {"Accepted": 18, "Proposed": 3}


@pytest.mark.parametrize("relative", RECORDS)
def test_decision_retains_its_original_status_date_and_issue(relative: str):
    fields = parse_header((ARCHITECTURE / relative).read_text(encoding="utf-8"))
    assert (fields["status"], fields["date"], fields["issue"]) == RECORDS[relative]


@pytest.mark.parametrize("relative, identity", [
    ("decisions/ai-assistance-authority.md", "Contract-ID: `ayvu-assistance-authority-v1`"),
    ("decisions/rights-purpose-governance.md", "Policy-ID: `ayvu-rights-purpose-v1`"),
])
def test_proposed_contract_identity_and_vocabulary_are_preserved(relative: str, identity: str):
    text = (ARCHITECTURE / relative).read_text(encoding="utf-8")
    parse_header(text)
    prefix = re.split(r"^## ", text, maxsplit=1, flags=re.MULTILINE)[0]
    assert re.findall(r"^(?:Contract-ID|Policy-ID): .*", prefix, re.MULTILINE) == [identity]
    assert re.findall(r"^Vocabulary-Version: .*", prefix, re.MULTILINE) == ["Vocabulary-Version: `1`"]


@pytest.mark.parametrize("relative, implementation", [
    ("modules/content-use-cases.md", "Implementation: Implemented"),
    ("modules/review.md", "Implementation: Implemented; shared application use cases and CLI orchestration are connected."),
    ("modules/interfaces-cli.md", "Implementation: Implementation matrix consolidated in #158; this does not change ADR status."),
])
def test_implementation_notes_do_not_claim_decision_status(relative: str, implementation: str):
    text = (ARCHITECTURE / relative).read_text(encoding="utf-8")
    prefix = re.split(r"^## ", text, maxsplit=1, flags=re.MULTILINE)[0]
    assert implementation in prefix.splitlines()
    assert not re.search(r"^Status:", prefix, re.MULTILINE)


@pytest.mark.parametrize("field", ["Status", "Decision-Date", "Owners", "Issue"])
@pytest.mark.parametrize("operation", ["missing", "duplicate", "separate-duplicate"])
def test_missing_or_duplicate_metadata_is_rejected(field: str, operation: str):
    line = re.search(rf"^{field}: .*\n", VALID, re.MULTILINE).group()
    replacement = {"missing": "", "duplicate": line + line, "separate-duplicate": line + "\n" + line}[operation]
    with pytest.raises((AssertionError, ValueError)):
        parse_header(VALID.replace(line, replacement, 1))


@pytest.mark.parametrize("old, new", [
    ("# Example decision", "## Example decision"),
    ("# Example decision\n\n", "# Example decision\n"),
    ("Status: Accepted\nDecision-Date: `2026-11-03`", "Decision-Date: `2026-11-03`\nStatus: Accepted"),
    ("Status: Accepted", "Status: `Accepted`"),
    ("Status: Accepted", "Status: Implemented"),
    ("Status: Accepted", "Status: Unknown"),
    ("`2026-11-03`", "`2026-02-30`"),
    ("`2026-11-03`", "`20261103`"),
    ("Owners: Project maintainer (acceptance); Content (contract ownership)", "Owners: "),
    ("Owners: Project maintainer (acceptance); Content (contract ownership)", "Owners:   "),
    ("; Content (contract ownership)", ";\nContent (contract ownership)"),
    ("Issue: [#140 — Content model](https://github.com/MichaelAlexandreDev/ayvu/issues/140)", "Issue: "),
    ("https://github.com/", "https://example.com/"),
    ("/issues/140)", "/issues/141)"),
    ("/issues/140)\n\n", "/issues/140)\n"),
])
def test_malformed_headers_are_rejected(old: str, new: str):
    with pytest.raises((AssertionError, ValueError)):
        parse_header(VALID.replace(old, new, 1))


@pytest.mark.parametrize("status", sorted(STATUSES))
def test_closed_status_vocabulary_and_body_examples(status: str):
    text = VALID.replace("Status: Accepted", f"Status: {status}", 1)
    text += "\n```markdown\nStatus: Accepted\nOwners: Example only\n```\n"
    assert parse_header(text)["status"] == status
