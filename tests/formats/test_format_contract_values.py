from dataclasses import replace

import pytest

from ayvu.formats.contracts import MAX_DETECTION_BYTES, ContentScope, DetectionEvidence, ExtractionRequest, ExtractionResult, FidelityAspect, FidelityClaim, FidelityLevel, FidelityRequirement, FindingSeverity, FormatError, FormatInventory, FormatLimits, FormatMaturity, FormatOperation, FormatOperationError, InventoryDocument, FormatSignature, InspectionResult, MaterializationResult, OperationClaim, OperationSupport, ReconstructionRequest, UnitTranslation, ValidationFinding, ValidationResult, negotiate_format
from ayvu.formats.local import LocalFormatSource, LocalOutputGuard
from tests.support.format_contract_env import PROJECT
from tests.support.format_fakes import CARDS_REGISTRATION, EVIDENCE, NOTES_REGISTRATION, all_fidelity, cards_adapter, cards_descriptor, notes_adapter, notes_descriptor, unsupported
from ayvu.kernel import JobFailure, JobFailureFamily

from tests.formats.format_adapter_support import CARDS
from tests.formats.format_adapter_support import cards, complete, documents_of
from tests.formats.format_adapter_support import registry_with, requirements


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
              *unsupported(FormatOperation.EXTRACT, FormatOperation.EXPORT_VISIBLE_CONTENT, FormatOperation.PREVIEW,
                          FormatOperation.RECONSTRUCT, FormatOperation.VALIDATE))
    with pytest.raises(FormatError, match="^contradictory_claim$"):
        notes_descriptor(operations=claims)


@pytest.mark.parametrize("changes", [
    {"adapter": "notes"}, {"locators": ()}, {"locators": ("x", 1)}, {"locators": (("Bad", 1),)}, {"locators": (("x", 0),)}, {"locators": (("x", True),)},
    {"locators": (("x", 1), ("x", 1))}, {"contract_version": 0},
    {"media_types": ("nope",)}, {"extensions": ("notes",)}, {"extensions": (".NOTES",)}, {"extensions": (".a", ".a")},
    {"format_versions": ("../x",)}, {"signatures": (b"x",)}, {"limits": 5}, {"maturity": "stable"}, {"content_scopes": frozenset({"body"})},
    {"operations": []}, {"fidelity": "all"}, {"extensions": (), "media_types": ()},
], ids=range(20))
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
    values = [UnitTranslation("u", secret), DetectionEvidence(secret.encode()), FormatInventory(1, 0, title=secret, authors=(secret,)), InventoryDocument(secret, secret),
              ExtractionRequest(PROJECT, selection=(secret,)), LocalFormatSource(f"/private/{secret}.cards"), LocalOutputGuard(f"/private/{secret}"),
              ReconstructionRequest(documents, (UnitTranslation(complete(documents)[0].unit_id, secret),))]
    for value in values:
        assert secret not in repr(value) and "private" not in repr(value)
    assert CARDS.decode() not in repr(documents)
