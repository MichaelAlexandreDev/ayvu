"""Reads an EPUB: its metadata, documents, chapters and language, and exports its visible text as Markdown.

The v1 translation of a whole EPUB that used to live here is ``v1/epub.py`` (REF-10).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from ebooklib import ITEM_DOCUMENT
from ebooklib import epub

from .html_structure import extract_visible_text
from ...kernel import ChapterSelection, ChapterSelectionError


@dataclass
class EpubInfo:
    path: Path
    title: str | None
    authors: list[str]
    language: str | None
    document_count: int
    item_count: int


@dataclass(frozen=True)
class EpubDocument:
    name: str
    archive_path: str
    title: str | None = None


@dataclass(frozen=True)
class SelectedChapter:
    index: int
    name: str
    archive_path: str
    title: str | None = None


def inspect_epub(path: str | Path) -> EpubInfo:
    epub_path = Path(path)
    book = epub.read_epub(str(epub_path))
    title = _first_metadata(book, "DC", "title")
    language = _first_metadata(book, "DC", "language")
    authors = [entry[0] for entry in book.get_metadata("DC", "creator")]
    documents = list(book.get_items_of_type(ITEM_DOCUMENT))
    items = list(book.get_items())
    return EpubInfo(
        path=epub_path,
        title=title,
        authors=authors,
        language=language,
        document_count=len(documents),
        item_count=len(items),
    )


def normalize_language_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    primary = cleaned.split("-", 1)[0].split("_", 1)[0].lower()
    if not primary.isalpha() or not 2 <= len(primary) <= 3:
        return None
    return primary


def detect_epub_language(path: str | Path) -> str | None:
    info = inspect_epub(path)
    return normalize_language_code(info.language)


def resolve_chapter_selection(
    input_path: str | Path,
    selection: ChapterSelection,
    translate_metadata: bool = False,
    max_documents: int | None = None,
) -> list[SelectedChapter]:
    source_path = Path(input_path)
    book = epub.read_epub(str(source_path))
    opf_archive_path = _get_opf_archive_path(source_path)
    opf_base_path = _opf_base_path(opf_archive_path)

    with ZipFile(source_path, "r") as source_epub:
        documents = _limited_documents(
            _translation_documents(
                book,
                source_epub,
                opf_archive_path,
                opf_base_path,
                translate_metadata,
                include_titles=True,
            ),
            max_documents,
        )

    return _resolve_selected_chapters(documents, selection)


def extract_markdown(input_path: str | Path, output_dir: str | Path) -> list[Path]:
    source_path = Path(input_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    book = epub.read_epub(str(source_path))
    written: list[Path] = []

    for index, item in enumerate(book.get_items_of_type(ITEM_DOCUMENT), start=1):
        text = "\n".join(extract_visible_text(item.get_content()))
        file_path = destination / f"{index:03d}-{Path(item.get_name()).stem}.md"
        file_path.write_text(_clean_extracted_text(text), encoding="utf-8")
        written.append(file_path)

    return written


def _clean_extracted_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    clean_lines = [line for line in lines if line]
    return "\n\n".join(clean_lines) + ("\n" if clean_lines else "")


def _get_opf_base_path(epub_path: Path) -> PurePosixPath:
    return _opf_base_path(_get_opf_archive_path(epub_path))


def _get_opf_archive_path(epub_path: Path) -> str | None:
    with ZipFile(epub_path, "r") as source_epub:
        container_xml = source_epub.read("META-INF/container.xml")

    root = ET.fromstring(container_xml)
    namespace = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = root.find(".//container:rootfile", namespace)
    if rootfile is None:
        rootfile = root.find(".//rootfile")
    if rootfile is None:
        return None

    full_path = rootfile.attrib.get("full-path", "").strip()
    return full_path or None


def _opf_base_path(opf_archive_path: str | None) -> PurePosixPath:
    if opf_archive_path is None:
        return PurePosixPath(".")
    parent = PurePosixPath(opf_archive_path).parent
    return parent if str(parent) != "." else PurePosixPath(".")


def _document_zip_path(opf_base_path: PurePosixPath, item_name: str) -> str:
    path = PurePosixPath(item_name)
    if opf_base_path != PurePosixPath("."):
        path = opf_base_path / path
    return path.as_posix()


def _document_entries(
    book: epub.EpubBook,
    opf_base_path: PurePosixPath,
    include_titles: bool = False,
) -> list[EpubDocument]:
    documents: list[EpubDocument] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        name = item.get_name()
        documents.append(
            EpubDocument(
                name=name,
                archive_path=_document_zip_path(opf_base_path, name),
                title=_document_title(item) if include_titles else None,
            )
        )
    return documents


def _limited_documents(documents: list[EpubDocument], max_documents: int | None) -> list[EpubDocument]:
    if max_documents is None:
        return documents
    return documents[:max(0, max_documents)]


def _documents_for_translation(
    documents: list[EpubDocument],
    navigation_documents: list[EpubDocument],
    translate_metadata: bool,
) -> list[EpubDocument]:
    navigation_paths = {document.archive_path for document in navigation_documents}
    if not translate_metadata:
        return [document for document in documents if document.archive_path not in navigation_paths]

    selected = list(documents)
    selected_paths = {document.archive_path for document in selected}
    for document in navigation_documents:
        if document.archive_path not in selected_paths:
            selected.append(document)
            selected_paths.add(document.archive_path)
    return selected


def _translation_documents(
    book: epub.EpubBook,
    source_epub: ZipFile,
    opf_archive_path: str | None,
    opf_base_path: PurePosixPath,
    translate_metadata: bool,
    include_titles: bool = False,
) -> list[EpubDocument]:
    return _documents_for_translation(
        _document_entries(book, opf_base_path, include_titles=include_titles),
        _navigation_document_entries(source_epub, opf_archive_path, opf_base_path),
        translate_metadata=translate_metadata,
    )


def _resolve_selected_chapters(
    documents: list[EpubDocument],
    selection: ChapterSelection,
) -> list[SelectedChapter]:
    selected: list[SelectedChapter] = []
    for index, document in enumerate(documents, start=1):
        if selection.matches(index, document.name, document.archive_path, document.title):
            selected.append(
                SelectedChapter(
                    index=index,
                    name=document.name,
                    archive_path=document.archive_path,
                    title=document.title,
                )
            )

    if not selected:
        raise ChapterSelectionError(f"no chapters matched selection: {selection.source}")
    return selected


def _document_title(item: object) -> str | None:
    title = getattr(item, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    get_title = getattr(item, "get_title", None)
    if callable(get_title):
        title = get_title()
        if isinstance(title, str) and title.strip():
            return title.strip()
    get_content = getattr(item, "get_content", None)
    if callable(get_content):
        try:
            for text in extract_visible_text(get_content()):
                clean = " ".join(text.split())
                if clean:
                    return clean
        except Exception:
            return None
    return None


def _navigation_document_entries(
    source_epub: ZipFile,
    opf_archive_path: str | None,
    opf_base_path: PurePosixPath,
) -> list[EpubDocument]:
    if opf_archive_path is None or opf_archive_path not in source_epub.namelist():
        return []

    try:
        root = ET.fromstring(source_epub.read(opf_archive_path))
    except ET.ParseError:
        return []

    manifest = _first_child_by_local_name(root, "manifest")
    if manifest is None:
        return []

    documents: list[EpubDocument] = []
    for item in manifest:
        if _local_name(item.tag) != "item":
            continue
        properties = item.attrib.get("properties", "")
        if "nav" not in properties.split():
            continue
        href = item.attrib.get("href", "").strip()
        if not href:
            continue
        archive_path = _document_zip_path(opf_base_path, _manifest_href_path(href))
        documents.append(EpubDocument(name=href, archive_path=archive_path))
    return documents


def _manifest_href_path(href: str) -> str:
    clean = href.split("#", 1)[0].split("?", 1)[0]
    return PurePosixPath(unquote(clean)).as_posix()


def _first_child_by_local_name(root: ET.Element, name: str) -> ET.Element | None:
    for child in root.iter():
        if _local_name(child.tag) == name:
            return child
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_metadata(book: epub.EpubBook, namespace: str, name: str) -> str | None:
    values = book.get_metadata(namespace, name)
    if not values:
        return None
    return values[0][0]
