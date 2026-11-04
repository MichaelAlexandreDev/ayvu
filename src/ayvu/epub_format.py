"""The production EPUB adapter: what an EPUB can do, stated through the format contract.

It reads through ``epub_archive`` and delegates every XHTML document to ``html_structure``,
so it holds no second segmenter. It never calls a provider, and nothing in the current
translation flow uses it yet.
"""
from __future__ import annotations

import posixpath
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .content import AdapterIdentity, ContentDocument, Locator, SemanticKind, TranslationUnit, build_document
from .epub_archive import (
    DEFAULT_LIMITS, RESOURCE_CODES, EpubArchive, EpubArchiveError, ManifestItem, package_title, parse_package_for_edit, resolve_internal_path,
    serialize_package,
)
from .format_adapter import (
    ContentScope, ExtractionRequest, FidelityAspect, FidelityClaim, FidelityLevel, FindingSeverity, FormatDescriptor, FormatInventory,
    FormatLimits, FormatMaturity, FormatOperation, FormatOperationError, FormatSignature, InventoryDocument, OperationClaim, OperationSupport,
    PreviewRequest, ReconstructionRequest, UnitTranslation, ValidationFinding,
)
from .format_service import FormatAdapter, FormatContext, FormatRegistration
from .html_structure import (
    HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION, HTML_SEGMENTER_VERSION, HtmlStructureError, extract_html_document, extract_visible_text,
    plain_text_content, reconstruct_html, restore_plain_text,
)
from .job_events import JobFailure, JobFailureFamily

EPUB_ADAPTER = AdapterIdentity("epub", 1, HTML_SEGMENTER_VERSION)
PACKAGE_LOCATOR_SCHEMA = "epub_package"
PACKAGE_LOCATOR_VERSION = 1
PACKAGE_TITLE_LOCATOR = Locator(PACKAGE_LOCATOR_SCHEMA, PACKAGE_LOCATOR_VERSION, "title")
EPUB_LOCATORS = ((HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION), (PACKAGE_LOCATOR_SCHEMA, PACKAGE_LOCATOR_VERSION))
BETWEEN_MEMBERS = "epub_between_members"
BETWEEN_DOCUMENTS = "epub_between_documents"
BEFORE_OUTPUT = "epub_before_output"
# Below this, a book being rebuilt stays in memory. Above it, it spills to an unnamed file with
# owner-only permissions, which holds the book in plain text until it is closed.
SPOOL_MEMORY_BYTES = 64 * 1024 ** 2
_CHUNK_BYTES = 1024 * 1024
_EVIDENCE = ("contract:format_adapter_v1", "test:tests/test_epub_format.py")
_INTEGRITY_CODES = frozenset({"corrupt_archive", "corrupt_member"})
_POLICY_CODES = frozenset({"encrypted_member", "encrypted_content", "entity_declaration"})
_INVALID_FINDINGS = frozenset({"no_documents"})


def _epub_failure(error: EpubArchiveError) -> FormatOperationError:
    code = str(error)
    if code in RESOURCE_CODES:
        family = JobFailureFamily.RESOURCE
    elif code in _INTEGRITY_CODES:
        family = JobFailureFamily.INTEGRITY
    elif code in _POLICY_CODES:
        family = JobFailureFamily.POLICY
    else:
        family = JobFailureFamily.INPUT
    return FormatOperationError(JobFailure(family, code))


def _integrity(code: str) -> FormatOperationError:
    return FormatOperationError(JobFailure(JobFailureFamily.INTEGRITY, code))


@contextmanager
def _opened(context: FormatContext) -> Iterator[EpubArchive]:
    try:
        stream = context.source.open()
    except Exception:
        raise FormatOperationError(JobFailure(JobFailureFamily.RESOURCE, "source_unreadable")) from None
    with stream:
        try:
            with EpubArchive(stream, checkpoint=lambda: context.checkpoint(BETWEEN_MEMBERS)) as archive:
                yield archive
        except EpubArchiveError as error:
            raise _epub_failure(error) from None
        except HtmlStructureError as error:
            raise _integrity(str(error)) from None


def _document_title(data: bytes) -> str | None:
    for text in extract_visible_text(data):
        clean = " ".join(text.split())
        if clean:
            return clean
    return None


