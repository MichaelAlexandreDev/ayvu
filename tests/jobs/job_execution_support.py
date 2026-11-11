from concurrent.futures import ThreadPoolExecutor
import ast
from datetime import UTC, datetime
import logging
from pathlib import Path
from threading import Barrier
from uuid import UUID
import pytest
from ayvu.jobs.model import ALLOWED_TRANSITIONS, JobRequest, JobState, JobTerminalSummary, new_job
from ayvu.jobs.events import (
    CancellationRequest, JobCorrelation, JobEvent, JobEventAcceptance, JobEventKind, JobEventOrderError, JobOutcome, JobProgress, encode_event)
from ayvu.jobs.execution import (
    CancellationSource, CancellationStatus, InMemoryJobEventLog)
from tests.support.job_contract_fakes import SAFE_POINTS, FakeOperation, OutcomeAudit, ProgressView
from ayvu.kernel import JobEventError, JobFailure, JobFailureFamily, JobOperationError, JobWarning, JobCancellationError

JOB = str(UUID(int=2))


PROJECT = str(UUID(int=3))


NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


VALIDATED = JobOutcome(JobState.VALIDATED, JobTerminalSummary('validated'))


CANCELLED = JobOutcome(JobState.CANCELLED, JobTerminalSummary('user_requested'))


FAILED = JobOutcome(JobState.FAILED, JobTerminalSummary('timeout'), JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout'))


def job():
    return new_job(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'a' * 64), JOB, NOW)


def event(sequence, payload=None, event_id=None):
    return JobEvent(event_id or str(UUID(int=1000 + sequence)), JOB, sequence, NOW, JobCorrelation(PROJECT),
                    payload or JobWarning('w'))


def harness(**options):
    log = InMemoryJobEventLog(JOB)
    source = CancellationSource(SAFE_POINTS)
    return log, source, FakeOperation(job(), log, source, **options)


CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub',
            'Translate this prompt', 'https://user:hunter2@example.test/translate', 'sk-secret-token')
