import logging
from dataclasses import replace

import pytest

from ayvu.content.model import AdapterIdentity
from ayvu.formats.contracts import ContentScope, ExtractionRequest, FindingSeverity, FormatCancelledError, FormatError, FormatInventory, FormatLimits, FormatOperationError, InventoryDocument
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.service import FormatAdapter, FormatRegistry, FormatService
from tests.support.format_contract_env import PROJECT, CountingSource
from tests.support.format_fakes import CARDS_HEADER, CARDS_REGISTRATION, NOTES_REGISTRATION, CardsPorts, NotesPorts, cards_descriptor, notes_descriptor
from ayvu.kernel import JobFailure, JobFailureFamily

from tests.formats.format_adapter_support import cards
from tests.formats.format_adapter_support import extractor_returning, fake_document
from tests.formats.format_adapter_support import registry_with, service_for
from tests.formats.format_adapter_support import with_method


@pytest.mark.parametrize("returned", [
    "a document", ["a", "list"], ("not a document",),
], ids=["string", "list", "tuple-of-string"])
def test_an_extractor_returning_something_else_is_a_mismatch(cards, returned):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", lambda self, context, request: returned),
                            previewer=base, reconstructor=base, validator=base)
    assert service_for(adapter).extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure.code == "descriptor_mismatch"


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


def test_a_format_can_place_content_in_more_than_one_way(cards):
    base = CardsPorts()
    two = (("fake_card", 3), ("fake_title", 1))
    adapter = FormatAdapter(cards_descriptor(locators=two), inspector=base, extractor=base, previewer=base, reconstructor=base, validator=base)
    service = service_for(adapter, replace(CARDS_REGISTRATION, locators=two))
    assert service.extract("fake_cards", LocalFormatSource(cards), ExtractionRequest(PROJECT)).failure is None
    with pytest.raises(FormatError, match="^incompatible_locator$"):
        FormatRegistry().register(CARDS_REGISTRATION, adapter)


@pytest.mark.parametrize("documents", [
    [InventoryDocument("a")], ("a",), (InventoryDocument("a"), InventoryDocument("a")), (InventoryDocument("a"),),
], ids=["list", "strings", "duplicate", "count-disagrees"])
def test_an_inventory_cannot_contradict_its_document_list(documents):
    with pytest.raises(FormatError, match="^invalid_inventory$"):
        FormatInventory(2, 0, documents=documents)


@pytest.mark.parametrize("kwargs", [{"logical_key": ""}, {"logical_key": None}, {"logical_key": "a", "title": 1}], ids=range(3))
def test_inventory_documents_are_validated(kwargs):
    with pytest.raises(FormatError, match="^invalid_inventory$"):
        InventoryDocument(**kwargs)


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
    from ayvu.jobs.execution import CancellationSource

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
    from tests.support.format_contract_env import TriggerToken
    from ayvu.formats.service import format_safe_points

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
