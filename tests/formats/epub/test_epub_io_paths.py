from pathlib import Path, PurePosixPath


from ayvu.formats.epub.io import _document_entries, _document_zip_path, extract_markdown, inspect_epub

from tests.support.epub_io_support import FakeBook


def test_document_zip_path_for_root_opf():
    assert _document_zip_path(PurePosixPath("."), "text/chapter.xhtml") == "text/chapter.xhtml"


def test_document_zip_path_for_nested_opf():
    assert _document_zip_path(PurePosixPath("OEBPS"), "text/chapter.xhtml") == "OEBPS/text/chapter.xhtml"


def test_document_entries_keep_item_names_and_archive_paths():
    book = FakeBook(["text/chapter.xhtml", "text/next.xhtml"])

    documents = _document_entries(book, PurePosixPath("OEBPS"))

    assert [document.name for document in documents] == ["text/chapter.xhtml", "text/next.xhtml"]
    assert [document.archive_path for document in documents] == [
        "OEBPS/text/chapter.xhtml",
        "OEBPS/text/next.xhtml",
    ]


def test_inspect_epub_reads_minimal_generated_epub(minimal_epub_path: Path):
    info = inspect_epub(minimal_epub_path)

    assert info.path == minimal_epub_path
    assert info.title == "Minimal Test Book"
    assert info.authors == ["Ayvu Tests"]
    assert info.language == "en"
    assert info.document_count >= 2
    assert info.item_count >= 4


def test_extract_markdown_reads_visible_text_from_minimal_generated_epub(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_dir = tmp_path / "extracted"

    written = extract_markdown(minimal_epub_path, output_dir)

    extracted = "\n".join(path.read_text(encoding="utf-8") for path in written)
    assert written
    assert all(path.parent == output_dir for path in written)
    assert "Chapter One" in extracted
    assert "Hello reader. Visit" in extracted
    assert "chapter two" in extracted
    assert "Chapter Two" in extracted
    assert "Goodbye reader." in extracted
