import csv
import threading
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
import pytest
from ebooklib import ITEM_DOCUMENT
from ayvu.v1.cache import TranslationCache
from ayvu.formats.epub.io import EpubDocument, _document_entries, _document_zip_path, _get_opf_archive_path, _navigation_document_entries, _opf_base_path, detect_epub_language, extract_markdown, inspect_epub, normalize_language_code, resolve_chapter_selection
from ayvu.translation import TranslationReport
from ayvu.v1.epub import EpubReplacements, EpubStructureError, apply_reviewed_epub, translate_epub
from ayvu.knowledge.glossary.rules import GLOSSARY_RULE_FORBIDDEN, GLOSSARY_RULE_PRESERVE, Glossary, GlossaryEntry
from ayvu.review.csv_export import REVIEW_CSV_COLUMNS, write_review_csv
from ayvu.review.csv_import import ReviewImportData, ReviewImportError, ReviewRow, read_review_csv
from tests.support.epub_fakes import OPF, epub_bytes
from ayvu.kernel import ChapterSelection, ChapterSelectionError, LanguagePair, TranslationOptions

class FakeBook:
    def __init__(self, names: list[str]) -> None:
        self._items = [FakeItem(name) for name in names]

    def get_items_of_type(self, item_type: int) -> list["FakeItem"]:
        assert item_type == ITEM_DOCUMENT
        return self._items


class FakeItem:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class FakeZip:
    def read(self, filename: str) -> bytes:
        return f"original:{filename}".encode("utf-8")


class PrefixTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        return f"PT:{text}"


class RaisingTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        raise AssertionError("translator must not be called in cache-only mode")


def _read_navigation_document(epub_path: Path) -> str:
    opf_archive_path = _get_opf_archive_path(epub_path)
    with ZipFile(epub_path) as source_epub:
        documents = _navigation_document_entries(source_epub, opf_archive_path, _opf_base_path(opf_archive_path))
        assert documents
        return source_epub.read(documents[0].archive_path).decode("utf-8")


def _read_chapter_one(epub_path: Path) -> str:
    with ZipFile(epub_path) as output_epub:
        chapter_name = next(
            name for name in output_epub.namelist() if name.endswith("text/chapter1.xhtml")
        )
        return output_epub.read(chapter_name).decode("utf-8")


def _export_review_csv(minimal_epub_path: Path, tmp_path: Path) -> Path:
    output_path = tmp_path / "minimal-pt.epub"
    review_segments = []
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_epub(
            minimal_epub_path,
            output_path,
            translator=PrefixTranslator(),
            cache=cache,
            options=options,
            review_segments=review_segments,
        )
    return write_review_csv(tmp_path / "review.csv", review_segments)


def _rewrite_csv(csv_path: Path, mutate) -> None:
    with csv_path.open(encoding="utf-8", newline="") as input_file:
        rows = list(csv.DictReader(input_file))
    for row in rows:
        mutate(row)
    with csv_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(REVIEW_CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


# The OPF title: only its text changes

OPF_MEMBER = "OEBPS/content.opf"


# ebooklib, which the current flow opens every book with, rejects a remote manifest item
# and a processing instruction directly inside the metadata.
OPENABLE_OPF = OPF.replace(b'    <item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>\n', b"")


RICH_OPF = (
    OPENABLE_OPF.replace(b'xmlns:dc=', b'xmlns:rendition="http://www.idpf.org/vocab/rendition/#" xmlns:dc=', 1)
    .replace(b'opf:role="aut"', b'opf:role="aut" opf:file-as="Author, An"', 1)
    .replace(b"<!-- keep -->", b"<!-- keep --><dc:description><![CDATA[<p>x & y</p>]]></dc:description>", 1)
    .replace(b"<metadata>", b"<?keep this?><metadata>", 1)
)


TITLE_BYTES = b'<dc:title id="t">The Title</dc:title>'


class FixedTranslator:
    def __init__(self, change) -> None:
        self.change = change
        self.calls: list[str] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append(text)
        return self.change(text)


def _book_with_opf(tmp_path: Path, opf: bytes) -> Path:
    path = tmp_path / "book.epub"
    path.write_bytes(epub_bytes(replace={OPF_MEMBER: opf}))
    return path


def _opf_of(path: Path) -> bytes:
    with ZipFile(path) as book:
        return book.read(OPF_MEMBER)


def _translate_title(tmp_path: Path, opf: bytes, *, change=lambda text: f"PT:{text}", **options):
    source = _book_with_opf(tmp_path, opf)
    output = tmp_path / "out.epub"
    translator = FixedTranslator(change)
    settings = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"), translate_metadata=True, **options)
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(source, output, translator=translator, cache=cache, options=settings)
    return output, report, translator


def _reviewed_title(translated: str, original: str = "The Title") -> ReviewImportData:
    row = ReviewRow("metadata-title", OPF_MEMBER, original, translated, "metadata_title", "en", "pt")
    return ReviewImportData((row,), ())


def _without_declaration(data: bytes) -> bytes:
    return data.split(b"?>", 1)[1].lstrip()
