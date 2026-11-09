from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT
from ebooklib import epub

from .epub_archive import resolve_internal_path as _resolve_internal_path
from .html_translate import extract_visible_text

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ValidationResult:
    ok: bool
    warnings: list[str] = field(default_factory=list)
    document_count: int = 0


def validate_output_epub(path: str | Path, on_progress: ProgressCallback | None = None) -> ValidationResult:
    epub_path = Path(path)

    if not epub_path.exists():
        return ValidationResult(ok=False, warnings=[f"Arquivo de saída não existe: {epub_path}"])

    try:
        book = epub.read_epub(str(epub_path))
    except Exception as exc:
        return ValidationResult(
            ok=False,
            warnings=[f"Não foi possível abrir o EPUB gerado com o ebooklib: {exc}"],
        )

    documents = list(book.get_items_of_type(ITEM_DOCUMENT))
    if not documents:
        return ValidationResult(ok=False, warnings=["O EPUB gerado não contém documentos XHTML/HTML"])

    item_names = {item.get_name() for item in book.get_items()}
    warnings: list[str] = []
    total = len(documents)
    for index, document in enumerate(documents, start=1):
        name = document.get_name()
        content = document.get_content()
        if not isinstance(document, (epub.EpubNav, epub.EpubCoverHtml)):
            warnings.extend(_empty_chapter_warnings(name, content))
        warnings.extend(_broken_internal_link_warnings(name, content, item_names))
        warnings.extend(_missing_image_warnings(name, content, item_names))
        if on_progress is not None:
            on_progress(index, total, name)

    return ValidationResult(ok=not warnings, warnings=warnings, document_count=total)


def _empty_chapter_warnings(name: str, content: bytes) -> list[str]:
    soup = BeautifulSoup(content, "lxml-xml")
    body = soup.find("body")
    scope = body if body is not None else soup
    if scope.find(["img", "image", "svg"]) is not None:
        return []
    if "".join(extract_visible_text(str(scope))).strip():
        return []
    return [f"Capítulo sem texto visível: {name}"]


def _broken_internal_link_warnings(name: str, content: bytes, item_names: set[str]) -> list[str]:
    soup = BeautifulSoup(content, "lxml-xml")
    base_dir = posixpath.dirname(name)
    warnings: list[str] = []
    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        target = _resolve_internal_path(href, base_dir)
        if target is not None and target not in item_names:
            warnings.append(f"Link interno quebrado em {name}: {href}")
    return warnings


def _missing_image_warnings(name: str, content: bytes, item_names: set[str]) -> list[str]:
    soup = BeautifulSoup(content, "lxml-xml")
    base_dir = posixpath.dirname(name)
    warnings: list[str] = []
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        target = _resolve_internal_path(src, base_dir)
        if target is not None and target not in item_names:
            warnings.append(f"Imagem ausente referenciada em {name}: {src}")
    for image in soup.find_all("image"):
        href = (image.get("xlink:href") or image.get("href") or "").strip()
        target = _resolve_internal_path(href, base_dir)
        if target is not None and target not in item_names:
            warnings.append(f"Imagem ausente referenciada em {name}: {href}")
    return warnings
