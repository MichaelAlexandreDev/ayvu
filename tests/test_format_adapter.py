import ast
import importlib
import logging
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from ayvu.content import AdapterIdentity, Locator, UnitContent, build_document
from ayvu.format_adapter import (
    MAX_DETECTION_BYTES, AliasVerdict, ContentScope, DetectionEvidence, DetectionOutcome, ExtractionRequest, ExtractionResult, FidelityAspect,
    FidelityClaim, FidelityLevel, FidelityRequirement, FindingSeverity, FormatCancelledError, FormatError, FormatInventory,
    FormatLimits, FormatMaturity, FormatOperation, FormatOperationError, FormatRequirements, FormatSignature, InspectionResult, MaterializationResult,
    OperationClaim, OperationSupport, PreviewRequest, ReconstructionRequest, UnitTranslation, ValidationFinding, ValidationResult, negotiate_format,
)
from ayvu.format_local import LocalFormatSource, LocalOutputGuard
from ayvu.format_service import FormatAdapter, FormatRegistration, FormatRegistry, FormatService
from ayvu.job_events import JobFailure, JobFailureFamily
from format_contract import PROJECT, CountingSource, FixedGuard, FormatAdapterContract, FormatEnv, FormatHarness
from format_fakes import (
    CARDS_HEADER, CARDS_REGISTRATION, EVIDENCE, KIND, NOTES_REGISTRATION, CardsPorts, NotesPorts, all_fidelity, cards_adapter, cards_descriptor, notes_adapter,
    notes_descriptor, unsupported,
)

CARDS = CARDS_HEADER + b"#one\nfirst card\nsecond card\n#two\nthird card\n"
ROOT = Path(__file__).parents[1]


class TestNotesContract(FormatAdapterContract):
    @pytest.fixture
    def harness(self):
        return FormatHarness(notes_adapter(), NOTES_REGISTRATION, b"a note\nanother note\n", b"a changed note\n", ".notes")


class TestCardsContract(FormatAdapterContract):
    @pytest.fixture
    def harness(self):
        return FormatHarness(cards_adapter(), CARDS_REGISTRATION, CARDS, CARDS_HEADER + b"#one\nchanged\n", ".cards")


def registry_with(*pairs):
    registry = FormatRegistry()
    for registration, adapter in pairs:
        registry.register(registration, adapter)
    return registry


def service_for(adapter=None, registration=CARDS_REGISTRATION):
    return FormatService(registry_with((registration, adapter or cards_adapter())))


@pytest.fixture
def cards(tmp_path):
    path = tmp_path / "book.cards"
    path.write_bytes(CARDS)
    return path


def documents_of(path):
    return service_for().extract("fake_cards", LocalFormatSource(path), ExtractionRequest(PROJECT)).documents


def complete(documents):
    return tuple(UnitTranslation(unit.unit_id, "text") for document in documents for unit in document.units)


# The two fakes tell the truth, and the truth differs.

def test_the_two_fakes_expose_different_abilities():
    notes, cards_ = notes_descriptor(), cards_descriptor()
    assert not notes.runs(FormatOperation.RECONSTRUCT) and cards_.runs(FormatOperation.RECONSTRUCT)
    assert notes.fidelity_of(FidelityAspect.STRUCTURE) is FidelityLevel.NOT_APPLICABLE
    assert cards_.fidelity_of(FidelityAspect.STRUCTURE) is FidelityLevel.PRESERVED


# Claims

@pytest.mark.parametrize("support,evidence,safe_points,code", [
    (OperationSupport.SUPPORTED, (), (), "contradictory_claim"),
    (OperationSupport.UNSUPPORTED, EVIDENCE, (), "contradictory_claim"),
    (OperationSupport.UNKNOWN, EVIDENCE, (), "contradictory_claim"),
    (OperationSupport.UNSUPPORTED, (), ("a_point",), "contradictory_claim"),
    (OperationSupport.SUPPORTED, ("no colon",), (), "invalid_operation_claim"),
    (OperationSupport.SUPPORTED, ("doc:/etc/passwd",), (), "invalid_operation_claim"),
    (OperationSupport.SUPPORTED, ("http://x",), (), "invalid_operation_claim"),
    (OperationSupport.SUPPORTED, EVIDENCE, ("Bad Point",), "invalid_operation_claim"),
    (OperationSupport.SUPPORTED, EVIDENCE, ("a", "a"), "invalid_operation_claim"),
    (OperationSupport.SUPPORTED, EVIDENCE, ("artifact_before_write",), "reserved_safe_point"),
    (OperationSupport.SUPPORTED, EVIDENCE, ("format_before_work",), "reserved_safe_point"),
    ("supported", EVIDENCE, (), "invalid_operation_claim"),
], ids=range(12))
def test_operation_claims_must_be_consistent(support, evidence, safe_points, code):
    with pytest.raises(FormatError, match=f"^{code}$"):
        OperationClaim(FormatOperation.EXTRACT, support, evidence, safe_points)


def test_experimental_support_may_have_no_evidence():
    assert OperationClaim(FormatOperation.EXTRACT, OperationSupport.EXPERIMENTAL).evidence == ()


@pytest.mark.parametrize("level,evidence", [
    (FidelityLevel.PRESERVED, ()), (FidelityLevel.BEST_EFFORT, ()), (FidelityLevel.LOST, ()),
    (FidelityLevel.UNKNOWN, EVIDENCE), (FidelityLevel.NOT_APPLICABLE, EVIDENCE),
])
def test_fidelity_claims_must_be_consistent(level, evidence):
    with pytest.raises(FormatError, match="^contradictory_claim$"):
        FidelityClaim(FidelityAspect.IMAGES, level, evidence)


