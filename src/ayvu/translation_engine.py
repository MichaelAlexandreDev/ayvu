"""Translates one book through a format adapter, one unit at a time, with real project identity.

The adapter owns the structure and the executor owns the language work, so nothing here parses
markup or calls a provider. It keeps the current flow's rules: a failed text stays in the source
language and the run goes on, unless the run is fail-fast. It is not ``DocumentTranslator``, which
stops at the first failure and has no production cache until #153.

Nothing in the current translation uses it yet. #145 migrates callers.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from .artifact import Artifact
from .content import ContentDocument, TranslationUnit
from .domain import ChapterSelection, ChapterSelectionError
from .format_adapter import ContentScope, ExtractionRequest, ReconstructionRequest, UnitTranslation
from .format_service import FormatPublication, FormatService, FormatSource
from .job_events import JobFailure, JobFailureFamily

STATUS_TRANSLATED = "translated"
STATUS_CACHE = "cache"
STATUS_MEMORY = "memory"
STATUS_MISSING = "missing"
STATUS_ERROR = "error"
METADATA_KIND = "title"
ALT_TEXT_KIND = "alt_text"
TITLE_HOLDS_OTHER_NODES = "the title holds a comment, element or entity, so it stays untranslated"


@dataclass(frozen=True, repr=False)
class UnitExecution:
    """``reconstruction_text`` goes to the adapter. The others are what the run reports about it."""

    reconstruction_text: str
    status: str
    original: str
    translated: str
    missing_text: str | None = None
    memory_suggestion: str | None = None
    glossary_usage: object | None = None

    def __repr__(self) -> str:
        return f"UnitExecution(status={self.status!r})"


class UnitExecutor(Protocol):
    def execute(self, unit: TranslationUnit) -> UnitExecution: ...
    def untranslated(self, unit: TranslationUnit) -> str: ...


class ExecutorPool(Protocol):
    """Each worker gets its own executor, because a provider session or a cache cannot be shared."""

    def worker(self) -> AbstractContextManager[UnitExecutor]: ...


@dataclass(frozen=True, repr=False)
class BookFacts:
    """What the application needs about a book that its documents do not say."""

    document_names: tuple[tuple[str, str], ...] = ()
    title_holds_other_nodes: bool = False

    def __repr__(self) -> str:
        return f"BookFacts(documents={len(self.document_names)})"


@dataclass(frozen=True, repr=False)
class EngineRequest:
    adapter_id: str
    source: FormatSource
    publication: FormatPublication
    project_id: str
    facts: BookFacts = BookFacts()
    translate_metadata: bool = False
    translate_alt_text: bool = False
    chapter_selection: ChapterSelection | None = None
    fail_fast: bool = False
    require_full_cache: bool = False
    workers: int = 1

    def __repr__(self) -> str:
        return f"EngineRequest(adapter_id={self.adapter_id!r}, workers={self.workers})"


@dataclass(frozen=True, repr=False)
class ReviewEntry:
    chapter_index: int
    segment_index: int
    chapter_name: str
    logical_key: str
    kind: str
    original: str
    translated: str
    from_cache: bool

    def __repr__(self) -> str:
        return f"ReviewEntry(chapter_index={self.chapter_index}, segment_index={self.segment_index})"


@dataclass(repr=False)
class TextStats:
    translated: int = 0
    from_cache: int = 0
    from_memory: int = 0
    memory_suggestions: int = 0
    alt_translated: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)
    missing_texts: list[str] = field(default_factory=list)
    memory_suggestion_texts: list[str] = field(default_factory=list)
    glossary_usages: list[object] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"TextStats(translated={self.translated}, missing={self.missing}, errors={len(self.errors)})"

    def record(self, execution: UnitExecution) -> None:
        if execution.memory_suggestion is not None:
            self.memory_suggestions += 1
            self.memory_suggestion_texts.append(execution.memory_suggestion)
        if execution.status == STATUS_MEMORY:
            self.from_memory += 1
        elif execution.status == STATUS_MISSING:
            self.missing += 1
            self.missing_texts.append(execution.missing_text or "")
        elif execution.status == STATUS_CACHE:
            self.from_cache += 1
        else:
            self.translated += 1
        if execution.glossary_usage is not None:
            self.glossary_usages.append(execution.glossary_usage)

    def merge(self, other: TextStats) -> None:
        self.translated += other.translated
        self.from_cache += other.from_cache
        self.from_memory += other.from_memory
        self.memory_suggestions += other.memory_suggestions
        self.alt_translated += other.alt_translated
        self.missing += other.missing
        self.errors.extend(other.errors)
        self.missing_texts.extend(other.missing_texts)
        self.memory_suggestion_texts.extend(other.memory_suggestion_texts)
        self.glossary_usages.extend(other.glossary_usages)


@dataclass(repr=False)
class EngineResult:
    stats: TextStats = field(default_factory=TextStats)
    chapters_processed: int = 0
    review: list[ReviewEntry] = field(default_factory=list)
    output_written: bool = False
    artifact: Artifact | None = None
    failure: JobFailure | None = None

    def __repr__(self) -> str:
        return f"EngineResult(chapters_processed={self.chapters_processed}, output_written={self.output_written})"


@dataclass(frozen=True)
class EngineObserver:
    on_chapter_start: Callable[[int, int, str], None] | None = None
    on_chapter_done: Callable[[int, int, str, TextStats], None] | None = None
    on_text_processed: Callable[[str], None] | None = None


@dataclass(frozen=True, repr=False)
class _Chapter:
    index: int
    name: str
    document: ContentDocument


@dataclass(repr=False)
class _ChapterOutcome:
    stats: TextStats = field(default_factory=TextStats)
    translations: list[UnitTranslation] = field(default_factory=list)
    review: list[ReviewEntry] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)


def _scopes(request: EngineRequest) -> frozenset[ContentScope]:
    scopes = {ContentScope.BODY}
    if request.translate_metadata:
        scopes |= {ContentScope.METADATA, ContentScope.NAVIGATION}
    if request.translate_alt_text:
        scopes.add(ContentScope.IMAGE_ALT_TEXT)
    return frozenset(scopes)


def _is_metadata(document: ContentDocument) -> bool:
    return any(unit.content.kind.name == METADATA_KIND for unit in document.units)


def _translate_chapter(executor: UnitExecutor, chapter: _Chapter, *, fail_fast: bool,
                       on_status: Callable[[str], None]) -> _ChapterOutcome:
    outcome = _ChapterOutcome()
    for unit in chapter.document.units:
        try:
            execution = executor.execute(unit)
        except Exception as error:
            outcome.stats.errors.append(str(error))
            on_status(STATUS_ERROR)
            outcome.translations.append(UnitTranslation(unit.unit_id, executor.untranslated(unit)))
            if fail_fast:
                raise
            continue
        outcome.translations.append(UnitTranslation(unit.unit_id, execution.reconstruction_text))
        outcome.stats.record(execution)
        kind = unit.content.kind.name
        if kind == ALT_TEXT_KIND and execution.status != STATUS_MISSING:
            outcome.stats.alt_translated += 1
        outcome.review.append(ReviewEntry(
            chapter.index, len(outcome.review) + 1, chapter.name, chapter.document.logical_key, kind,
            execution.original, execution.translated, execution.status == STATUS_CACHE))
        on_status(execution.status)
    return outcome


class TranslationEngine:
    def __init__(self, formats: FormatService, executor: UnitExecutor, *, pool: ExecutorPool | None = None,
                 observer: EngineObserver | None = None) -> None:
        self._formats = formats
        self._executor = executor
        self._pool = pool
        self._observer = observer or EngineObserver()

    def __repr__(self) -> str:
        return "TranslationEngine()"

    def _status(self, status: str) -> None:
        if self._observer.on_text_processed:
            self._observer.on_text_processed(status)

    def run(self, request: EngineRequest) -> EngineResult:
        """Nothing is sent to a provider before the book has been read and the selection resolved."""
        if request.workers < 1 or (request.workers > 1 and self._pool is None):
            raise ValueError("a worker pool is required for more than one worker")
        result = EngineResult()
        extraction = self._formats.extract(request.adapter_id, request.source,
                                           ExtractionRequest(request.project_id, _scopes(request)))
        if extraction.failure is not None:
            result.failure = extraction.failure
            return result
        title = next((document for document in extraction.documents if _is_metadata(document)), None)
        chapters = self._chapters(request, [document for document in extraction.documents if document is not title], result)
        if chapters is None:
            return result
        materialized: list[ContentDocument] = []
        translations: list[UnitTranslation] = []
        if request.translate_metadata:
            self._translate_title(request, title, result, materialized, translations)
        run_chapters = self._run_parallel if request.workers > 1 else self._run_sequential
        for chapter, outcome in run_chapters(request, chapters, result):
            materialized.append(chapter.document)
            translations.extend(outcome.translations)
        if request.require_full_cache and result.stats.missing > 0:
            return result
        if not materialized:
            result.failure = JobFailure(JobFailureFamily.INPUT, "nothing_to_materialize")
            return result
        materialization = self._formats.reconstruct(
            request.adapter_id, request.source, ReconstructionRequest(tuple(materialized), tuple(translations)),
            request.publication)
        result.failure, result.artifact = materialization.failure, materialization.artifact
        result.output_written = materialization.failure is None
        return result

    def _chapters(self, request: EngineRequest, documents: list[ContentDocument], result: EngineResult) -> list[_Chapter] | None:
        names = dict(request.facts.document_names)
        titles: dict[str, str | None] = {}
        selection = request.chapter_selection
        if selection is not None:
            inspection = self._formats.inspect(request.adapter_id, request.source)
            if inspection.failure is not None:
                result.failure = inspection.failure
                return None
            titles = {item.logical_key: item.title for item in inspection.inventory.documents}
        numbered = [(index, names.get(document.logical_key, document.logical_key), document)
                    for index, document in enumerate(documents, start=1)]
        if selection is not None:
            numbered = [item for item in numbered
                        if selection.matches(item[0], item[1], item[2].logical_key, titles.get(item[2].logical_key))]
            if not numbered:
                raise ChapterSelectionError(f"no chapters matched selection: {selection.source}")
        return [_Chapter(index, name, document) for index, (_, name, document) in enumerate(numbered, start=1)]

    def _translate_title(self, request: EngineRequest, title: ContentDocument | None, result: EngineResult,
                         materialized: list[ContentDocument], translations: list[UnitTranslation]) -> None:
        def failed(error: Exception) -> None:
            result.stats.errors.append(f"metadata title: {error}")
            self._status(STATUS_ERROR)
            if request.fail_fast:
                raise error

        if request.facts.title_holds_other_nodes:
            failed(ValueError(TITLE_HOLDS_OTHER_NODES))
            return
        if title is None:
            return
        unit = title.units[0]
        try:
            execution = self._executor.execute(unit)
        except Exception as error:
            failed(error)
            return
        result.review.append(ReviewEntry(0, 0, "", title.logical_key, METADATA_KIND, execution.original, execution.translated,
                                         execution.status == STATUS_CACHE))
        result.stats.record(execution)
        self._status(execution.status)
        # A title that does not change leaves the package out, so it keeps its bytes.
        if execution.status != STATUS_MISSING and execution.translated != unit.source_text:
            materialized.append(title)
            translations.append(UnitTranslation(unit.unit_id, execution.reconstruction_text))

    def _finished(self, chapter: _Chapter, outcome: _ChapterOutcome, total: int, result: EngineResult) -> None:
        result.stats.merge(outcome.stats)
        result.review.extend(outcome.review)
        result.chapters_processed += 1
        if self._observer.on_chapter_done:
            self._observer.on_chapter_done(chapter.index, total, chapter.name, outcome.stats)

    def _start(self, chapter: _Chapter, total: int) -> None:
        if self._observer.on_chapter_start:
            self._observer.on_chapter_start(chapter.index, total, chapter.name)

    def _run_sequential(self, request: EngineRequest, chapters: list[_Chapter],
                        result: EngineResult) -> Iterator[tuple[_Chapter, _ChapterOutcome]]:
        for chapter in chapters:
            self._start(chapter, len(chapters))
            try:
                outcome = _translate_chapter(self._executor, chapter, fail_fast=request.fail_fast, on_status=self._status)
            except Exception as error:
                result.stats.errors.append(f"{chapter.name}: {error}")
                if request.fail_fast:
                    raise
                continue
            self._finished(chapter, outcome, len(chapters), result)
            yield chapter, outcome

    def _run_parallel(self, request: EngineRequest, chapters: list[_Chapter],
                      result: EngineResult) -> Iterator[tuple[_Chapter, _ChapterOutcome]]:
        def work(chapter: _Chapter) -> _ChapterOutcome:
            statuses: list[str] = []
            with self._pool.worker() as executor:
                outcome = _translate_chapter(executor, chapter, fail_fast=request.fail_fast, on_status=statuses.append)
            outcome.statuses = statuses
            return outcome

        with ThreadPoolExecutor(max_workers=request.workers) as threads:
            futures: list[tuple[_Chapter, Future[_ChapterOutcome]]] = [(chapter, threads.submit(work, chapter)) for chapter in chapters]
            # Reported in book order, whichever worker finished first.
            for chapter, future in futures:
                self._start(chapter, len(chapters))
                try:
                    outcome = future.result()
                except Exception as error:
                    result.stats.errors.append(f"{chapter.name}: {error}")
                    if request.fail_fast:
                        raise
                    continue
                for status in outcome.statuses:
                    self._status(status)
                self._finished(chapter, outcome, len(chapters), result)
                yield chapter, outcome
