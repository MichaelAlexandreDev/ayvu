"""A cancellable fake operation and two consumers that know only the event stream."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID

from ayvu.job import JobState, JobTerminalSummary, TranslationJob, transition_job
from ayvu.job_events import (
    JobCorrelation, JobEvent, JobEventKind, JobFailure, JobOutcome, JobProgress, JobWarning, normalize_failure,
)
from ayvu.job_execution import CancellationSource, JobEventSink, JobEventSource

SAFE_POINTS = ('before_document', 'after_document')
START = datetime(2026, 11, 3, 12, tzinfo=UTC)


class FakeOperation:
    """The single producer of its job's events, as the contract requires."""

    def __init__(self, job: TranslationJob, sink: JobEventSink, source: CancellationSource, *, documents: int = 3,
                 on_safe_point: Callable[[str, int], None] | None = None, fail_at: int | None = None,
                 error: BaseException | None = None) -> None:
        self.job = job
        self.sink = sink
        self.source = source
        self.documents = documents
        self.on_safe_point = on_safe_point
        self.fail_at = fail_at
        self.error = error
        self.stopped_at: tuple[str, int] | None = None
        self.documents_done = 0
        self._sequence = 0
        self._tick = 0

    def _now(self) -> datetime:
        self._tick += 1
        return START + timedelta(seconds=self._tick)

    def _emit(self, payload: object) -> None:
        self._sequence += 1
        self.sink.emit(JobEvent(str(UUID(int=10_000 + self._sequence)), self.job.job_id, self._sequence, self._now(),
                                JobCorrelation(self.job.request.project_id), payload))

    def _move(self, state: JobState, summary: JobTerminalSummary | None = None) -> None:
        self.job = transition_job(self.job, state, expected_revision=self.job.revision, now=self._now(),
                                  summary=summary)

    def _finish(self, outcome: JobOutcome, *path: JobState) -> JobOutcome:
        settlement = self.source.settle(outcome)
        if settlement.first:
            for state in path:
                self._move(state)
            self._move(outcome.state, outcome.summary)
            self._emit(outcome)
        return settlement.outcome

    def _stop_if_requested(self, safe_point: str, document: int) -> JobOutcome | None:
        if self.on_safe_point is not None:
            self.on_safe_point(safe_point, document)
        acknowledgement = self.source.token.checkpoint(safe_point)
        if acknowledgement is None:
            return None
        self.stopped_at = (safe_point, document)
        self._emit(self.source.requested)
        self._emit(acknowledgement)
        return self._finish(JobOutcome(JobState.CANCELLED, JobTerminalSummary(acknowledgement.reason_code)),
                            JobState.CANCELLING)

    def run(self) -> JobOutcome:
        self._move(JobState.PREFLIGHT)
        self._move(JobState.RUNNING)
        self._emit(JobProgress('translate', 'documents', 0, self.documents))
        for document in range(self.documents):
            stop = self._stop_if_requested('before_document', document)
            if stop is not None:
                return stop
            if document == self.fail_at:
                failure = normalize_failure(self.error)
                return self._finish(JobOutcome(JobState.FAILED, JobTerminalSummary(failure.code), failure))
            self.documents_done += 1
            self._emit(JobProgress('translate', 'documents', self.documents_done, self.documents))
            if document == 0:
                self._emit(JobWarning('glossary_term_skipped'))
            stop = self._stop_if_requested('after_document', document)
            if stop is not None:
                return stop
        self._move(JobState.COMPLETED)
        self._move(JobState.VALIDATING)
        return self._finish(JobOutcome(JobState.VALIDATED, JobTerminalSummary(
            'validated', (('documents_done', self.documents_done),))))


class ProgressView:
    """Stands in for a progress display."""

    def __init__(self, source: JobEventSource) -> None:
        self.source = source
        self.position = 0
        self.latest: dict[str, JobProgress] = {}

    def pull(self) -> None:
        for event in self.source.replay(self.position):
            self.position = event.sequence
            if event.kind is JobEventKind.PROGRESS:
                self.latest[event.payload.phase] = event.payload


class OutcomeAudit:
    """Stands in for a report: it reads the whole stream each time."""

    def __init__(self, source: JobEventSource) -> None:
        self.source = source

    def outcome(self) -> JobOutcome | None:
        terminal = [event.payload for event in self.source.replay() if event.kind is JobEventKind.OUTCOME]
        assert len(terminal) <= 1
        return terminal[0] if terminal else None

    def sequences(self) -> list[int]:
        return [event.sequence for event in self.source.replay()]

    def warnings(self) -> list[str]:
        return [event.payload.code for event in self.source.replay() if event.kind is JobEventKind.WARNING]

    def failure(self) -> JobFailure | None:
        outcome = self.outcome()
        return outcome.failure if outcome else None