def test_a_descriptor_must_state_every_operation_and_aspect():
    with pytest.raises(FormatError, match="^incomplete_descriptor$"):
        notes_descriptor(operations=notes_descriptor().operations[:-1])
    with pytest.raises(FormatError, match="^incomplete_descriptor$"):
        notes_descriptor(fidelity=notes_descriptor().fidelity[:-1])
    twice = notes_descriptor().operations
    with pytest.raises(FormatError, match="^incomplete_descriptor$"):
        notes_descriptor(operations=(*twice[:-1], twice[0]))
    with pytest.raises(FormatError, match="^incomplete_descriptor$"):
        notes_descriptor(operations=(*twice, twice[0]))


def test_a_format_that_writes_nothing_cannot_claim_to_preserve_anything():
    with pytest.raises(FormatError, match="^contradictory_claim$"):
        notes_descriptor(fidelity=all_fidelity(FidelityLevel.PRESERVED, EVIDENCE))


def test_a_format_that_extracts_must_state_scopes_and_one_that_does_not_cannot():
    with pytest.raises(FormatError, match="^contradictory_claim$"):
        notes_descriptor(content_scopes=frozenset())
    claims = (OperationClaim(FormatOperation.INSPECT, OperationSupport.SUPPORTED, EVIDENCE),
              *unsupported(FormatOperation.EXTRACT, FormatOperation.PREVIEW, FormatOperation.RECONSTRUCT, FormatOperation.VALIDATE))
    with pytest.raises(FormatError, match="^contradictory_claim$"):
        notes_descriptor(operations=claims)


@pytest.mark.parametrize("changes", [
    {"adapter": "notes"}, {"locator_schema": "Bad"}, {"locator_version": 0}, {"locator_version": True}, {"contract_version": 0},
    {"media_types": ("nope",)}, {"extensions": ("notes",)}, {"extensions": (".NOTES",)}, {"extensions": (".a", ".a")},
    {"format_versions": ("../x",)}, {"signatures": (b"x",)}, {"limits": 5}, {"maturity": "stable"}, {"content_scopes": frozenset({"body"})},
    {"operations": []}, {"fidelity": "all"}, {"extensions": (), "media_types": ()},
], ids=lambda value: next(iter(value)))
def test_malformed_descriptors_cannot_be_built(changes):
    with pytest.raises(FormatError, match="^invalid_descriptor$"):
        notes_descriptor(**changes)


@pytest.mark.parametrize("parts", [(), "x", ((0, b""),), ((-1, b"x"),), ((MAX_DETECTION_BYTES, b"x"),), ((0, "x"),), ((True, b"x"),), ((0,),)], ids=range(8))
def test_a_signature_must_fit_the_detection_window(parts):
    with pytest.raises(FormatError, match="^invalid_signature$"):
        FormatSignature(parts)


def test_a_signature_needs_every_part():
    signature = FormatSignature(((0, b"AB"), (4, b"CD")))
    assert signature.matches(b"ABxxCD") and not signature.matches(b"ABxxCE") and not signature.matches(b"AB")


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_are_positive_integers_or_unknown(value):
    with pytest.raises(FormatError, match="^invalid_limit$"):
        FormatLimits(max_source_bytes=value)
    assert FormatLimits().max_documents is None


# Negotiation

def requirements(*fidelity, operations=(), scopes=frozenset(), experimental=False):
    return FormatRequirements(operations, tuple(FidelityRequirement(aspect, level) for aspect, level in fidelity), scopes, experimental)


@pytest.mark.parametrize("descriptor,needed,code", [
    (notes_descriptor(), requirements(operations=(FormatOperation.RECONSTRUCT,)), "unsupported_operation"),
    (cards_descriptor(), requirements(operations=(FormatOperation.PREVIEW,)), "experimental_not_allowed"),
    (cards_descriptor(), requirements(operations=(FormatOperation.PREVIEW,), experimental=True), None),
    (cards_descriptor(), requirements(operations=(FormatOperation.RECONSTRUCT,)), None),
    (notes_descriptor(), requirements(scopes=frozenset({ContentScope.NAVIGATION})), "unsupported_scope"),
    (cards_descriptor(), requirements(scopes=frozenset({ContentScope.METADATA})), None),
    (cards_descriptor(), requirements((FidelityAspect.LINKS, FidelityLevel.BEST_EFFORT)), "unknown_fidelity"),
    (cards_descriptor(), requirements((FidelityAspect.STYLES, FidelityLevel.BEST_EFFORT)), "insufficient_fidelity"),
    (cards_descriptor(), requirements((FidelityAspect.METADATA, FidelityLevel.PRESERVED)), "insufficient_fidelity"),
    (cards_descriptor(), requirements((FidelityAspect.METADATA, FidelityLevel.BEST_EFFORT)), None),
    (cards_descriptor(), requirements((FidelityAspect.STRUCTURE, FidelityLevel.PRESERVED)), None),
    (cards_descriptor(), requirements((FidelityAspect.IMAGES, FidelityLevel.PRESERVED)), None),
    (cards_descriptor(), requirements(), None),
], ids=range(13))
def test_negotiation_matrix(descriptor, needed, code):
    result = negotiate_format(descriptor, needed)
    assert (result.accepted, result.code) == (code is None, code)


def test_an_operation_of_unknown_support_is_refused():
    claims = tuple(replace(claim, support=OperationSupport.UNKNOWN, evidence=()) if claim.operation is FormatOperation.VALIDATE else claim
                   for claim in notes_descriptor().operations)
    assert negotiate_format(notes_descriptor(operations=claims), requirements(operations=(FormatOperation.VALIDATE,))).code == "unknown_operation"


