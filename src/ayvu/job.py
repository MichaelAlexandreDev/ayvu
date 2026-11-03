"""Framework-neutral durable translation job lifecycle values and rules.

Everything here is pure: callers supply time, identifiers and owner tokens.
Error messages are bounded codes and never include caller data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

MAX_OUTPUT_ROLES = 16
MAX_SUMMARY_COUNTERS = 16
MAX_LEASE_DURATION = timedelta(hours=1)
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_OWNER_TOKEN = re.compile(r"[0-9a-f]{64}")


class JobError(ValueError):
    """A bounded error: messages never include caller data."""


class JobValidationError(JobError):
    pass


class JobNotFoundError(JobError):
    pass


class JobTransitionError(JobError):
    pass


class JobRevisionConflictError(JobError):
    pass


class JobLeaseError(JobError):
    pass


class JobContinuationError(JobError):
    pass


class JobStoreError(JobError):
    pass


class JobState(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    COMPLETED = "completed"
    VALIDATING = "validating"
    VALIDATED = "validated"


TERMINAL_STATES = frozenset({
    JobState.VALIDATED, JobState.PARTIALLY_COMPLETED, JobState.FAILED, JobState.CANCELLED,
})

ALLOWED_TRANSITIONS = MappingProxyType({
    JobState.QUEUED: frozenset({JobState.PREFLIGHT, JobState.CANCELLED}),
    JobState.PREFLIGHT: frozenset({JobState.RUNNING, JobState.FAILED}),
    JobState.RUNNING: frozenset({
        JobState.PAUSING, JobState.CANCELLING, JobState.PARTIALLY_COMPLETED,
        JobState.FAILED, JobState.COMPLETED,
    }),
    JobState.PAUSING: frozenset({JobState.PAUSED}),
    JobState.PAUSED: frozenset({JobState.QUEUED}),
    JobState.CANCELLING: frozenset({JobState.CANCELLED}),
    JobState.COMPLETED: frozenset({JobState.VALIDATING}),
    JobState.VALIDATING: frozenset({JobState.VALIDATED, JobState.PARTIALLY_COMPLETED}),
    JobState.VALIDATED: frozenset(),
    JobState.PARTIALLY_COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
})


def validate_job_identity(value: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise JobValidationError("invalid_job_identity") from None


def _validate_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise JobValidationError("invalid_timestamp")


def _validate_revision(value: int) -> None:
    if type(value) is not int or not 1 <= value < 2**63:
        raise JobValidationError("invalid_revision")


def _is_slug(value: object) -> bool:
    return isinstance(value, str) and _SLUG.fullmatch(value) is not None


@dataclass(frozen=True, repr=False)
class JobRequest:
    """Immutable identity of the work; the fingerprint never reveals its inputs."""

    project_id: str
    command_type: str
    command_version: int
    input_fingerprint: str
    output_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_job_identity(self.project_id)
        if not _is_slug(self.command_type):
            raise JobValidationError("invalid_command_type")
        if type(self.command_version) is not int or not 1 <= self.command_version < 2**31:
            raise JobValidationError("invalid_command_version")
        if (not isinstance(self.input_fingerprint, str)
                or not _FINGERPRINT.fullmatch(self.input_fingerprint)):
            raise JobValidationError("invalid_fingerprint")
        if (type(self.output_roles) is not tuple or len(self.output_roles) > MAX_OUTPUT_ROLES
                or not all(_is_slug(role) for role in self.output_roles)
                or len(set(self.output_roles)) != len(self.output_roles)):
            raise JobValidationError("invalid_output_roles")

    def __repr__(self) -> str:
        return f"JobRequest(command_type={self.command_type!r}, command_version={self.command_version})"


@dataclass(frozen=True, repr=False)
class JobLease:
    owner_token: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.owner_token, str) or not _OWNER_TOKEN.fullmatch(self.owner_token):
            raise JobValidationError("invalid_owner_token")
        _validate_utc(self.acquired_at)
        _validate_utc(self.expires_at)
        if self.expires_at <= self.acquired_at:
            raise JobValidationError("invalid_lease")

    def __repr__(self) -> str:
        return f"JobLease(<redacted>, expires_at={self.expires_at.isoformat()!r})"

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True, repr=False)
class JobTerminalSummary:
    """Allowlisted codes and counts; the slug format excludes text and paths."""

    reason_code: str
    counters: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not _is_slug(self.reason_code):
            raise JobValidationError("invalid_terminal_summary")
        if type(self.counters) is not tuple or len(self.counters) > MAX_SUMMARY_COUNTERS:
            raise JobValidationError("invalid_terminal_summary")
        seen: set[str] = set()
        for entry in self.counters:
            if (type(entry) is not tuple or len(entry) != 2 or not _is_slug(entry[0])
                    or entry[0] in seen or type(entry[1]) is not int
                    or not 0 <= entry[1] < 2**63):
                raise JobValidationError("invalid_terminal_summary")
            seen.add(entry[0])

    def __repr__(self) -> str:
        return f"JobTerminalSummary(reason_code={self.reason_code!r})"


@dataclass(frozen=True, repr=False)
class TranslationJob:
    job_id: str
    request: JobRequest
    state: JobState
    revision: int
    created_at: datetime
    updated_at: datetime
    lease: JobLease | None = None
    terminal_summary: JobTerminalSummary | None = None
    continuation_of: str | None = None

    def __post_init__(self) -> None:
        validate_job_identity(self.job_id)
        if type(self.request) is not JobRequest:
            raise JobValidationError("invalid_request")
        if type(self.state) is not JobState:
            raise JobValidationError("unknown_state")
        _validate_revision(self.revision)
        _validate_utc(self.created_at)
        _validate_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise JobValidationError("invalid_timestamp_order")
        if self.lease is not None and type(self.lease) is not JobLease:
            raise JobValidationError("invalid_lease")
        if self.state in TERMINAL_STATES:
            if self.lease is not None:
                raise JobValidationError("terminal_job_has_lease")
        elif self.terminal_summary is not None:
            raise JobValidationError("summary_requires_terminal_state")
        if self.terminal_summary is not None and type(self.terminal_summary) is not JobTerminalSummary:
            raise JobValidationError("invalid_terminal_summary")
        if self.continuation_of is not None:
            validate_job_identity(self.continuation_of)
            if self.continuation_of == self.job_id:
                raise JobContinuationError("self_continuation")

    def __repr__(self) -> str:
        return f"TranslationJob(job_id={self.job_id!r}, state={self.state.value!r}, revision={self.revision})"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def new_job(request: JobRequest, job_id: str, now: datetime, *, continuation_of: str | None = None) -> TranslationJob:
    return TranslationJob(job_id=job_id, request=request, state=JobState.QUEUED, revision=1,
                          created_at=now, updated_at=now, continuation_of=continuation_of)


def transition_job(job: TranslationJob, target: JobState, *, expected_revision: int, now: datetime,
                   summary: JobTerminalSummary | None = None) -> TranslationJob:
    """Compare-and-swap one edge. A repeat of the current state returns ``job`` itself."""
    if type(target) is not JobState:
        raise JobValidationError("unknown_state")
    _validate_revision(expected_revision)
    _validate_utc(now)
    # Revision is checked first: a stale caller conflicts on every edge, valid or not.
    if expected_revision != job.revision:
        raise JobRevisionConflictError("stale_revision")
    if target is job.state:
        return job
    if target not in ALLOWED_TRANSITIONS[job.state]:
        raise JobTransitionError("forbidden_transition")
    if job.revision + 1 >= 2**63:
        raise JobValidationError("invalid_revision")
    return replace(job, state=target, revision=job.revision + 1,
                   # A clock moving backwards must not reorder the recorded history.
                   updated_at=max(now, job.updated_at),
                   lease=None if target in TERMINAL_STATES else job.lease,
                   terminal_summary=summary)


def _validate_duration(duration: timedelta) -> None:
    if not isinstance(duration, timedelta) or not timedelta(0) < duration <= MAX_LEASE_DURATION:
        raise JobLeaseError("invalid_lease_duration")


def requires_reconciliation(job: TranslationJob, now: datetime) -> bool:
    """Report an abandoned owner; only an explicit later decision may act on it."""
    _validate_utc(now)
    return not job.terminal and job.lease is not None and job.lease.expired(now)


def acquire_lease(job: TranslationJob, owner_token: str, now: datetime, duration: timedelta) -> TranslationJob:
    """Lease changes leave revision and updated_at alone: they are not lifecycle history."""
    _validate_utc(now)
    _validate_duration(duration)
    if job.terminal:
        raise JobLeaseError("job_terminal")
    if job.lease is not None:
        raise JobLeaseError("lease_requires_reconciliation" if job.lease.expired(now) else "lease_held")
    return replace(job, lease=JobLease(owner_token, now, now + duration))


def _held_lease(job: TranslationJob, owner_token: str, now: datetime) -> JobLease:
    _validate_utc(now)
    if job.lease is None or job.lease.owner_token != owner_token:
        raise JobLeaseError("lease_not_held")
    if job.lease.expired(now):
        raise JobLeaseError("lease_requires_reconciliation")
    return job.lease


def renew_lease(job: TranslationJob, owner_token: str, now: datetime, duration: timedelta) -> TranslationJob:
    _validate_duration(duration)
    lease = _held_lease(job, owner_token, now)
    return replace(job, lease=replace(lease, expires_at=now + duration))


def release_lease(job: TranslationJob, owner_token: str, now: datetime) -> TranslationJob:
    _held_lease(job, owner_token, now)
    return replace(job, lease=None)
