
import pytest

from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from tests.support.epub_fakes import TWO, epub_bytes
from tests.support.format_contract_env import FormatHarness
from tests.support.format_contract import FormatAdapterContract

from tests.formats.epub.epub_format_support import readable
from tests.formats.epub.epub_format_support import translation_of


class TestEpubContract(FormatAdapterContract):
    @pytest.fixture
    def harness(self):
        return FormatHarness(epub_format_adapter(), EPUB_REGISTRATION, epub_bytes(), epub_bytes(replace={"OEBPS/text/one.xhtml": TWO}), ".epub", translation_of, readable)


# What it claims