def test_maturity_never_changes_a_decision():
    needed = requirements(operations=(FormatOperation.RECONSTRUCT,))
    assert negotiate_format(cards_descriptor(maturity=FormatMaturity.EXPERIMENTAL), needed).accepted
    assert not negotiate_format(notes_descriptor(maturity=FormatMaturity.STABLE), needed).accepted


@pytest.mark.parametrize("minimum", [FidelityLevel.LOST, FidelityLevel.UNKNOWN, FidelityLevel.NOT_APPLICABLE, "preserved"])
def test_a_requirement_can_only_ask_for_something_real(minimum):
    with pytest.raises(FormatError, match="^invalid_requirement$"):
        FidelityRequirement(FidelityAspect.IMAGES, minimum)


def test_a_generic_consumer_decides_from_descriptors_alone():
    def can_round_trip(descriptor):
        return negotiate_format(descriptor, requirements(operations=(FormatOperation.RECONSTRUCT,))).accepted

    registry = registry_with((NOTES_REGISTRATION, notes_adapter()), (CARDS_REGISTRATION, cards_adapter()))
    assert {item.adapter_id: can_round_trip(item) for item in registry.descriptors()} == {"fake_cards": True, "fake_notes": False}


# Registry

def test_descriptors_are_listed_in_a_fixed_order():
    registry = registry_with((NOTES_REGISTRATION, notes_adapter()), (CARDS_REGISTRATION, cards_adapter()))
    assert [item.adapter_id for item in registry.descriptors()] == ["fake_cards", "fake_notes"]


@pytest.mark.parametrize("registration,code", [
    (CARDS_REGISTRATION, "duplicate_adapter"),
    (replace(CARDS_REGISTRATION, adapter_id="other"), "incompatible_adapter"),
    (replace(CARDS_REGISTRATION, adapter_version=9), "incompatible_adapter"),
    (replace(CARDS_REGISTRATION, contract_version=9), "incompatible_adapter"),
    (replace(CARDS_REGISTRATION, locator_schema="other"), "incompatible_locator"),
    (replace(CARDS_REGISTRATION, locator_version=9), "incompatible_locator"),
    (replace(CARDS_REGISTRATION, adapter_id="Bad Id"), "invalid_registration"),
    ("cards", "invalid_registration"),
], ids=range(8))
def test_registration_conflicts_are_distinct(registration, code):
    registry = registry_with((CARDS_REGISTRATION, cards_adapter())) if code == "duplicate_adapter" else FormatRegistry()
    with pytest.raises(FormatError, match=f"^{code}$"):
        registry.register(registration, cards_adapter())
    assert len(registry.descriptors()) == (1 if code == "duplicate_adapter" else 0)


def test_a_descriptor_from_another_contract_version_is_refused():
    descriptor = cards_descriptor(contract_version=2)
    ports = CardsPorts()
    adapter = FormatAdapter(descriptor, inspector=ports, extractor=ports, previewer=ports, reconstructor=ports, validator=ports)
    with pytest.raises(FormatError, match="^incompatible_adapter$"):
        FormatRegistry().register(replace(CARDS_REGISTRATION, contract_version=2), adapter)


def test_an_unknown_adapter_is_distinct():
    registry = FormatRegistry()
    for value in ("absent", None, 7):
        with pytest.raises(FormatError, match="^unknown_adapter$"):
            registry.get(value)
    assert FormatService(registry).inspect("absent", None).failure.code == "unknown_adapter"


def test_stored_documents_resolve_only_to_a_compatible_adapter(cards):
    document = documents_of(cards)[0]
    assert registry_with((CARDS_REGISTRATION, cards_adapter())).for_document(document).descriptor.adapter_id == "fake_cards"
    with pytest.raises(FormatError, match="^unknown_adapter$"):
        FormatRegistry().for_document(document)
    ports = CardsPorts()

    def adapter(**changes):
        return FormatAdapter(cards_descriptor(**changes), inspector=ports, extractor=ports, previewer=ports, reconstructor=ports, validator=ports)

    upgraded = registry_with((replace(CARDS_REGISTRATION, adapter_version=3), adapter(adapter=AdapterIdentity("fake_cards", 3, 1))))
    with pytest.raises(FormatError, match="^incompatible_adapter$"):
        upgraded.for_document(document)
    resegmented = registry_with((CARDS_REGISTRATION, adapter(adapter=AdapterIdentity("fake_cards", 2, 2))))
    with pytest.raises(FormatError, match="^incompatible_adapter$"):
        resegmented.for_document(document)
    relocated = registry_with((replace(CARDS_REGISTRATION, locator_version=4), adapter(locator_version=4)))
    with pytest.raises(FormatError, match="^incompatible_locator$"):
        relocated.for_document(document)


# The port and its claims

@pytest.mark.parametrize("build", [
    lambda: FormatAdapter(notes_descriptor(), inspector=NotesPorts()),
    lambda: FormatAdapter(notes_descriptor(), inspector=NotesPorts(), extractor=NotesPorts(), reconstructor=CardsPorts()),
    lambda: FormatAdapter(notes_descriptor(), inspector=NotesPorts(), extractor=object()),
    lambda: FormatAdapter(notes_descriptor(), inspector=CardsPorts(), extractor=NotesPorts(), validator=CardsPorts()),
], ids=["claim-without-port", "port-without-claim", "port-of-the-wrong-shape", "extra-validator"])
def test_a_descriptor_and_its_ports_must_agree(build):
    with pytest.raises(FormatError, match="^descriptor_mismatch$"):
        build()


def test_an_adapter_needs_a_descriptor():
    with pytest.raises(FormatError, match="^invalid_adapter$"):
        FormatAdapter("cards")


