from dataclasses import replace

import pytest

from ayvu.content.model import AdapterIdentity
from ayvu.formats.contracts import FormatError
from ayvu.formats.service import FormatAdapter, FormatRegistry, FormatService
from tests.support.format_fakes import CARDS_REGISTRATION, NOTES_REGISTRATION, CardsPorts, NotesPorts, cards_adapter, cards_descriptor, notes_adapter, notes_descriptor

from tests.formats.format_adapter_support import cards, documents_of
from tests.formats.format_adapter_support import registry_with


def test_descriptors_are_listed_in_a_fixed_order():
    registry = registry_with((NOTES_REGISTRATION, notes_adapter()), (CARDS_REGISTRATION, cards_adapter()))
    assert [item.adapter_id for item in registry.descriptors()] == ["fake_cards", "fake_notes"]


@pytest.mark.parametrize("registration,code", [
    (CARDS_REGISTRATION, "duplicate_adapter"),
    (replace(CARDS_REGISTRATION, adapter_id="other"), "incompatible_adapter"),
    (replace(CARDS_REGISTRATION, adapter_version=9), "incompatible_adapter"),
    (replace(CARDS_REGISTRATION, contract_version=9), "incompatible_adapter"),
    (replace(CARDS_REGISTRATION, locators=(("other", 3),)), "incompatible_locator"),
    (replace(CARDS_REGISTRATION, locators=(("fake_card", 9),)), "incompatible_locator"),
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
    relocated = registry_with((replace(CARDS_REGISTRATION, locators=(("fake_card", 4),)), adapter(locators=(("fake_card", 4),))))
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
