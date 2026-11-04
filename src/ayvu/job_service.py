"""Framework-neutral job repository contract."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from .job import JobRequest, JobState, JobTerminalSummary, TranslationJob


@dataclass(frozen=True)
class JobSnapshot:
    """A job as read now. Reading never resolves ``requires_reconciliation``."""

    job: TranslationJob
    requires_reconciliation: bool


class JobRepository(Protocol):
    """Every operation is one short transaction and raises typed ``JobError`` values."""

    def create(self, request: JobRequest) -> TranslationJob: ...
    def get(self, job_id: str) -> JobSnapshot: ...
    def list_jobs(self, project_id: str, *, states: tuple[JobState, ...] | None = None,
                  limit: int = 256, newest_first: bool = False) -> tuple[JobSnapshot, ...]: ...
    def transition(self, job_id: str, target: JobState, *, expected_revision: int,
                   summary: JobTerminalSummary | None = None) -> TranslationJob: ...
    def acquire_lease(self, job_id: str, duration: timedelta) -> TranslationJob: ...
    def renew_lease(self, job_id: str, owner_token: str, duration: timedelta) -> TranslationJob: ...
    def release_lease(self, job_id: str, owner_token: str) -> TranslationJob: ...
    def create_continuation(self, terminal_job_id: str, request: JobRequest) -> TranslationJob: ...
    def get_continuation(self, job_id: str) -> JobSnapshot | None: ...