def with_method(name, method):
    class Ports(CardsPorts):
        pass
    setattr(Ports, name, method)
    return Ports()


@pytest.mark.parametrize("returned", [
    "a document", ["a", "list"], ("not a document",),
], ids=["string", "list", "tuple-of-string"])
def test_an_extractor_returning_something_else_is_a_mismatch(cards, returned):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", lambda self, context, request: returned),
                            previewer=base, reconstructor=base, validator=base)
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure.code == "descriptor_mismatch"


def extractor_returning(make):
    base = CardsPorts()
    return FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", lambda self, context, request: make(context, request)),
                         previewer=base, reconstructor=base, validator=base)


def fake_document(context, request, **changes):
    values = dict(project_id=request.project_id, logical_key="one", adapter=cards_descriptor().adapter, schema="fake_card", version=3,
                  fingerprint=context.source_fingerprint)
    values.update(changes)
    return build_document(values["project_id"], values["logical_key"], values["adapter"], request.source_locale,
                          [UnitContent(Locator(values["schema"], values["version"], "1"), "text", KIND, (1,))], source_fingerprint=values["fingerprint"])


@pytest.mark.parametrize("changes", [
    {"adapter": AdapterIdentity("other", 2, 1)}, {"adapter": AdapterIdentity("fake_cards", 1, 1)}, {"schema": "other"}, {"version": 9},
    {"fingerprint": "sha256:" + "0" * 64}, {"fingerprint": None}, {"project_id": "00000000-0000-4000-8000-000000000999"},
], ids=lambda value: next(iter(value)))
def test_a_document_that_contradicts_the_descriptor_is_a_mismatch(cards, changes):
    adapter = extractor_returning(lambda context, request: (fake_document(context, request, **changes),))
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure.code == "descriptor_mismatch"


def test_duplicate_documents_are_a_mismatch(cards):
    adapter = extractor_returning(lambda context, request: (fake_document(context, request), fake_document(context, request)))
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure.code == "descriptor_mismatch"


def test_returning_what_was_not_selected_is_a_mismatch(cards):
    adapter = extractor_returning(lambda context, request: (fake_document(context, request),))
    request = ExtractionRequest(PROJECT, selection=("two",))
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), request).failure.code == "descriptor_mismatch"


@pytest.mark.parametrize("returned", ["inventory", FormatInventory(0, 0, format_version="9")], ids=["type", "undeclared-version"])
def test_an_inventory_that_contradicts_the_descriptor_is_a_mismatch(cards, returned):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=with_method("inspect", lambda self, context: returned), extractor=base,
                            previewer=base, reconstructor=base, validator=base)
    assert service_for(adapter).inspect("fake_cards", LocalFormatSource(cards)).failure.code == "descriptor_mismatch"


def test_findings_of_the_wrong_type_are_a_mismatch(cards):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=base,
                            validator=with_method("validate", lambda self, context: ["error"]))
    assert service_for(adapter).validate("fake_cards", LocalFormatSource(cards)).failure.code == "descriptor_mismatch"


def test_validation_separates_errors_from_warnings(tmp_path, cards):
    assert service_for().validate("fake_cards", LocalFormatSource(cards)).ok
    empty = tmp_path / "empty.cards"
    empty.write_bytes(CARDS_HEADER)
    warned = service_for().validate("fake_cards", LocalFormatSource(empty))
    assert warned.ok and [item.severity for item in warned.findings] == [FindingSeverity.WARNING]
    broken = tmp_path / "broken.cards"
    broken.write_bytes(b"nothing")
    assert not service_for().validate("fake_cards", LocalFormatSource(broken)).ok


def test_an_undeclared_safe_point_fails_the_operation(cards):
    def extract(self, context, request):
        context.checkpoint("never_declared")
        return ()

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", extract), previewer=base, reconstructor=base, validator=base)
    result = service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTERNAL, "undeclared_safe_point")


def test_a_safe_point_of_another_operation_is_undeclared(cards):
    def extract(self, context, request):
        context.checkpoint("cards_before_render")
        return ()

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", extract), previewer=base, reconstructor=base, validator=base)
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure.code == "undeclared_safe_point"


def test_a_token_that_never_declared_the_safe_point_is_a_failure(cards):
    from ayvu.job_execution import CancellationSource

    token = CancellationSource(("something_else",)).token
    result = service_for().extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT), cancellation=token)
    assert result.failure.code == "undeclared_safe_point"


def test_an_expected_failure_from_an_adapter_is_kept(cards):
    failure = JobFailure(JobFailureFamily.INPUT, "corrupt_container")

    def extract(self, context, request):
        raise FormatOperationError(failure)

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", extract), previewer=base, reconstructor=base, validator=base)
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure is failure


def test_an_adapter_error_is_reduced_to_a_code_without_its_message(cards, caplog):
    caplog.set_level(logging.DEBUG)

    def extract(self, context, request):
        raise RuntimeError("Call me Ishmael. /home/reader/book.epub hunter2")

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", extract), previewer=base, reconstructor=base, validator=base)
    result = service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTERNAL, "adapter_failed")
    assert "Ishmael" not in repr(result) and "hunter2" not in caplog.text


def test_interrupts_are_not_swallowed(cards):
    def extract(self, context, request):
        raise KeyboardInterrupt

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", extract), previewer=base, reconstructor=base, validator=base)
    with pytest.raises(KeyboardInterrupt):
        service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT))


