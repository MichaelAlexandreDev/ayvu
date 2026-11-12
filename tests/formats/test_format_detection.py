
import pytest

from ayvu.formats.contracts import MAX_DETECTION_BYTES, DetectionEvidence, DetectionOutcome, FormatError
from ayvu.formats.local import LocalFormatSource
from tests.support.format_contract_env import CountingSource
from tests.support.format_fakes import CARDS_HEADER

from tests.formats.format_adapter_support import detector
from tests.formats.format_adapter_support import named
from tests.formats.format_adapter_support import service_for
from tests.formats.format_adapter_support import signed


@pytest.mark.parametrize("adapters,evidence,outcome,reason,candidates", [
    ([], DetectionEvidence(b"x"), DetectionOutcome.NO_MATCH, "no_candidate", ()),
    ([signed("a", b"AAA")], DetectionEvidence(b"AAA...", ".other"), DetectionOutcome.MATCH, "signature_match", ("a",)),
    ([signed("a", b"AAA")], DetectionEvidence(b"BBB", ".signed"), DetectionOutcome.NO_MATCH, "signature_conflict", ("a",)),
    ([signed("a", b"AAA")], DetectionEvidence(b"", ".signed"), DetectionOutcome.NO_MATCH, "signature_conflict", ("a",)),
    ([signed("a", b"AAA")], DetectionEvidence(b"BBB", None), DetectionOutcome.NO_MATCH, "no_candidate", ()),
    ([named("a")], DetectionEvidence(b"anything", ".plain"), DetectionOutcome.MATCH, "extension_match", ("a",)),
    ([named("a")], DetectionEvidence(b"anything", ".other"), DetectionOutcome.NO_MATCH, "no_candidate", ()),
    ([named("a", media_types=("text/plain",))], DetectionEvidence(b"x", None, "text/plain"), DetectionOutcome.MATCH, "extension_match", ("a",)),
    ([signed("b"), signed("a")], DetectionEvidence(b"SAME"), DetectionOutcome.AMBIGUOUS, "multiple_signatures", ("a", "b")),
    ([named("b"), named("a")], DetectionEvidence(b"x", ".plain"), DetectionOutcome.AMBIGUOUS, "multiple_extensions", ("a", "b")),
    ([signed("a", b"AAA"), named("b")], DetectionEvidence(b"AAA", ".plain"), DetectionOutcome.AMBIGUOUS, "signature_extension_conflict", ("a", "b")),
    ([signed("b", b"AAA"), named("a")], DetectionEvidence(b"AAA", ".plain"), DetectionOutcome.AMBIGUOUS, "signature_extension_conflict", ("a", "b")),
    ([signed("a", b"AAA", (".plain",)), named("b")], DetectionEvidence(b"AAA", ".plain"), DetectionOutcome.MATCH, "signature_match", ("a",)),
    ([signed("a", b"AAA"), named("b")], DetectionEvidence(b"AAA"), DetectionOutcome.MATCH, "signature_match", ("a",)),
    ([signed("a", b"AAA", (".plain",)), named("b")], DetectionEvidence(b"zzz", ".plain"), DetectionOutcome.MATCH, "extension_match", ("b",)),
    ([signed("a", b"AAA"), signed("b", b"BBB", (".pretend",))], DetectionEvidence(b"AAA", ".pretend"), DetectionOutcome.MATCH, "signature_match", ("a",)),
], ids=range(16))
def test_detection_rules(adapters, evidence, outcome, reason, candidates):
    result = detector(*adapters).detect(evidence)
    assert (result.outcome, result.reason, result.candidates) == (outcome, reason, candidates)
    assert result.adapter_id == (candidates[0] if outcome is DetectionOutcome.MATCH else None)


def test_detection_order_does_not_depend_on_registration():
    first = detector(signed("a"), signed("b")).detect(DetectionEvidence(b"SAME"))
    second = detector(signed("b"), signed("a")).detect(DetectionEvidence(b"SAME"))
    assert first == second


@pytest.mark.parametrize("kwargs", [
    {"head": "text"}, {"head": b"x" * (MAX_DETECTION_BYTES + 1)}, {"head": b"", "extension": "plain"}, {"head": b"", "extension": ".A"}, {"head": b"", "media_type": "nope"},
], ids=range(5))
def test_detection_evidence_is_bounded(kwargs):
    with pytest.raises(FormatError, match="^invalid_detection_evidence$"):
        DetectionEvidence(**kwargs)


def test_detection_reads_no_more_than_its_window(tmp_path):
    path = tmp_path / "big.cards"
    path.write_bytes(CARDS_HEADER + b"x" * 100000)

    class Spy(CountingSource):
        limits = []

        def head(self, limit):
            self.limits.append(limit)
            return self.inner.head(limit)

    source = Spy(LocalFormatSource(path))
    assert service_for().detect(source).adapter_id == "fake_cards"
    assert Spy.limits == [MAX_DETECTION_BYTES] and source.reads == 0


# Local files
