"""Read a book for an offline estimate or an online preview without durable state.

Configured workers are retained as presentation data. Local documents run in
order, sequentially; this use case does not claim concurrent worker execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from ..formats import BookFacts, ContentScope, ExtractionRequest, FormatService, FormatSource, PreviewRequest, UnitTranslation
from ..kernel import TranslationOptions, ChapterSelectionError
from .engine import EngineObserver, EngineResult, TextStats
from .errors import BookRefused, TranslationDryRunCancelled
from .transient_units import TransientUnits


@dataclass(frozen=True, repr=False)
class TransientExecution:
    source: FormatSource
    options: TranslationOptions
    facts: BookFacts = BookFacts()
    project_id: str | None = None
    document_selection: tuple[str, ...] | None = None


class TransientPublication(Protocol):
    """Publish a checked preview without any durable execution identity."""

    def publish(self, source: FormatSource, request: PreviewRequest, *, cancellation=None): ...


class TransientTranslation:
    """Estimate locally, or publish a complete preview through an injected port."""

    def __init__(self, formats: FormatService, units: TransientUnits, *,
                 observer: EngineObserver = EngineObserver(), cancellation=None,
                 publication: TransientPublication | None = None):
        self.formats, self.units, self.observer, self.cancellation = formats, units, observer, cancellation
        self.units.checkpoint = self.checkpoint
        self.publication = publication
        self._translations = []

    def checkpoint(self, point):
        if self.cancellation is not None and self.cancellation.checkpoint(point) is not None:
            raise TranslationDryRunCancelled()

    def execute(self, request: TransientExecution) -> EngineResult:
        if not request.options.dry_run and self.publication is None:
            raise ValueError("transient_dry_run_required")
        self._translations = []
        options = request.options
        scopes = {ContentScope.BODY}
        if options.translate_metadata:
            scopes |= {ContentScope.METADATA, ContentScope.NAVIGATION}
        if options.translate_alt_text:
            scopes.add(ContentScope.IMAGE_ALT_TEXT)
        self.checkpoint("engine_before_chapter")
        extraction = self.formats.extract("epub", request.source,
            ExtractionRequest(request.project_id or str(uuid4()), frozenset(scopes)), cancellation=self.cancellation)
        if extraction.failure is not None:
            if extraction.failure.family.value == "cancelled":
                raise TranslationDryRunCancelled()
            raise BookRefused(extraction.failure.code)
        documents = self._selected(request, extraction.documents)
        result = EngineResult()
        titles = [item for item in documents if any(unit.content.kind.name == "title" for unit in item[1].units)]
        for _, title in titles:
            result.stats.merge(self._document_stats(title))
        documents = [item for item in documents if item not in titles]
        for index, (name, document) in enumerate(documents, 1):
            self.checkpoint("engine_before_chapter")
            if self.observer.on_chapter_start:
                self.observer.on_chapter_start(index, len(documents), name)
            stats = self._document_stats(document)
            result.stats.merge(stats)
            result.chapters_processed += 1
            if self.observer.on_chapter_done:
                self.observer.on_chapter_done(index, len(documents), name, stats)
        if not options.dry_run:
            self.checkpoint("engine_before_publication")
            self.publication.publish(request.source, PreviewRequest(extraction.documents, tuple(self._translations)),
                                     cancellation=self.cancellation)
            result.output_written = True
        return result

    def _document_stats(self, document):
        stats = TextStats()
        for unit in document.units:
            self.checkpoint("engine_before_unit")
            execution = self.units.execute(unit)
            if self.publication is not None:
                self._translations.append(UnitTranslation(unit.unit_id, execution.reconstruction_text))
            stats.record(execution)
            if unit.content.kind.name == "alt_text" and execution.status != "missing":
                stats.alt_translated += 1
            if self.observer.on_text_processed:
                self.observer.on_text_processed(execution.status)
        return stats

    def _selected(self, request, documents):
        names = dict(request.facts.document_names)
        titles = {}
        selection = request.options.chapter_selection
        if request.document_selection is not None:
            by_key = {doc.logical_key: doc for doc in documents}
            if any(key not in by_key for key in request.document_selection):
                raise BookRefused("preview_selection_unavailable")
            metadata = [(doc.logical_key, doc) for doc in documents
                        if any(unit.content.kind.name == "title" for unit in doc.units)]
            return metadata + [(names.get(key, key), by_key[key]) for key in request.document_selection if key in by_key]
        if selection is not None:
            inspection = self.formats.inspect("epub", request.source, cancellation=self.cancellation)
            if inspection.failure is not None:
                raise BookRefused(inspection.failure.code)
            titles = {item.logical_key: item.title for item in inspection.inventory.documents}
        title = [doc for doc in documents if any(unit.content.kind.name == "title" for unit in doc.units)]
        body = [doc for doc in documents if doc not in title]
        chosen = [(names.get(doc.logical_key, doc.logical_key), doc) for index, doc in enumerate(body, 1)
                  if selection is None or selection.matches(index, names.get(doc.logical_key, doc.logical_key),
                                                           doc.logical_key, titles.get(doc.logical_key))]
        if selection is not None and not chosen:
            raise ChapterSelectionError(f"no chapters matched selection: {selection.source}")
        return [(doc.logical_key, doc) for doc in title] + chosen