def test_an_adapter_that_swallows_cancellation_still_returns_nothing(cards):
    from format_contract import TriggerToken
    from ayvu.format_service import format_safe_points

    def extract(self, context, request):
        try:
            context.checkpoint("cards_between_documents")
        except FormatCancelledError:
            pass
        return ()

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", extract), previewer=base, reconstructor=base, validator=base)
    token = TriggerToken(format_safe_points(cards_descriptor()), "cards_between_documents")
    result = service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT), cancellation=token)
    assert result.documents is None and result.failure.family is JobFailureFamily.CANCELLED


def test_the_source_limit_stops_before_reading_content(tmp_path, cards):
    source = CountingSource(LocalFormatSource(cards))
    base = NotesPorts()
    adapter = FormatAdapter(notes_descriptor(limits=FormatLimits(max_source_bytes=4)), inspector=base, extractor=base)
    result = FormatService(registry_with((NOTES_REGISTRATION, adapter))).inspect("fake_notes", source)
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.RESOURCE, "source_too_large")
    assert source.reads == 1  # Only the size.


def test_the_document_limit_is_enforced(cards):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(limits=FormatLimits(max_documents=1)), inspector=base, extractor=base, previewer=base, reconstructor=base, validator=base)
    result = service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.RESOURCE, "too_many_documents")


def test_an_unsupported_scope_stops_before_reading(cards):
    source = CountingSource(LocalFormatSource(cards))
    request = ExtractionRequest(PROJECT, frozenset({ContentScope.IMAGE_ALT_TEXT}))
    assert service_for().extract("fake_cards", source, request).failure.code == "unsupported_scope"
    assert source.reads == 0


def test_an_unreadable_source_is_a_resource_failure(tmp_path):
    source = LocalFormatSource(tmp_path / "gone.cards")
    result = service_for().inspect("fake_cards", source)
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.RESOURCE, "source_unreadable")
    assert service_for().detect(source).reason == "source_unreadable"


def test_a_source_with_a_malformed_fingerprint_is_refused(cards):
    class Odd(CountingSource):
        def fingerprint(self):
            return "not a digest"

    assert service_for().inspect("fake_cards", Odd(LocalFormatSource(cards))).failure.code == "source_unreadable"


# Materializing

def materialize(tmp_path, cards, adapter=None, *, kind=ReconstructionRequest, guard=None, translations=None, requirements_=None, source=None):
    env = FormatEnv(tmp_path / "env")
    documents = documents_of(cards)
    request = kind(documents, complete(documents) if translations is None else translations, requirements_ or FormatRequirements(allow_experimental=True))
    publication = env.publication(guard)
    method = service_for(adapter).reconstruct if kind is ReconstructionRequest else service_for(adapter).preview
    return method("fake_cards", source or LocalFormatSource(cards), request, publication), env, publication


@pytest.fixture
def envdir(tmp_path):
    return tmp_path


def test_a_preview_can_hold_partial_results_and_a_reconstruction_cannot(envdir, cards):
    documents = documents_of(cards)
    some = complete(documents)[:1]
    previewed, env, publication = materialize(envdir, cards, kind=PreviewRequest, translations=(UnitTranslation(some[0].unit_id, "TRANSLATED"),))
    assert previewed.artifact is not None
    assert b"TRANSLATED" in env.read(publication) and b"second card" in env.read(publication)


def test_a_preview_is_refused_when_experimental_support_was_not_accepted(envdir, cards):
    result, env, publication = materialize(envdir, cards, kind=PreviewRequest, requirements_=FormatRequirements())
    assert result.failure.code == "experimental_not_allowed" and env.publisher.calls == 0


def test_required_fidelity_is_checked_before_reading_or_publishing(envdir, cards):
    source = CountingSource(LocalFormatSource(cards))
    needed = FormatRequirements(fidelity=(FidelityRequirement(FidelityAspect.LINKS, FidelityLevel.BEST_EFFORT),))
    result, env, _ = materialize(envdir, cards, requirements_=needed, source=source)
    assert result.failure.code == "unknown_fidelity" and source.reads == 0 and env.publisher.calls == 0
    needed = FormatRequirements(fidelity=(FidelityRequirement(FidelityAspect.STYLES, FidelityLevel.BEST_EFFORT),))
    result, env, _ = materialize(envdir / "env", cards, requirements_=needed, source=source)
    assert result.failure.code == "insufficient_fidelity" and source.reads == 0


@pytest.mark.parametrize("guard,code", [
    (FixedGuard("yes"), "output_alias_unknown"), (None, None),
], ids=["garbage-verdict", "fine"])
def test_only_an_explicit_distinct_verdict_lets_work_start(envdir, cards, guard, code):
    result, _, _ = materialize(envdir, cards, guard=guard)
    assert (result.failure.code if result.failure else None) == code


def test_a_guard_that_breaks_blocks_the_work(envdir, cards):
    class Broken:
        def verdict(self, source):
            raise RuntimeError("hunter2")

    result, env, _ = materialize(envdir, cards, guard=Broken())
    assert result.failure.code == "output_alias_unknown" and env.publisher.calls == 0 and "hunter2" not in repr(result)


def test_the_guard_runs_before_the_source_is_read(envdir, cards):
    source = CountingSource(LocalFormatSource(cards))
    result, _, _ = materialize(envdir, cards, guard=FixedGuard(AliasVerdict.SAME), source=source)
    assert result.failure.code == "output_is_source" and source.reads == 0


def test_a_request_of_the_wrong_kind_is_refused(cards, envdir):
    env = FormatEnv(envdir / "env")
    documents = documents_of(cards)
    service = service_for()
    assert service.reconstruct("fake_cards", LocalFormatSource(cards), PreviewRequest(documents, ()), env.publication()).failure.code == "invalid_materialization_request"
    assert service.preview("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, ()), env.publication()).failure.code == "invalid_materialization_request"
    assert service.reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)), "here").failure.code == "invalid_publication"
    assert service.extract("fake_cards", LocalFormatSource(cards), "all").failure.code == "invalid_extraction_request"


