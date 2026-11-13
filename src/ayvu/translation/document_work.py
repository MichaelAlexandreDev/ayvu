"""Whole-document work and ordered presentation, independent of the engine loop."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field

from ..formats import UnitTranslation
from ..jobs import DEFAULT_EVENT_LOG_CAPACITY, JobEventAcceptance, JobProgress, JobEvent
from ..kernel import JobFailure, JobFailureFamily, JobWarning
from .document_contracts import TranslationExecutionError
from .document_scheduler import DocumentScheduler
from .engine import ALT_TEXT_KIND, ENGINE_BEFORE_CHAPTER, ENGINE_BEFORE_UNIT, STATUS_MISSING, TextStats, UnitExecution


@dataclass(repr=False)
class DocumentWorkResult:
    document_index: int
    executions: tuple[UnitExecution, ...]
    translations: list[UnitTranslation] = field(default_factory=list)
    unit_sources: list[tuple[str, str]] = field(default_factory=list)
    stats: TextStats = field(default_factory=TextStats)
    statuses: list[str] = field(default_factory=list)
    # Unit index, local event index, event. Only warnings enter this bounded buffer.
    warnings: tuple[tuple[int, int, JobEvent], ...] = ()


class DocumentEvents:
    def __init__(self):
        self.unit_index = 0
        self.warnings = []

    def begin_unit(self, index):
        self.unit_index = index

    def emit(self, event):
        if isinstance(event.payload, JobWarning):
            if len(self.warnings) >= DEFAULT_EVENT_LOG_CAPACITY:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.RESOURCE, "document_event_limit"))
            self.warnings.append((self.unit_index, len(self.warnings), event))
        elif isinstance(event.payload, JobProgress):
            self.unit_index = event.payload.completed
        return JobEventAcceptance.APPLIED


def document_result(document, index, executions, warnings=()):
    result = DocumentWorkResult(index, tuple(executions), warnings=tuple(warnings))
    for unit, execution in zip(document.units, result.executions, strict=True):
        result.translations.append(UnitTranslation(unit.unit_id, execution.reconstruction_text))
        result.unit_sources.append((unit.unit_id, execution.status))
        result.stats.record(execution)
        if unit.content.kind.name == ALT_TEXT_KIND and execution.status != STATUS_MISSING:
            result.stats.alt_translated += 1
        result.statuses.append(execution.status)
    return result


class DocumentWork:
    """The engine's complete document port; factories are called inside tasks.

    Workers own local events and executors. Only consume/run, on the coordinator,
    call the public sink and observer. No worker owns a CLI callback.
    """

    def __init__(self, factory, context, *, checkpoint=lambda: None):
        self.factory, self.context = factory, context
        self.scheduler = DocumentScheduler(checkpoint)

    def execute(self, document, index=0, check=None):
        check = check or self.scheduler.check
        check()  # Before even opening a worker or its adapters.
        events = DocumentEvents()
        with self.factory(events, check) as executor:
            check()
            executions = executor.execute_document(document, index)
            check()
            return document_result(document, index, executions, events.warnings)

    def consume(self, result, on_status=lambda _: None):
        warnings = iter(sorted(result.warnings, key=lambda item: item[:2]))
        pending = next(warnings, None)
        for index, status in enumerate(result.statuses):
            while pending is not None and pending[0] <= index:
                self.context.events.emit(pending[2])
                pending = next(warnings, None)
            on_status(status)
        while pending is not None:
            self.context.events.emit(pending[2])
            pending = next(warnings, None)

    def title(self, document):
        result = self.execute(document, 0)
        self.consume(result)
        return result.executions[0]

    def run(self, request, chapters, result, observer, engine_checkpoint=lambda point: None):
        def execute(chapter, check):
            return self.execute(chapter.document, chapter.document_index, check)

        def admit(chapter):
            # The engine's safe points and the chapter start stay on the coordinator, in book order.
            engine_checkpoint(ENGINE_BEFORE_CHAPTER)
            if observer.on_chapter_start:
                observer.on_chapter_start(chapter.index, len(chapters), chapter.name)
            engine_checkpoint(ENGINE_BEFORE_UNIT)

        with closing(self.scheduler.ordered(chapters, request.workers, execute, admit)) as ordered:
            for chapter, outcome in ordered:
                self.scheduler.check()
                self.consume(outcome, observer.on_text_processed or (lambda _: None))
                result.stats.merge(outcome.stats)
                result.chapters_processed += 1
                if observer.on_chapter_done:
                    observer.on_chapter_done(chapter.index, len(chapters), chapter.name, outcome.stats)
                yield chapter, outcome
        self.scheduler._checkpoint()  # Final authority check before reconstruction.
