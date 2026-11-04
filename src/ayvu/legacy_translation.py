"""The current language work and reports, behind the translation engine.

This is the named compatibility path: it reaches the v1 cache and the current text pipeline, and
it presents results in the current report's types. It gives none of them a provider, model or
pipeline identity they do not have. #153 and #146 retire the parts they replace.
"""
from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .content import TranslationUnit
from .domain import TranslationOptions
from .epub_archive import EpubArchive, EpubArchiveError, package_title, parse_package_for_edit
from .epub_io import TranslationReport, _validate_worker_options
from .format_service import FormatPublication, FormatService, FormatSource
from .html_structure import html_unit_template, normalize_translated_text, review_text_from_template, without_xml_invalid
from .html_translate import HtmlTranslationStats, TextTranslationResult, translate_text
from .review_export import (
    METADATA_CHAPTER_NAME, METADATA_SEGMENT_ID, METADATA_SEGMENT_KIND, ReviewSegment, format_segment_id,
)
from .translation_memory import TranslationMemory
from .translation_engine import (
    ALT_TEXT_KIND, METADATA_KIND, STATUS_CACHE, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED,
    BookFacts, EngineObserver, EngineRequest, EngineResult, ExecutorPool, ReviewEntry, TextStats, TranslationEngine,
    UnitExecution,
)

EPUB_ADAPTER_ID = "epub"
_REVIEW_KINDS = {ALT_TEXT_KIND: "alt"}


def _status(result: TextTranslationResult) -> str:
    if result.from_memory:
        return STATUS_MEMORY
    if result.missing:
        return STATUS_MISSING
    if result.from_cache:
        return STATUS_CACHE
    return STATUS_TRANSLATED


class LegacyUnitExecutor:
    """One unit through ``translate_text``, which holds the stages and the v1 cache."""

    def __init__(self, translator: object, cache: object, options: TranslationOptions, *, glossary: object | None = None,
                 memory: object | None = None) -> None:
        self._translator = translator
        self._cache = cache
        self._options = options
        self._glossary = glossary
        self._memory = memory

    def __repr__(self) -> str:
        return "LegacyUnitExecutor()"

    def untranslated(self, unit: TranslationUnit) -> str:
        return html_unit_template(unit)

    def execute(self, unit: TranslationUnit) -> UnitExecution:
        options = self._options
        template = html_unit_template(unit)
        result = translate_text(template, translator=self._translator, cache=self._cache, source=options.source,
                                target=options.target, glossary=self._glossary, cache_only=options.cache_only,
                                chunk_limit=options.chunk_limit, memory=self._memory)
        kind = unit.content.kind.name
        suggestion = None if result.memory_suggestion is None else result.memory_suggestion.original
        if kind == METADATA_KIND:
            if without_xml_invalid(result.text) != result.text:
                raise ValueError("the title translation holds characters XML cannot store")
            return UnitExecution(template if result.missing else result.text, _status(result), template, result.text,
                                 result.text if result.missing else None, suggestion, result.glossary_usage)
        markup = [unit.source_text[part.start:part.end] for part in unit.content.protected_parts]
        original = review_text_from_template(template, markup)
        if result.missing:
            rebuilt = template
        else:
            rebuilt = without_xml_invalid(result.text) if kind == ALT_TEXT_KIND else normalize_translated_text(result.text)
        return UnitExecution(rebuilt, _status(result), original, review_text_from_template(result.text, markup),
                             original if result.missing else None, suggestion, result.glossary_usage)


class LegacyExecutorPool:
    """Translation memory stays out of workers, as it does in the current flow."""

    def __init__(self, translator_factory: Callable[[], object], cache_factory: Callable[[], object],
                 options: TranslationOptions, *, glossary: object | None = None) -> None:
        self._translator_factory = translator_factory
        self._cache_factory = cache_factory
        self._options = options
        self._glossary = glossary

    def __repr__(self) -> str:
        return "LegacyExecutorPool()"

    @contextmanager
    def worker(self) -> Iterator[LegacyUnitExecutor]:
        translator = self._translator_factory()
        with self._cache_factory() as cache:
            yield LegacyUnitExecutor(translator, cache, self._options, glossary=self._glossary)


@dataclass(frozen=True, repr=False)
class _Package:
    facts: BookFacts
    archive_path: str | None = None
    language: str | None = None


def _read_package(path: Path) -> _Package:
    """A book the adapter refuses has no facts here, and the engine reports the refusal itself."""
    try:
        with path.open("rb") as stream, EpubArchive(stream) as archive:
            package = archive.package
            title = package_title(parse_package_for_edit(archive.read(package.archive_path)))
            names = tuple((item.archive_path, posixpath.relpath(item.archive_path, package.base or "."))
                          for item in package.documents)
            return _Package(BookFacts(names, title is not None and len(title) > 0), package.archive_path, package.language)
    except (EpubArchiveError, OSError, ValueError):
        return _Package(BookFacts())


