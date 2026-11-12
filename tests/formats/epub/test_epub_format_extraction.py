from zipfile import ZipFile

import pytest

from ayvu.formats.epub import io as epub_io
from ayvu.formats.contracts import ContentScope, ExtractionRequest
from ayvu.formats.epub.html_structure import html_unit_template
from tests.support.epub_fakes import epub_bytes, opf_with
from tests.support.format_contract_env import PROJECT
from ayvu.kernel import JobFailureFamily

from tests.formats.epub.epub_format_support import all_units, extract
from tests.formats.epub.epub_format_support import service, source_of


def test_inspection_reports_the_book_and_what_a_selection_can_name(service, tmp_path):
    _, source = source_of(tmp_path)
    inventory = service.inspect("epub", source).inventory
    assert (inventory.title, inventory.authors, inventory.language, inventory.format_version) == ("The Title", ("An Author", "Another"), "en", "3.0")
    assert inventory.resource_count == 6
    assert [(item.logical_key, item.title) for item in inventory.documents] == [
        ("OEBPS/content.opf", "The Title"), ("OEBPS/nav.xhtml", "First"), ("OEBPS/text/one.xhtml", "Hello"), ("OEBPS/text/two words.xhtml", "Second")]
    assert inventory.document_count == 4


def test_an_unknown_package_version_is_reported_as_unknown(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b'version="3.0"', b'version="9.9"')))
    assert service.inspect("epub", source).inventory.format_version is None


def test_a_book_with_no_title_lists_only_its_documents(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b'<dc:title id="t">The Title</dc:title>', b"")))
    inventory = service.inspect("epub", source).inventory
    assert inventory.title is None and inventory.document_count == 3


# Extraction

def test_body_only_is_the_default_scope(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    assert [document.logical_key for document in documents] == ["OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]
    assert "A picture" not in [unit.source_text for unit in all_units(documents)]


@pytest.mark.parametrize("scopes,keys", [
    ({ContentScope.BODY}, ["OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]),
    ({ContentScope.NAVIGATION}, ["OEBPS/nav.xhtml"]),
    ({ContentScope.METADATA}, ["OEBPS/content.opf"]),
    ({ContentScope.METADATA, ContentScope.BODY}, ["OEBPS/content.opf", "OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]),
], ids=range(4))
def test_each_scope_is_separate(service, tmp_path, scopes, keys):
    _, source = source_of(tmp_path)
    assert [document.logical_key for document in extract(service, source, frozenset(scopes)).documents] == keys


def test_alt_text_comes_only_when_asked_for(service, tmp_path):
    _, source = source_of(tmp_path)
    with_alt = all_units(extract(service, source, frozenset({ContentScope.BODY, ContentScope.IMAGE_ALT_TEXT})).documents)
    assert "A picture" in [unit.source_text for unit in with_alt]


def test_alt_text_alone_is_refused_rather_than_ignored(service, tmp_path):
    path, source = source_of(tmp_path)
    failure = extract(service, source, frozenset({ContentScope.IMAGE_ALT_TEXT})).failure
    assert (failure.family, failure.code) == (JobFailureFamily.CAPABILITY, "unsupported_scope")


def test_the_title_is_a_unit_with_its_own_locator(service, tmp_path):
    _, source = source_of(tmp_path)
    document = extract(service, source, frozenset({ContentScope.METADATA})).documents[0]
    unit = document.units[0]
    assert (unit.source_text, unit.content.locator.schema, unit.content.kind.name) == ("The Title", "epub_package", "title")


def test_a_book_with_a_blank_title_has_no_title_unit(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b">The Title<", b">   <")))
    assert extract(service, source, frozenset({ContentScope.METADATA})).documents == ()


def test_the_selection_is_followed_and_never_chosen(service, tmp_path):
    _, source = source_of(tmp_path)
    selected = extract(service, source, selection=("OEBPS/text/two words.xhtml", "OEBPS/content.opf"))
    assert [document.logical_key for document in selected.documents] == ["OEBPS/content.opf", "OEBPS/text/two words.xhtml"]
    assert extract(service, source, selection=("OEBPS/nothing.xhtml",)).documents == ()


def test_a_selected_navigation_needs_its_scope(service, tmp_path):
    _, source = source_of(tmp_path)
    assert service.extract("epub", source, ExtractionRequest(PROJECT, selection=("OEBPS/nav.xhtml",))).documents == ()


def test_html_is_delegated_and_units_keep_markup_protected(service, tmp_path):
    _, source = source_of(tmp_path)
    unit = service.extract("epub", source, ExtractionRequest(PROJECT)).documents[0].units[1]
    assert html_unit_template(unit) == "Hello __AYVU_TAG_0__world__AYVU_TAG_1__"
    assert unit.content.locator.schema == "html_position"


def test_extraction_agrees_with_the_chapter_order_of_the_current_flow(service, tmp_path):
    from ebooklib import epub as ebooklib_epub

    path, source = source_of(tmp_path, epub_bytes(replace=opf_with(b'<item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>', b"")))
    book = ebooklib_epub.read_epub(str(path))
    with ZipFile(path) as archive:
        current = [item.archive_path for item in epub_io._translation_documents(book, archive, "OEBPS/content.opf", epub_io._opf_base_path("OEBPS/content.opf"), False)]
    assert [document.logical_key for document in service.extract("epub", source, ExtractionRequest(PROJECT)).documents] == current


def test_a_document_that_declares_entities_is_refused(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/text/one.xhtml": b'<!DOCTYPE x [<!ENTITY e "v">]><html><body><p>&e;</p></body></html>'}))
    assert extract(service, source).failure.code == "entity_declaration"


# Reconstruction: what changes and what does not
