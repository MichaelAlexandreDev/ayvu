from __future__ import annotations

import base64
from pathlib import Path

import pytest
from ebooklib import epub


MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich wraps at the terminal width, so a long temporary path would split across lines."""
    monkeypatch.setenv("COLUMNS", "500")


@pytest.fixture
def minimal_epub_path(tmp_path: Path) -> Path:
    path = tmp_path / "minimal.epub"
    book = epub.EpubBook()
    book.set_identifier("ayvu-minimal-test")
    book.set_title("Minimal Test Book")
    book.set_language("en")
    book.add_author("Ayvu Tests")

    chapter_one = epub.EpubHtml(title="Chapter One", file_name="text/chapter1.xhtml", lang="en")
    chapter_one.content = """
    <h1>Chapter One</h1>
    <p>Hello reader. Visit <a href="chapter2.xhtml#answer">chapter two</a>.</p>
    <p><img src="../images/pixel.png" alt="Pixel" /></p>
    """

    chapter_two = epub.EpubHtml(title="Chapter Two", file_name="text/chapter2.xhtml", lang="en")
    chapter_two.content = """
    <h1 id="answer">Chapter Two</h1>
    <p>Goodbye reader.</p>
    """

    image = epub.EpubItem(
        uid="pixel",
        file_name="images/pixel.png",
        media_type="image/png",
        content=MINIMAL_PNG,
    )

    book.add_item(chapter_one)
    book.add_item(chapter_two)
    book.add_item(image)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (
        epub.Link("text/chapter1.xhtml", "Chapter One", "chapter-one"),
        epub.Link("text/chapter2.xhtml", "Chapter Two", "chapter-two"),
    )
    book.spine = ["nav", chapter_one, chapter_two]

    epub.write_epub(str(path), book)
    return path
