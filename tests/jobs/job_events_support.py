from datetime import UTC, datetime, timedelta, timezone
import json
from uuid import UUID
import pytest
from ayvu.jobs.model import JobRequest, JobState, JobTerminalSummary, new_job, transition_job
from ayvu.jobs.events import (
    MAX_EVENT_BYTES, CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobEventAcceptance, JobEventKind, JobEventOrderError, JobEventSequence, JobOutcome, JobProgress, decode_event, encode_event)
from ayvu.kernel import (
    DIAGNOSTIC_KEYS, JobEventError, JobFailure, JobFailureFamily, JobOperationError, JobWarning,
    job_diagnostics, normalize_failure,
)

JOB = str(UUID(int=2))


PROJECT = str(UUID(int=3))


NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub',
            'Translate this prompt', 'https://user:hunter2@example.test/translate', 'sk-secret-token')


def event(sequence=1, payload=None, *, event_id=None, job_id=JOB, project=PROJECT, at=NOW):
    return JobEvent(event_id or str(UUID(int=1000 + sequence)), job_id, sequence, at, JobCorrelation(project),
                    payload if payload is not None else JobProgress('translate', 'documents', sequence, None))


OUTCOMES = {
    JobState.VALIDATED: JobOutcome(JobState.VALIDATED, JobTerminalSummary('validated', (('z', 1), ('a', 2)))),
    JobState.PARTIALLY_COMPLETED: JobOutcome(JobState.PARTIALLY_COMPLETED, JobTerminalSummary('validation_findings')),
    JobState.FAILED: JobOutcome(JobState.FAILED, JobTerminalSummary('timeout'),
                                JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout', True,
                                           job_diagnostics(attempt=3, retry_after_ms=1500.5))),
    JobState.CANCELLED: JobOutcome(JobState.CANCELLED, JobTerminalSummary('user_requested')),
}


PAYLOADS = [
    JobProgress('translate', 'documents', 1, 3, job_diagnostics(units_done=4, partial_output_retained=True)),
    JobProgress('translate', 'documents', 0),
    JobProgress('validate', 'ratio', 0.5, 1.0),
    JobWarning('glossary_term_skipped', job_diagnostics(units_skipped=2)),
    CancellationRequest('user_requested', NOW),
    CancellationAcknowledgement('user_requested', 'between_documents'),
    *OUTCOMES.values(),
]
