from pathlib import Path

from ebooklib import epub

from ayvu.formats.epub.validation import validate_output_epub


def _build_epub(tmp_path: Path, chapters: list[epub.EpubHtml], extra_items: list[object] | None = None) -> Path:
    path = tmp_path / "book.epub"
    book = epub.EpubBook()
    book.set_identifier("ayvu-validation-test")
    book.set_title("Validation Test Book")
    book.set_language("en")
    book.add_author("Ayvu Tests")

    for chapter in chapters:
        book.add_item(chapter)
    for item in extra_items or []:
        book.add_item(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = tuple(epub.Link(c.file_name, c.title, c.id) for c in chapters)
    book.spine = ["nav", *chapters]
    epub.write_epub(str(path), book)
    return path


def test_validate_output_epub_accepts_minimal_generated_epub(minimal_epub_path: Path):
    result = validate_output_epub(minimal_epub_path)

    assert result.ok
    assert result.warnings == []
    assert result.document_count >= 2


def test_validate_reports_empty_chapter(tmp_path: Path):
    empty = epub.EpubHtml(title="Empty", file_name="text/empty.xhtml", lang="en")
    empty.content = "<h1></h1><p>   </p>"
    filled = epub.EpubHtml(title="Filled", file_name="text/filled.xhtml", lang="en")
    filled.content = "<p>Conteúdo de verdade.</p>"

    result = validate_output_epub(_build_epub(tmp_path, [empty, filled]))

    assert not result.ok
    assert any("Capítulo sem texto visível" in w and "empty.xhtml" in w for w in result.warnings)
    assert not any("filled.xhtml" in w for w in result.warnings)


def test_validate_reports_broken_internal_link(tmp_path: Path):
    chapter = epub.EpubHtml(title="One", file_name="text/one.xhtml", lang="en")
    chapter.content = '<p>Veja <a href="missing.xhtml">o capítulo dois</a>.</p>'

    result = validate_output_epub(_build_epub(tmp_path, [chapter]))

    assert not result.ok
    assert any("Link interno quebrado" in w and "missing.xhtml" in w for w in result.warnings)


def test_validate_reports_missing_referenced_image(tmp_path: Path):
    chapter = epub.EpubHtml(title="One", file_name="text/one.xhtml", lang="en")
    chapter.content = '<p>Figura.</p><p><img src="../images/none.png" alt="x" /></p>'

    result = validate_output_epub(_build_epub(tmp_path, [chapter]))

    assert not result.ok
    assert any("Imagem ausente referenciada" in w and "none.png" in w for w in result.warnings)


def test_validate_invokes_progress_callback_for_each_document(minimal_epub_path: Path):
    calls: list[tuple[int, int, str]] = []

    result = validate_output_epub(minimal_epub_path, on_progress=lambda i, t, n: calls.append((i, t, n)))

    assert result.ok
    assert len(calls) == result.document_count
    assert [index for index, _total, _name in calls] == list(range(1, result.document_count + 1))
    assert {total for _index, total, _name in calls} == {result.document_count}