def to_html_stats(stats: TextStats) -> HtmlTranslationStats:
    converted = HtmlTranslationStats(
        translated=stats.translated, from_cache=stats.from_cache, from_memory=stats.from_memory,
        memory_suggestions=stats.memory_suggestions, alt_translated=stats.alt_translated, missing=stats.missing,
        errors=list(stats.errors), missing_texts=list(stats.missing_texts),
        memory_suggestion_texts=list(stats.memory_suggestion_texts))
    for usage in stats.glossary_usages:
        converted.glossary_usage.merge(usage)
    return converted


def _to_report(result: EngineResult, source_path: Path, destination_path: Path, package: _Package,
               options: TranslationOptions, glossary: object | None) -> TranslationReport:
    report = TranslationReport(
        output_path=destination_path, input_path=source_path, detected_language=package.language, target_language=options.target,
        glossary_terms_configured=len(glossary.entries) if glossary else 0, output_written=result.output_written,
        chapters_processed=result.chapters_processed)
    stats = to_html_stats(result.stats)
    report.texts_translated, report.texts_from_cache = stats.translated, stats.from_cache
    report.texts_from_memory, report.memory_suggestions = stats.from_memory, stats.memory_suggestions
    report.texts_missing, report.alt_texts_translated = stats.missing, stats.alt_translated
    report.errors, report.missing_texts = stats.errors, stats.missing_texts
    report.memory_suggestion_texts = stats.memory_suggestion_texts
    report.glossary_usage = stats.glossary_usage
    if glossary and result.output_written:
        report.glossary_usage.finalize_required_terms(glossary)
    return report


def _to_review_segment(entry: ReviewEntry, source_path: Path, destination_path: Path, options: TranslationOptions) -> ReviewSegment:
    metadata = entry.kind == METADATA_KIND
    return ReviewSegment(
        segment_id=METADATA_SEGMENT_ID if metadata else format_segment_id(entry.chapter_index, entry.segment_index),
        source_epub=str(source_path), output_epub=str(destination_path), chapter_index=entry.chapter_index,
        chapter_name=METADATA_CHAPTER_NAME if metadata else entry.chapter_name,
        document_name=entry.logical_key if metadata else entry.chapter_name, document_path=entry.logical_key,
        segment_kind=METADATA_SEGMENT_KIND if metadata else _REVIEW_KINDS.get(entry.kind, "text"),
        source_language=options.source, target_language=options.target, original=entry.original,
        translated=entry.translated, from_cache=entry.from_cache)


def translate_book(source_path: Path, destination_path: Path, translator: object, cache: object, options: TranslationOptions, *,
                   formats: FormatService, source: FormatSource, publication: FormatPublication, project_id: str,
                   glossary: object | None = None,
                   on_chapter_start: Callable[[int, int, str], None] | None = None,
                   on_chapter_done: Callable[[int, int, str, HtmlTranslationStats], None] | None = None,
                   on_text_processed: Callable[[str], None] | None = None,
                   review_segments: list[ReviewSegment] | None = None,
                   translator_factory: Callable[[], object] | None = None,
                   cache_factory: Callable[[], object] | None = None) -> tuple[TranslationReport, EngineResult]:
    """The engine in the current report's terms. A failure is in the result, and nothing was published."""
    _validate_worker_options(options, translator_factory=translator_factory, cache_factory=cache_factory)
    package = _read_package(source_path)
    memory = TranslationMemory(cache, options.translation_memory) if options.translation_memory is not None else None
    pool: ExecutorPool | None = None
    if options.workers > 1:
        pool = LegacyExecutorPool(translator_factory, cache_factory, options, glossary=glossary)
    observer = EngineObserver(
        on_chapter_start, None if on_chapter_done is None else
        lambda index, total, name, stats: on_chapter_done(index, total, name, to_html_stats(stats)), on_text_processed)
    engine = TranslationEngine(formats, LegacyUnitExecutor(translator, cache, options, glossary=glossary, memory=memory),
                               pool=pool, observer=observer)
    result = engine.run(EngineRequest(
        EPUB_ADAPTER_ID, source, publication, project_id, package.facts, options.translate_metadata,
        options.translate_alt_text, options.chapter_selection, options.fail_fast,
        options.cache_only and options.require_full_cache, options.workers))
    if review_segments is not None:
        review_segments.extend(_to_review_segment(entry, source_path, destination_path, options) for entry in result.review)
    return _to_report(result, source_path, destination_path, package, options, glossary), result
