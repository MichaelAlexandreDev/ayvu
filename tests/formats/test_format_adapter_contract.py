
import pytest

from tests.support.format_contract_env import FormatHarness
from tests.support.format_contract import FormatAdapterContract
from tests.support.format_fakes import CARDS_HEADER, CARDS_REGISTRATION, NOTES_REGISTRATION, cards_adapter, notes_adapter

from tests.formats.format_adapter_support import CARDS


class TestNotesContract(FormatAdapterContract):
    @pytest.fixture
    def harness(self):
        return FormatHarness(notes_adapter(), NOTES_REGISTRATION, b"a note\nanother note\n", b"a changed note\n", ".notes")


class TestCardsContract(FormatAdapterContract):
    @pytest.fixture
    def harness(self):
        return FormatHarness(cards_adapter(), CARDS_REGISTRATION, CARDS, CARDS_HEADER + b"#one\nchanged\n", ".cards")


# The two fakes tell the truth, and the truth differs.
