"""The v1 translation of a whole EPUB: chapters through the v1 cache, translator and HTML translation, with the
v1 review CSV beside it. Reading the EPUB is ``formats.epub.io``. It moved out of ``epub_io.py`` in REF-10.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from ebooklib import epub

from .cache import TranslationCache
from ..formats.epub.archive import package_title, parse_package_for_edit, serialize_package
from ..knowledge.glossary import Glossary, GlossaryUsage
from .html_translate import HtmlTranslatedSegment, HtmlTranslationStats, TextTranslationResult, translate_html, translate_text
from ..review_export import METADATA_CHAPTER_NAME, METADATA_SEGMENT_ID, METADATA_SEGMENT_KIND, ReviewSegment, format_segment_id
from ..review_import import LegacyReviewInspection, ReviewImportError
from ..knowledge.memory import TranslationMemory
from .translator import Translator
from ..kernel import ChapterSelection, TranslationOptions
from ..formats.epub.io import EpubDocument, _first_metadata, _get_opf_archive_path, _limited_documents, _opf_base_path, _resolve_selected_chapters, _translation_documents


ChapterStartCallback = Callable[[int, int, str], None]
ChapterDoneCallback = Callable[[int, int, str, HtmlTranslationStats], None]
TextProgressCallback = Callable[[str], None]
MetadataReviewCallback = Callable[[str, TextTranslationResult], None]


TranslatorFactory = Callable[[], Translator]
CacheFactory = Callable[[], TranslationCache]


@dataclass(frozen=True)
class DocumentTranslationJob:
    index: int
    document: EpubDocument
    content: bytes


@dataclass(frozen=True)
class DocumentTranslationResult:
    index: int
    document: EpubDocument
    content: bytes
    stats: HtmlTranslationStats
    review_segments: tuple[ReviewSegment, ...]
    text_statuses: tuple[str, ...]


@dataclass(frozen=True)
class EpubStructureError:
    document: EpubDocument
    detail: str

    @classmethod
    def missing_document(cls, document: EpubDocument) -> "EpubStructureError":
        return cls(
            document=document,
            detail=f"document not found in EPUB archive at {document.archive_path}",
        )

    @classmethod
    def chapter_error(cls, document: EpubDocument, exc: Exception) -> "EpubStructureError":
        return cls(document=document, detail=str(exc))

    def as_message(self) -> str:
        return f"{self.document.name}: {self.detail}"


@dataclass
class TranslationReport:
    chapters_processed: int = 0
    texts_translated: int = 0
    texts_from_cache: int = 0
    texts_from_memory: int = 0
    memory_suggestions: int = 0
    texts_skipped: int = 0
    texts_missing: int = 0
    alt_texts_translated: int = 0
    errors: list[str] = field(default_factory=list)
    missing_texts: list[str] = field(default_factory=list)
    memory_suggestion_texts: list[str] = field(default_factory=list)
    glossary_terms_configured: int = 0
    glossary_usage: GlossaryUsage = field(default_factory=GlossaryUsage)
    output_path: Path | None = None
    output_written: bool = True
    review_output_path: Path | None = None
    input_path: Path | None = None
    detected_language: str | None = None
    target_language: str | None = None

    def record_error(self, message: str) -> None:
        self.errors.append(message)

    def record_chapter(self, stats: HtmlTranslationStats) -> None:
        self.texts_translated += stats.translated
        self.texts_from_cache += stats.from_cache
        self.texts_from_memory += stats.from_memory
        self.memory_suggestions += stats.memory_suggestions
        self.texts_skipped += stats.skipped
        self.texts_missing += stats.missing
        self.alt_texts_translated += stats.alt_translated
        self.errors.extend(stats.errors)
        self.missing_texts.extend(stats.missing_texts)
        self.memory_suggestion_texts.extend(stats.memory_suggestion_texts)
        self.glossary_usage.merge(stats.glossary_usage)
        self.chapters_processed += 1

    def record_text(self, result: TextTranslationResult) -> None:
        if result.memory_suggestion is not None:
            self.memory_suggestions += 1
            self.memory_suggestion_texts.append(result.memory_suggestion.original)
        if result.from_memory:
            self.texts_from_memory += 1
        elif result.missing:
            self.texts_missing += 1
            self.missing_texts.append(result.text)
        elif result.from_cache:
            self.texts_from_cache += 1
        else:
            self.texts_translated += 1
        self.glossary_usage.merge(result.glossary_usage)


@dataclass
class EpubReplacements:
    items: dict[str, bytes] = field(default_factory=dict)

    def add(self, archive_path: str, content: bytes) -> None:
        self.items[archive_path] = content

    def content_for(self, filename: str, source_epub: ZipFile) -> bytes:
        replacement = self.items.get(filename)
        if replacement is not None:
            return replacement
        return source_epub.read(filename)


def translate_epub(
    input_path: str | Path,
    output_path: str | Path,
    translator: Translator,
    cache: TranslationCache,
    options: TranslationOptions,
    glossary: Glossary | None = None,
    on_chapter_start: ChapterStartCallback | None = None,
    on_chapter_done: ChapterDoneCallback | None = None,
    on_text_processed: TextProgressCallback | None = None,
    review_segments: list[ReviewSegment] | None = None,
    translator_factory: TranslatorFactory | None = None,
    cache_factory: CacheFactory | None = None,
) -> TranslationReport:
    _validate_worker_options(options, translator_factory=translator_factory, cache_factory=cache_factory)
    source_path = Path(input_path)
    destination_path = Path(output_path)
    book = epub.read_epub(str(source_path))
    report = TranslationReport(
        output_path=destination_path,
        input_path=source_path,
        detected_language=_first_metadata(book, "DC", "language"),
        target_language=options.target,
        glossary_terms_configured=len(glossary.entries) if glossary else 0,
    )
    opf_archive_path = _get_opf_archive_path(source_path)
    opf_base_path = _opf_base_path(opf_archive_path)
    replacements = EpubReplacements()
    memory = (
        TranslationMemory(cache, options.translation_memory)
        if options.translation_memory is not None
        else None
    )

    with ZipFile(source_path, "r") as source_epub:
        archive_names = set(source_epub.namelist())
        documents = _limited_documents(
            _translation_documents(
                book,
                source_epub,
                opf_archive_path,
                opf_base_path,
                options.translate_metadata,
                include_titles=options.chapter_selection is not None,
            ),
            options.max_documents,
        )
        documents = _select_documents(documents, options.chapter_selection)
        total_documents = len(documents)

        if options.translate_metadata and opf_archive_path and opf_archive_path in archive_names:
            try:
                translated_opf = _translate_opf_title(
                    source_epub.read(opf_archive_path),
                    translator=translator,
                    cache=cache,
                    options=options,
                    glossary=glossary,
                    report=report,
                    memory=memory,
                    on_text_processed=on_text_processed,
                    on_review_text=_metadata_review_callback(
                        review_segments,
                        source_path=source_path,
                        destination_path=destination_path,
                        opf_archive_path=opf_archive_path,
                        options=options,
                    ),
                )
                if translated_opf is not None and not options.dry_run:
                    replacements.add(opf_archive_path, translated_opf)
            except Exception as exc:
                report.record_error(f"metadata title: {exc}")
                _notify_text_processed(on_text_processed, "error")
                if options.fail_fast:
                    raise

        if options.workers > 1:
            assert translator_factory is not None
            assert cache_factory is not None
            _translate_documents_parallel(
                documents=documents,
                archive_names=archive_names,
                source_epub=source_epub,
                source_path=source_path,
                destination_path=destination_path,
                translator_factory=translator_factory,
                cache_factory=cache_factory,
                options=options,
                glossary=glossary,
                report=report,
                replacements=replacements,
                total_documents=total_documents,
                on_chapter_start=on_chapter_start,
                on_chapter_done=on_chapter_done,
                on_text_processed=on_text_processed,
                review_segments=review_segments,
            )
        else:
            _translate_documents_sequential(
                documents=documents,
                archive_names=archive_names,
                source_epub=source_epub,
                source_path=source_path,
                destination_path=destination_path,
                translator=translator,
                cache=cache,
                memory=memory,
                options=options,
                glossary=glossary,
                report=report,
                replacements=replacements,
                total_documents=total_documents,
                on_chapter_start=on_chapter_start,
                on_chapter_done=on_chapter_done,
                on_text_processed=on_text_processed,
                review_segments=review_segments,
            )

    if not options.dry_run:
        blocked = options.cache_only and options.require_full_cache and report.texts_missing > 0
        if blocked:
            report.output_written = False
        else:
            if glossary:
                report.glossary_usage.finalize_required_terms(glossary)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            _copy_epub_with_replacements(source_path, destination_path, replacements)

    return report


def _validate_worker_options(
    options: TranslationOptions,
    *,
    translator_factory: TranslatorFactory | None,
    cache_factory: CacheFactory | None,
) -> None:
    if options.workers < 1:
        raise ValueError("worker count must be 1 or greater")
    if options.workers == 1:
        return
    if options.translation_memory is not None:
        raise ValueError("translation memory is not supported with multiple workers")
    if translator_factory is None:
        raise ValueError("translator_factory is required when workers is greater than 1")
    if cache_factory is None:
        raise ValueError("cache_factory is required when workers is greater than 1")


def _translate_documents_sequential(
    *,
    documents: list[EpubDocument],
    archive_names: set[str],
    source_epub: ZipFile,
    source_path: Path,
    destination_path: Path,
    translator: Translator,
    cache: TranslationCache,
    memory: TranslationMemory | None,
    options: TranslationOptions,
    glossary: Glossary | None,
    report: TranslationReport,
    replacements: EpubReplacements,
    total_documents: int,
    on_chapter_start: ChapterStartCallback | None,
    on_chapter_done: ChapterDoneCallback | None,
    on_text_processed: TextProgressCallback | None,
    review_segments: list[ReviewSegment] | None,
) -> None:
    for index, document in enumerate(documents, start=1):
        if document.archive_path not in archive_names:
            report.record_error(EpubStructureError.missing_document(document).as_message())
            if options.fail_fast:
                raise FileNotFoundError(document.archive_path)
            continue

        if on_chapter_start:
            on_chapter_start(index, total_documents, document.name)

        try:
            translated_content, stats = _translate_document_content(
                DocumentTranslationJob(
                    index=index,
                    document=document,
                    content=source_epub.read(document.archive_path),
                ),
                translator=translator,
                cache=cache,
                source_path=source_path,
                destination_path=destination_path,
                options=options,
                glossary=glossary,
                memory=memory,
                on_text_processed=on_text_processed,
                collect_review_segments=review_segments is not None,
                review_segments=review_segments,
            )
            if not options.dry_run:
                replacements.add(document.archive_path, translated_content)
            report.record_chapter(stats)
            if on_chapter_done:
                on_chapter_done(index, total_documents, document.name, stats)
        except Exception as exc:
            report.record_error(EpubStructureError.chapter_error(document, exc).as_message())
            if options.fail_fast:
                raise


def _translate_documents_parallel(
    *,
    documents: list[EpubDocument],
    archive_names: set[str],
    source_epub: ZipFile,
    source_path: Path,
    destination_path: Path,
    translator_factory: TranslatorFactory,
    cache_factory: CacheFactory,
    options: TranslationOptions,
    glossary: Glossary | None,
    report: TranslationReport,
    replacements: EpubReplacements,
    total_documents: int,
    on_chapter_start: ChapterStartCallback | None,
    on_chapter_done: ChapterDoneCallback | None,
    on_text_processed: TextProgressCallback | None,
    review_segments: list[ReviewSegment] | None,
) -> None:
    jobs: list[DocumentTranslationJob] = []
    for index, document in enumerate(documents, start=1):
        if document.archive_path not in archive_names:
            report.record_error(EpubStructureError.missing_document(document).as_message())
            if options.fail_fast:
                raise FileNotFoundError(document.archive_path)
            continue
        jobs.append(
            DocumentTranslationJob(
                index=index,
                document=document,
                content=source_epub.read(document.archive_path),
            )
        )

    futures: dict[int, Future[DocumentTranslationResult]] = {}
    with ThreadPoolExecutor(max_workers=options.workers) as executor:
        for job in jobs:
            futures[job.index] = executor.submit(
                _translate_document_worker,
                job=job,
                source_path=source_path,
                destination_path=destination_path,
                translator_factory=translator_factory,
                cache_factory=cache_factory,
                options=options,
                glossary=glossary,
                collect_review_segments=review_segments is not None,
            )

        for job in jobs:
            if on_chapter_start:
                on_chapter_start(job.index, total_documents, job.document.name)
            try:
                result = futures[job.index].result()
                for status in result.text_statuses:
                    _notify_text_processed(on_text_processed, status)
                if review_segments is not None:
                    review_segments.extend(result.review_segments)
                if not options.dry_run:
                    replacements.add(result.document.archive_path, result.content)
                report.record_chapter(result.stats)
                if on_chapter_done:
                    on_chapter_done(result.index, total_documents, result.document.name, result.stats)
            except Exception as exc:
                report.record_error(EpubStructureError.chapter_error(job.document, exc).as_message())
                if options.fail_fast:
                    raise


def _translate_document_worker(
    *,
    job: DocumentTranslationJob,
    source_path: Path,
    destination_path: Path,
    translator_factory: TranslatorFactory,
    cache_factory: CacheFactory,
    options: TranslationOptions,
    glossary: Glossary | None,
    collect_review_segments: bool,
) -> DocumentTranslationResult:
    text_statuses: list[str] = []
    local_review_segments: list[ReviewSegment] = []
    translator = translator_factory()
    with cache_factory() as cache:
        translated_content, stats = _translate_document_content(
            job,
            translator=translator,
            cache=cache,
            source_path=source_path,
            destination_path=destination_path,
            options=options,
            glossary=glossary,
            memory=None,
            on_text_processed=text_statuses.append,
            collect_review_segments=collect_review_segments,
            review_segments=local_review_segments,
        )
    return DocumentTranslationResult(
        index=job.index,
        document=job.document,
        content=translated_content,
        stats=stats,
        review_segments=tuple(local_review_segments),
        text_statuses=tuple(text_statuses),
    )


def _translate_document_content(
    job: DocumentTranslationJob,
    *,
    translator: Translator,
    cache: TranslationCache,
    source_path: Path,
    destination_path: Path,
    options: TranslationOptions,
    glossary: Glossary | None,
    memory: TranslationMemory | None,
    on_text_processed: TextProgressCallback | None,
    collect_review_segments: bool,
    review_segments: list[ReviewSegment] | None = None,
) -> tuple[bytes, HtmlTranslationStats]:
    document_segment_index = 0

    def on_segment_translated(segment: HtmlTranslatedSegment) -> None:
        nonlocal document_segment_index
        if not collect_review_segments or review_segments is None:
            return
        document_segment_index += 1
        review_segments.append(
            _review_segment_for_document(
                segment,
                source_path=source_path,
                destination_path=destination_path,
                document=job.document,
                chapter_index=job.index,
                segment_index=document_segment_index,
                options=options,
            )
        )

    return translate_html(
        job.content,
        translator=translator,
        cache=cache,
        source=options.source,
        target=options.target,
        glossary=glossary,
        dry_run=options.dry_run,
        cache_only=options.cache_only,
        fail_fast=options.fail_fast,
        chunk_limit=options.chunk_limit,
        memory=memory,
        on_text_processed=on_text_processed,
        on_segment_translated=on_segment_translated if collect_review_segments else None,
        translate_alt_text=options.translate_alt_text,
    )


@dataclass(repr=False)
class ReviewApplyReport:
    input_path: Path | None = None
    output_path: Path | None = None
    source_language: str = ""
    target_language: str = ""
    applied: int = 0
    untranslated: int = 0
    inconsistent: list[str] = field(default_factory=list)
    missing_in_epub: list[str] = field(default_factory=list)
    duplicated: list[str] = field(default_factory=list)
    unknown_documents: list[str] = field(default_factory=list)
    invalid_ids: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"ReviewApplyReport(applied={self.applied}, warnings={int(self.has_warnings)})"

    @property
    def has_warnings(self) -> bool:
        return bool(
            self.untranslated
            or self.inconsistent
            or self.missing_in_epub
            or self.duplicated
            or self.unknown_documents
            or self.invalid_ids
        )


def apply_reviewed_epub(
    input_path: str | Path,
    output_path: str | Path,
    review: LegacyReviewInspection,
) -> None:
    """Refuse positional v1 application before touching input or output.

    V2 callers validate with ``review_exchange.map_review_package`` and hand its
    structural plan to the format reconstructor. The legacy CLI adapter has no
    durable project and machine-result context and therefore cannot apply either version.
    """
    # Positional v1 rows cannot prove source revision or machine-result
    # lineage. Reject before opening either EPUB or destination.
    if isinstance(review, LegacyReviewInspection):
        raise ReviewImportError("legacy_reexport_required")
    raise ReviewImportError("review_v2_mapper_required")


def _select_documents(
    documents: list[EpubDocument],
    selection: ChapterSelection | None,
) -> list[EpubDocument]:
    if selection is None:
        return documents
    selected = _resolve_selected_chapters(documents, selection)
    return [documents[chapter.index - 1] for chapter in selected]


def _metadata_review_callback(
    review_segments: list[ReviewSegment] | None,
    source_path: Path,
    destination_path: Path,
    opf_archive_path: str,
    options: TranslationOptions,
) -> MetadataReviewCallback | None:
    if review_segments is None:
        return None

    def append_metadata_segment(original: str, result: TextTranslationResult) -> None:
        review_segments.append(
            ReviewSegment(
                segment_id=METADATA_SEGMENT_ID,
                source_epub=str(source_path),
                output_epub=str(destination_path),
                chapter_index=0,
                chapter_name=METADATA_CHAPTER_NAME,
                document_name=opf_archive_path,
                document_path=opf_archive_path,
                segment_kind=METADATA_SEGMENT_KIND,
                source_language=options.source,
                target_language=options.target,
                original=original,
                translated=result.text,
                from_cache=result.from_cache,
            )
        )

    return append_metadata_segment


def _review_segment_for_document(
    segment: HtmlTranslatedSegment,
    source_path: Path,
    destination_path: Path,
    document: EpubDocument,
    chapter_index: int,
    segment_index: int,
    options: TranslationOptions,
) -> ReviewSegment:
    return ReviewSegment(
        segment_id=format_segment_id(chapter_index, segment_index),
        source_epub=str(source_path),
        output_epub=str(destination_path),
        chapter_index=chapter_index,
        chapter_name=document.name,
        document_name=document.name,
        document_path=document.archive_path,
        segment_kind=segment.kind,
        source_language=options.source,
        target_language=options.target,
        original=segment.original,
        translated=segment.translated,
        from_cache=segment.from_cache,
    )


def _translate_opf_title(
    opf_content: bytes,
    translator: Translator,
    cache: TranslationCache,
    options: TranslationOptions,
    glossary: Glossary | None,
    report: TranslationReport,
    on_text_processed: TextProgressCallback | None,
    memory: TranslationMemory | None = None,
    on_review_text: MetadataReviewCallback | None = None,
) -> bytes | None:
    root = parse_package_for_edit(opf_content)
    title = package_title(root)
    if title is not None and len(title):
        raise ValueError("the title holds a comment, element or entity, so it stays untranslated")
    if title is None or title.text is None or not title.text.strip():
        return None

    result = translate_text(
        title.text,
        translator=translator,
        cache=cache,
        source=options.source,
        target=options.target,
        glossary=glossary,
        dry_run=options.dry_run,
        cache_only=options.cache_only,
        chunk_limit=options.chunk_limit,
        memory=memory,
    )
    if on_review_text:
        on_review_text(title.text, result)
    changed = not result.missing and result.text != title.text
    if changed:
        title.text = result.text
    report.record_text(result)
    _notify_text_processed(on_text_processed, _text_progress_status(result, options.dry_run))
    return serialize_package(root, opf_content) if changed else None


def _text_progress_status(result: TextTranslationResult, dry_run: bool) -> str:
    if result.missing:
        return "missing"
    if result.from_cache:
        return "cache"
    if result.from_memory:
        return "memory"
    if dry_run:
        return "dry_run"
    return "translated"


def _notify_text_processed(callback: TextProgressCallback | None, status: str) -> None:
    if callback:
        callback(status)


def _copy_epub_with_replacements(
    input_path: Path,
    output_path: Path,
    replacements: EpubReplacements,
) -> None:
    with ZipFile(input_path, "r") as source_epub, ZipFile(output_path, "w") as output_epub:
        for info in source_epub.infolist():
            data = replacements.content_for(info.filename, source_epub)
            if info.filename == "mimetype":
                info.compress_type = ZIP_STORED
            output_epub.writestr(info, data)