def test_documents_from_another_adapter_are_refused(envdir, tmp_path):
    notes_path = tmp_path / "x.notes"
    notes_path.write_bytes(b"a note\n")
    notes = FormatService(registry_with((NOTES_REGISTRATION, notes_adapter()))).extract("fake_notes", LocalFormatSource(notes_path), ExtractionRequest(PROJECT)).documents
    env = FormatEnv(envdir / "env")
    result = service_for().reconstruct("fake_cards", LocalFormatSource(notes_path), ReconstructionRequest(notes, complete(notes)), env.publication())
    assert result.failure.code == "incompatible_adapter" and env.publisher.calls == 0


def test_documents_with_another_locator_are_refused(envdir, cards):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(locator_version=4), inspector=base, extractor=base, previewer=base, reconstructor=base, validator=base)
    env = FormatEnv(envdir / "env")
    documents = documents_of(cards)
    service = FormatService(registry_with((replace(CARDS_REGISTRATION, locator_version=4), adapter)))
    result = service.reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)), env.publication())
    assert result.failure.code == "incompatible_locator" and env.publisher.calls == 0


@pytest.mark.parametrize("name,method", [
    ("reconstruct", lambda self, context, request: iter(["text, not bytes"])),
    ("reconstruct", lambda self, context, request: b"bytes iterate as integers"),
    ("reconstruct", lambda self, context, request: 7),
], ids=["str-chunk", "bytes-object", "not-iterable"])
def test_output_of_the_wrong_type_never_becomes_an_artifact(envdir, cards, name, method):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=with_method(name, method), validator=base)
    result, env, publication = materialize(envdir, cards, adapter)
    assert result.failure.code in ("malformed_output", "adapter_failed") and result.artifact is None and not env.is_ready(publication)


def test_an_adapter_failing_midway_leaves_no_artifact_and_keeps_its_reason(envdir, cards):
    failure = JobFailure(JobFailureFamily.INPUT, "corrupt_container")

    def reconstruct(self, context, request):
        yield b"partial"
        raise FormatOperationError(failure)

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=with_method("reconstruct", reconstruct), validator=base)
    result, env, publication = materialize(envdir, cards, adapter)
    assert result.failure is failure and not env.is_ready(publication)


def test_an_adapter_crashing_midway_is_reduced_to_a_code(envdir, cards):
    def reconstruct(self, context, request):
        yield b"partial"
        raise RuntimeError("Call me Ishmael.")

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=with_method("reconstruct", reconstruct), validator=base)
    result, env, publication = materialize(envdir, cards, adapter)
    assert result.failure.code == "adapter_failed" and "Ishmael" not in repr(result) and not env.is_ready(publication)


class BrokenPublisher:
    def __init__(self, returned=None, error=None):
        self.returned, self.error = returned, error

    def publish(self, request, chunks, **options):
        list(chunks)
        if self.error:
            raise self.error
        return self.returned


def publish_with(tmp_path, cards, publisher):
    env = FormatEnv(tmp_path / "env")
    documents = documents_of(cards)
    return service_for().reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)),
                                     replace(env.publication(), artifacts=publisher))


def test_publication_failures_are_typed(envdir, cards):
    from ayvu.artifact import ArtifactIntegrityError, ArtifactStoreError

    assert publish_with(envdir, cards, BrokenPublisher(error=ArtifactStoreError("disk_full"))).failure.code == "disk_full"
    failed = publish_with(envdir / "env", cards, BrokenPublisher(error=ArtifactIntegrityError("digest_mismatch")))
    assert (failed.failure.family, failed.failure.code) == (JobFailureFamily.INTEGRITY, "digest_mismatch")
    message = publish_with(envdir / "env" / "env", cards, BrokenPublisher(error=RuntimeError("hunter2")))
    assert message.failure.code == "publication_failed" and "hunter2" not in repr(message)


def test_an_artifact_that_is_not_ready_is_not_success(envdir, cards):
    assert publish_with(envdir, cards, BrokenPublisher(returned="ready")).failure.code == "publication_failed"


def test_the_artifact_lease_is_still_enforced(envdir, cards):
    env = FormatEnv(envdir / "env")
    documents = documents_of(cards)
    publication = replace(env.publication(), owner_token="0" * 64)
    result = service_for().reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)), publication)
    assert result.artifact is None and not env.is_ready(publication)
    assert result.failure.family is JobFailureFamily.RESOURCE


# Detection

def detector(*pairs):
    return registry_with(*pairs)


def signed(name, signature=b"SAME", extensions=(".signed",)):
    base = CardsPorts()
    return (FormatRegistration(name, 2, "fake_card", 3),
            FormatAdapter(cards_descriptor(adapter=AdapterIdentity(name, 2, 1), extensions=extensions, signatures=(FormatSignature(((0, signature),)),)),
                          inspector=base, extractor=base, previewer=base, reconstructor=base, validator=base))


def named(name, extensions=(".plain",), media_types=()):
    base = NotesPorts()
    return (FormatRegistration(name, 1, "fake_line", 1),
            FormatAdapter(notes_descriptor(adapter=AdapterIdentity(name, 1, 1), extensions=extensions, media_types=media_types), inspector=base, extractor=base))


