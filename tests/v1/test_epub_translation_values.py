

from ayvu.formats.epub.io import EpubDocument
from ayvu.v1.epub import EpubReplacements, EpubStructureError, TranslationReport

from tests.support.epub_io_support import FakeZip


def test_epub_replacements_return_replacement_or_original_content():
    replacements = EpubReplacements()
    replacements.add("chapter.xhtml", b"translated")

    assert replacements.content_for("chapter.xhtml", FakeZip()) == b"translated"
    assert replacements.content_for("style.css", FakeZip()) == b"original:style.css"


def test_epub_structure_error_formats_missing_document_message():
    document = EpubDocument(name="text/chapter.xhtml", archive_path="OEBPS/text/chapter.xhtml")

    error = EpubStructureError.missing_document(document)

    assert error.as_message() == (
        "text/chapter.xhtml: document not found in EPUB archive at OEBPS/text/chapter.xhtml"
    )


def test_epub_structure_error_formats_chapter_error_message():
    document = EpubDocument(name="text/chapter.xhtml", archive_path="OEBPS/text/chapter.xhtml")

    error = EpubStructureError.chapter_error(document, ValueError("bad html"))

    assert error.as_message() == "text/chapter.xhtml: bad html"


def test_translation_report_records_preformatted_errors():
    report = TranslationReport()

    report.record_error("text/chapter.xhtml: bad html")

    assert report.errors == ["text/chapter.xhtml: bad html"]