def _title_document(archive: EpubArchive, request: ExtractionRequest, fingerprint: str) -> ContentDocument | None:
    title = archive.package.title
    if title is None or not title.strip():
        return None
    element = package_title(parse_package_for_edit(archive.read(archive.package.archive_path)))
    # A comment or element inside the title would leave part of it in the source language.
    if element is None or len(element):
        return None
    content = plain_text_content(title, PACKAGE_TITLE_LOCATOR, SemanticKind("title"), (0,))
    return build_document(request.project_id, archive.package.archive_path, EPUB_ADAPTER, request.source_locale, [content],
                          source_fingerprint=fingerprint)


def _wanted(item: ManifestItem, request: ExtractionRequest) -> bool:
    scope = ContentScope.NAVIGATION if item.is_navigation else ContentScope.BODY
    return scope in request.scopes and (request.selection is None or item.archive_path in request.selection)


def _replace_title(package: bytes, unit: TranslationUnit, text: str) -> bytes:
    """Only the title's text changes. Prefixes, attributes and comments stay, as a parser reads them."""
    root = parse_package_for_edit(package)
    title = package_title(root)
    if title is None or title.text != unit.source_text:
        raise _integrity("stale_revision")
    title.text = restore_plain_text(unit, text)
    return serialize_package(root, package)


def _replacements(context: FormatContext, archive: EpubArchive, request: PreviewRequest | ReconstructionRequest, *,
                  partial: bool) -> dict[str, bytes]:
    results = {item.unit_id: item for item in request.translations}
    replacements: dict[str, bytes] = {}
    for document in request.documents:
        context.checkpoint(BETWEEN_DOCUMENTS)
        wanted: list[UnitTranslation] = [results[unit.unit_id] for unit in document.units if unit.unit_id in results]
        if not wanted:
            continue
        if document.logical_key == archive.package.archive_path:
            unit = next(unit for unit in document.units if unit.content.locator == PACKAGE_TITLE_LOCATOR)
            replacements[document.logical_key] = _replace_title(archive.read(document.logical_key), unit, wanted[0].text)
        else:
            replacements[document.logical_key] = reconstruct_html(archive.read_document(document.logical_key), document, wanted, partial=partial)
    return replacements


def _findings(context: FormatContext, archive: EpubArchive) -> tuple[ValidationFinding, ...]:
    documents = archive.package.documents
    if not documents:
        return (ValidationFinding(FindingSeverity.ERROR, "no_documents"),)
    members = set(archive.names())
    counts = {"empty_chapter": 0, "broken_internal_link": 0, "missing_image": 0}
    for item in documents:
        context.checkpoint(BETWEEN_DOCUMENTS)
        data = archive.read_document(item.archive_path)
        soup = BeautifulSoup(data, "lxml-xml")
        base = posixpath.dirname(item.archive_path)
        # Only the body counts: a chapter with a title in its head still shows the reader nothing.
        body = soup.find("body") or soup
        if not item.is_navigation and body.find(["img", "image", "svg"]) is None and not "".join(extract_visible_text(str(body))).strip():
            counts["empty_chapter"] += 1

        def missing(reference: str | None) -> bool:
            target = resolve_internal_path(unquote((reference or "").strip()), base)
            return target is not None and target not in members

        counts["broken_internal_link"] += sum(missing(anchor.get("href")) for anchor in soup.find_all("a"))
        counts["missing_image"] += sum(missing(image.get("src")) for image in soup.find_all("img"))
        counts["missing_image"] += sum(missing(image.get("xlink:href") or image.get("href")) for image in soup.find_all("image"))
    return tuple(ValidationFinding(FindingSeverity.WARNING, code, count) for code, count in counts.items() if count)