@pytest.mark.parametrize("adapters,evidence,outcome,reason,candidates", [
    ([], DetectionEvidence(b"x"), DetectionOutcome.NO_MATCH, "no_candidate", ()),
    ([signed("a", b"AAA")], DetectionEvidence(b"AAA...", ".other"), DetectionOutcome.MATCH, "signature_match", ("a",)),
    ([signed("a", b"AAA")], DetectionEvidence(b"BBB", ".signed"), DetectionOutcome.NO_MATCH, "signature_conflict", ()),
    ([signed("a", b"AAA")], DetectionEvidence(b"", ".signed"), DetectionOutcome.NO_MATCH, "signature_conflict", ()),
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

def test_a_local_source_reads_and_fingerprints(cards):
    source = LocalFormatSource(cards)
    assert source.size() == len(CARDS) and source.head(6) == b"CARDS1" and source.extension == ".cards"
    assert source.fingerprint().startswith("sha256:") and len(source.fingerprint()) == 71
    with source.open() as stream:
        assert stream.read() == CARDS


def test_a_local_source_cannot_be_written_through(cards):
    with LocalFormatSource(cards).open() as stream:
        with pytest.raises(OSError):
            stream.write(b"x")
    assert cards.read_bytes() == CARDS


@pytest.mark.parametrize("name,extension", [
    ("book.EPUB", ".epub"), ("book", None), ("book.", None), (".hidden", None), ("a.we ird", None), ("a.açb", None), ("a." + "x" * 17, None),
])
def test_only_a_plain_extension_is_evidence(tmp_path, name, extension):
    assert LocalFormatSource(tmp_path / name).extension == extension


def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe.cards"
    os.mkfifo(fifo)
    source = LocalFormatSource(fifo)
    with pytest.raises(FormatError, match="^source_not_regular_file$"):
        source.open()
    assert service_for().inspect("fake_cards", source).failure.code == "source_unreadable"


def test_a_directory_is_refused(tmp_path):
    with pytest.raises(FormatError):
        LocalFormatSource(tmp_path).fingerprint()


def test_a_missing_source_reports_a_code(tmp_path):
    with pytest.raises(FormatError, match="^source_unreadable$"):
        LocalFormatSource(tmp_path / "gone").open()


def test_the_guard_accepts_a_new_or_different_file(tmp_path, cards):
    other = tmp_path / "other"
    other.write_bytes(CARDS)  # Equal content is not the same file.
    source = LocalFormatSource(cards)
    assert LocalOutputGuard(other).verdict(source) is AliasVerdict.DISTINCT
    assert LocalOutputGuard(tmp_path / "new.cards").verdict(source) is AliasVerdict.DISTINCT


def test_the_guard_recognizes_a_chain_of_links(tmp_path, cards):
    first, second = tmp_path / "first", tmp_path / "second"
    first.symlink_to(cards)
    second.symlink_to(first)
    assert LocalOutputGuard(second).verdict(LocalFormatSource(cards)) is AliasVerdict.SAME
    assert LocalOutputGuard(cards).verdict(LocalFormatSource(second)) is AliasVerdict.SAME


def test_the_guard_recognizes_a_linked_directory(tmp_path, cards):
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    assert LocalOutputGuard(linked / cards.name).verdict(LocalFormatSource(cards)) is AliasVerdict.SAME


@pytest.mark.parametrize("kind", ["dangling", "loop", "under_a_file", "unknown_source", "missing_source"])
def test_the_guard_says_unknown_when_it_cannot_tell(tmp_path, cards, kind):
    source = LocalFormatSource(cards)
    destination = tmp_path / "destination"
    if kind == "dangling":
        destination.symlink_to(tmp_path / "nowhere")
    elif kind == "loop":
        destination.symlink_to(destination)
    elif kind == "under_a_file":
        destination = cards / "child"
    elif kind == "unknown_source":
        source = CountingSource(source)
    else:
        source = LocalFormatSource(tmp_path / "gone")
    assert LocalOutputGuard(destination).verdict(source) is AliasVerdict.UNKNOWN


# Values

@pytest.mark.parametrize("kwargs,code", [
    ({"project_id": None}, "invalid_extraction_request"), ({"project_id": PROJECT, "scopes": frozenset()}, "invalid_extraction_request"),
    ({"project_id": PROJECT, "scopes": ("body",)}, "invalid_extraction_request"), ({"project_id": PROJECT, "selection": ()}, "invalid_selection"),
    ({"project_id": PROJECT, "selection": ("a", "a")}, "invalid_selection"), ({"project_id": PROJECT, "selection": ("",)}, "invalid_selection"),
    ({"project_id": PROJECT, "selection": ["a"]}, "invalid_selection"), ({"project_id": PROJECT, "allow_experimental": 1}, "invalid_extraction_request"),
], ids=range(8))
def test_extraction_requests_are_validated(kwargs, code):
    with pytest.raises(FormatError, match=f"^{code}$"):
        ExtractionRequest(**kwargs)


@pytest.mark.parametrize("unit_id,text", [("", "x"), (None, "x"), ("u", ""), ("u", None), ("u", "\ud800")], ids=range(5))
def test_translations_are_validated(unit_id, text):
    with pytest.raises(FormatError, match="^invalid_translation$"):
        UnitTranslation(unit_id, text)


def test_materialization_requests_are_validated(cards):
    documents = documents_of(cards)
    good = complete(documents)
    with pytest.raises(FormatError, match="^invalid_materialization_request$"):
        ReconstructionRequest((), good)
    with pytest.raises(FormatError, match="^invalid_materialization_request$"):
        ReconstructionRequest(documents, list(good))
    with pytest.raises(FormatError, match="^invalid_materialization_request$"):
        ReconstructionRequest((*documents, documents[0]), good)
    with pytest.raises(FormatError, match="^invalid_materialization_request$"):
        ReconstructionRequest(documents, good, "all")
    with pytest.raises(FormatError, match="^duplicate_translation$"):
        ReconstructionRequest(documents, (good[0], good[0]))


@pytest.mark.parametrize("kwargs", [
    {"document_count": -1, "resource_count": 0}, {"document_count": 0, "resource_count": True}, {"document_count": 0, "resource_count": 0, "title": 1},
    {"document_count": 0, "resource_count": 0, "authors": ["x"]}, {"document_count": 0, "resource_count": 0, "language": 1},
], ids=range(5))
def test_inventories_are_validated(kwargs):
    with pytest.raises(FormatError, match="^invalid_inventory$"):
        FormatInventory(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"severity": "error", "code": "x"}, {"severity": FindingSeverity.ERROR, "code": "Bad Code"}, {"severity": FindingSeverity.ERROR, "code": "x", "count": 0},
], ids=range(3))
def test_findings_are_validated(kwargs):
    with pytest.raises(FormatError, match="^invalid_finding$"):
        ValidationFinding(**kwargs)


