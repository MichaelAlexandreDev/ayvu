"""Translation jobs: their states and events, cancellation sources and the repository port. The SQLite store is
the adapter module jobs.store.
"""
from __future__ import annotations

from .events import (
    CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobEventAcceptance,
    JobOutcome, JobProgress,
)
from .execution import CancellationSource, DEFAULT_EVENT_LOG_CAPACITY, InMemoryJobEventLog, JobEventSink
from .model import (
    JobLeaseError, JobNotFoundError, JobRequest, JobState, JobStoreError, JobTerminalSummary,
    MAX_LEASE_DURATION, TranslationJob,
)
from .service import JobRepository, JobSnapshot

__all__ = [
    "CancellationAcknowledgement", "CancellationRequest", "CancellationSource", "DEFAULT_EVENT_LOG_CAPACITY",
    "InMemoryJobEventLog", "JobCorrelation", "JobEvent", "JobEventAcceptance", "JobEventSink",
    "JobLeaseError", "JobNotFoundError", "JobOutcome", "JobProgress", "JobRepository", "JobRequest",
    "JobSnapshot", "JobState", "JobStoreError", "JobTerminalSummary", "MAX_LEASE_DURATION", "TranslationJob",
]