class _EpubPorts:
    def __repr__(self) -> str:
        return "_EpubPorts()"

    def inspect(self, context: FormatContext) -> FormatInventory:
        with _opened(context) as archive:
            package = archive.package
            listed = [InventoryDocument(item.archive_path, _document_title(archive.read_document(item.archive_path)))
                      for item in package.documents]
            if package.title and package.title.strip():
                listed.insert(0, InventoryDocument(package.archive_path, package.title))
            version = package.version if package.version in _epub_descriptor().format_versions else None
            return FormatInventory(document_count=len(listed), resource_count=len(package.items), format_version=version, title=package.title,
                                   authors=package.creators, language=package.language, documents=tuple(listed))

    def extract(self, context: FormatContext, request: ExtractionRequest) -> tuple[ContentDocument, ...]:
        if ContentScope.IMAGE_ALT_TEXT in request.scopes and not request.scopes & {ContentScope.BODY, ContentScope.NAVIGATION}:
            # Alt text lives in documents, and extracting it means reading some.
            raise FormatOperationError(JobFailure(JobFailureFamily.CAPABILITY, "unsupported_scope"))
        documents: list[ContentDocument] = []
        with _opened(context) as archive:
            package = archive.package
            if ContentScope.METADATA in request.scopes and (request.selection is None or package.archive_path in request.selection):
                title = _title_document(archive, request, context.source_fingerprint)
                if title is not None:
                    documents.append(title)
            for item in package.documents:
                context.checkpoint(BETWEEN_DOCUMENTS)
                if _wanted(item, request):
                    documents.append(extract_html_document(
                        archive.read_document(item.archive_path), request.project_id, item.archive_path, adapter=EPUB_ADAPTER,
                        source_locale=request.source_locale, include_alt_text=ContentScope.IMAGE_ALT_TEXT in request.scopes,
                        source_fingerprint=context.source_fingerprint))
        return tuple(documents)

    def preview(self, context: FormatContext, request: PreviewRequest) -> Iterable[bytes]:
        return self._materialize(context, request, partial=True)

    def reconstruct(self, context: FormatContext, request: ReconstructionRequest) -> Iterable[bytes]:
        return self._materialize(context, request, partial=False)

    def _materialize(self, context: FormatContext, request: PreviewRequest | ReconstructionRequest, *, partial: bool) -> Iterable[bytes]:
        # A ZIP needs a seekable stream to keep its flags, so it is built first, then checked, and only
        # then handed to the artifact store. Nothing reaches staging before the book is known to be valid.
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES) as spool:
            with _opened(context) as archive:
                replacements = _replacements(context, archive, request, partial=partial)
                archive.copy_to(spool, replacements, lambda: context.checkpoint(BETWEEN_MEMBERS))
            spool.seek(0)
            try:
                with EpubArchive(spool) as written:
                    if any(item.code in _INVALID_FINDINGS for item in _findings(context, written)):
                        raise _integrity("invalid_output")
            except EpubArchiveError:
                raise _integrity("invalid_output") from None
            context.checkpoint(BEFORE_OUTPUT)
            spool.seek(0)
            while chunk := spool.read(_CHUNK_BYTES):
                yield chunk

    def validate(self, context: FormatContext) -> tuple[ValidationFinding, ...]:
        with _opened(context) as archive:
            return _findings(context, archive)


def _epub_descriptor() -> FormatDescriptor:
    def supported(operation: FormatOperation, *safe_points: str) -> OperationClaim:
        return OperationClaim(operation, OperationSupport.SUPPORTED, _EVIDENCE, safe_points)

    return FormatDescriptor(
        adapter=EPUB_ADAPTER,
        locators=EPUB_LOCATORS,
        operations=(
            supported(FormatOperation.INSPECT, BETWEEN_MEMBERS),
            supported(FormatOperation.EXTRACT, BETWEEN_MEMBERS, BETWEEN_DOCUMENTS),
            supported(FormatOperation.PREVIEW, BETWEEN_MEMBERS, BETWEEN_DOCUMENTS, BEFORE_OUTPUT),
            supported(FormatOperation.RECONSTRUCT, BETWEEN_MEMBERS, BETWEEN_DOCUMENTS, BEFORE_OUTPUT),
            supported(FormatOperation.VALIDATE, BETWEEN_MEMBERS, BETWEEN_DOCUMENTS),
        ),
        fidelity=tuple(FidelityClaim(aspect, FidelityLevel.PRESERVED, _EVIDENCE) for aspect in FidelityAspect),
        media_types=("application/epub+zip",),
        extensions=(".epub",),
        signatures=(FormatSignature(((0, b"PK\x03\x04"), (30, b"mimetypeapplication/epub+zip"))),),
        format_versions=("2.0", "2.0.1", "3.0", "3.1", "3.2", "3.3"),
        content_scopes=frozenset(ContentScope),
        limits=FormatLimits(max_source_bytes=DEFAULT_LIMITS.max_source_bytes),
        maturity=FormatMaturity.EXPERIMENTAL,
    )


EPUB_REGISTRATION = FormatRegistration(EPUB_ADAPTER.namespace, EPUB_ADAPTER.adapter_version, EPUB_LOCATORS)


def epub_format_adapter() -> FormatAdapter:
    ports = _EpubPorts()
    return FormatAdapter(_epub_descriptor(), inspector=ports, extractor=ports, previewer=ports, reconstructor=ports, validator=ports)