@pytest.mark.parametrize("kind,field", [
    (InspectionResult, "inventory"), (ExtractionResult, "documents"), (MaterializationResult, "artifact"), (ValidationResult, "findings"),
])
def test_a_result_holds_a_value_or_a_failure_and_never_both(kind, field):
    failure = JobFailure(JobFailureFamily.INPUT, "x")
    with pytest.raises(FormatError, match="^invalid_result$"):
        kind()
    with pytest.raises(FormatError, match="^invalid_result$"):
        kind(**{field: object()}, failure=failure)
    with pytest.raises(FormatError, match="^invalid_result$"):
        kind(failure="x")
    assert kind(failure=failure).failure is failure


def test_a_failed_validation_is_not_ok():
    assert not ValidationResult(failure=JobFailure(JobFailureFamily.INPUT, "x")).ok
    assert ValidationResult(findings=()).ok


def test_an_operation_error_needs_a_real_failure():
    with pytest.raises(FormatError, match="^invalid_failure$"):
        FormatOperationError("x")


def test_text_and_paths_stay_out_of_representations(cards):
    secret = "Call me Ishmael"
    documents = documents_of(cards)
    values = [UnitTranslation("u", secret), DetectionEvidence(secret.encode()), FormatInventory(1, 0, title=secret, authors=(secret,)),
              ExtractionRequest(PROJECT, selection=(secret,)), LocalFormatSource(f"/private/{secret}.cards"), LocalOutputGuard(f"/private/{secret}"),
              ReconstructionRequest(documents, (UnitTranslation(complete(documents)[0].unit_id, secret),))]
    for value in values:
        assert secret not in repr(value) and "private" not in repr(value)
    assert CARDS.decode() not in repr(documents)


# Boundaries

CONTRACT_MODULES = ("format_adapter", "format_service")


def test_the_contract_imports_nothing_concrete():
    forbidden = {"ebooklib", "bs4", "lxml", "typer", "rich", "PySide6", "requests", "mcp", "sqlite3", "os", "shutil", "zipfile",
                 "ayvu.translator", "ayvu.provider", "ayvu.provider_service", "ayvu.translation_pipeline", "ayvu.translation_document",
                 "ayvu.artifact_store", "ayvu.job_store", "ayvu.cache", "ayvu.glossary", "ayvu.html_translate", "ayvu.epub_io", "ayvu.format_local"}
    for name in CONTRACT_MODULES:
        tree = ast.parse((ROOT / "src" / "ayvu" / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                module = f"ayvu.{node.module}" if node.level else node.module
                found = {module}
            else:
                continue
            assert not found & forbidden, (name, found)


def test_the_contract_never_branches_on_a_format_or_a_missing_method():
    for name in CONTRACT_MODULES:
        source = (ROOT / "src" / "ayvu" / f"{name}.py").read_text()
        assert "hasattr" not in source and "fake_" not in source
        assert not re.search(r"epub|srt|webvtt|\.txt|\.pdf", source, re.IGNORECASE), name


def test_getattr_in_the_service_only_reads_declared_port_fields():
    tree = ast.parse((ROOT / "src" / "ayvu" / "format_service.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr"]
    assert [call.args[1].id for call in calls] == ["field"]


# The compatibility map

MAP = ROOT / "docs" / "architecture" / "format-compatibility.md"
REQUIRED_ROWS = ("inspect", "language detection", "extract", "preview", "chapter selection", "metadata", "navigation", "image alt text", "review export", "review apply",
                 "rebuild", "validation", "archive preservation", "mimetype storage", "visible text selection", "html reconstruction", "jobs", "resume", "output path", "translation")


def map_rows():
    rows = {}
    for line in MAP.read_text().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and not set(cells[0]) <= {"-", " "} and cells[0] != "Behavior":
            rows[cells[0]] = cells[1:]
    return rows


def resolve(reference):
    parts = reference.split(".")
    for split in range(len(parts), 1, -1):
        try:
            value = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for part in parts[split:]:
            value = getattr(value, part)
        return value
    raise ImportError(reference)


def test_the_map_covers_every_required_behavior():
    assert set(REQUIRED_ROWS) <= set(map_rows())


def test_every_symbol_in_the_map_exists():
    for name, (current, owner, _note) in map_rows().items():
        for reference in re.findall(r"`(ayvu\.[A-Za-z0-9_.]+)`", current + owner):
            assert resolve(reference) is not None, (name, reference)


def test_every_behavior_has_a_current_code_and_a_concrete_owner():
    for name, (current, owner, _note) in map_rows().items():
        assert re.search(r"`ayvu\.", current), name
        assert re.search(r"`ayvu\.|#\d+", owner), name


def test_jobs_and_resume_are_not_assigned_to_the_format_contract():
    rows = map_rows()
    for name in ("jobs", "resume", "output path", "translation"):
        assert "format_" not in rows[name][1], name
