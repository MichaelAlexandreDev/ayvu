"""Framework-neutral event boundary and cooperative cancellation for one job."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Lock
from typing import Protocol

from .job import JobError, JobState, is_slug
from .job_events import (
    CancellationAcknowledgement, CancellationRequest, JobEvent, JobEventAcceptance, JobEventError, JobEventOrderError,
    JobEventSequence, JobOutcome,
)

DEFAULT_EVENT_LOG_CAPACITY = 16384


class JobEventSink(Protocol):
    def emit(self, event: JobEvent) -> JobEventAcceptance: ...


class JobEventSource(Protocol):
    def replay(self, after_sequence: int = 0) -> tuple[JobEvent, ...]: ...


class InMemoryJobEventLog:
    """Consumers pull with ``replay``, so a failing consumer cannot disturb the producer."""

    def __init__(self, job_id: str, *, capacity: int = DEFAULT_EVENT_LOG_CAPACITY) -> None:
        if type(capacity) is not int or capacity < 1:
            raise JobEventError("invalid_capacity")
        self._sequence = JobEventSequence(job_id)
        self._capacity = capacity
        self._events: list[JobEvent] = []
        self._lock = Lock()

    def __repr__(self) -> str:
        return f"InMemoryJobEventLog(job_id={self._sequence.job_id!r})"

    @property
    def outcome(self) -> JobOutcome | None:
        with self._lock:
            return self._sequence.outcome

    def emit(self, event: JobEvent) -> JobEventAcceptance:
        with self._lock:
            # A replay of a stored event is still answered when the log is full.
            if (len(self._events) >= self._capacity and type(event) is JobEvent
                    and event.event_id not in self._sequence):
                raise JobEventOrderError("event_log_full")
            acceptance = self._sequence.accept(event)
            if acceptance is JobEventAcceptance.APPLIED:
                self._events.append(event)
            return acceptance

    def replay(self, after_sequence: int = 0) -> tuple[JobEvent, ...]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise JobEventError("invalid_sequence")
        with self._lock:
            # Sequences are contiguous from 1, so the position is the sequence.
            return tuple(self._events[after_sequence:])


class JobCancellationError(JobError):
    pass


class CancellationStatus(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_REQUESTED = "already_requested"
    TOO_LATE = "too_late"


@dataclass(frozen=True)
class CancellationResult:
    status: CancellationStatus
    request: CancellationRequest | None


@dataclass(frozen=True)
class JobSettlement:
    """``first`` tells exactly one caller to emit the terminal event."""

    outcome: JobOutcome
    first: bool


class CancellationToken:
    """The only view an operation gets: a request is visible at safe points and nowhere else."""

    def __init__(self, source: CancellationSource) -> None:
        self._source = source

    def __repr__(self) -> str:
        return "CancellationToken()"

    def checkpoint(self, safe_point: str) -> CancellationAcknowledgement | None:
        return self._source._observe(safe_point)


class CancellationSource:
    """Serializes requests, acknowledgement and the terminal decision of one operation.

    The lock decides every race: whichever of a request and a settlement arrives
    first is the fact the other one observes, so no contradiction can be recorded.
    """

    def __init__(self, safe_points: tuple[str, ...]) -> None:
        if (type(safe_points) is not tuple or not safe_points
                or not all(is_slug(point) for point in safe_points)
                or len(set(safe_points)) != len(safe_points)):
            raise JobCancellationError("invalid_safe_points")
        self._safe_points = frozenset(safe_points)
        self._lock = Lock()
        self._request: CancellationRequest | None = None
        self._acknowledgement: CancellationAcknowledgement | None = None
        self._outcome: JobOutcome | None = None
        self.token = CancellationToken(self)

    def __repr__(self) -> str:
        return "CancellationSource()"

    @property
    def requested(self) -> CancellationRequest | None:
        with self._lock:
            return self._request

    @property
    def acknowledgement(self) -> CancellationAcknowledgement | None:
        with self._lock:
            return self._acknowledgement

    @property
    def outcome(self) -> JobOutcome | None:
        with self._lock:
            return self._outcome

    def request(self, reason_code: str, now: datetime) -> CancellationResult:
        """Idempotent: the first reason and time stand; a request after the outcome changes nothing."""
        request = CancellationRequest(reason_code, now)
        with self._lock:
            if self._outcome is not None:
                return CancellationResult(CancellationStatus.TOO_LATE, self._request)
            if self._request is not None:
                return CancellationResult(CancellationStatus.ALREADY_REQUESTED, self._request)
            self._request = request
            return CancellationResult(CancellationStatus.ACCEPTED, request)

    def _observe(self, safe_point: str) -> CancellationAcknowledgement | None:
        if not isinstance(safe_point, str) or safe_point not in self._safe_points:
            raise JobCancellationError("undeclared_safe_point")
        with self._lock:
            if self._outcome is not None or self._request is None:
                return None
            if self._acknowledgement is None:
                self._acknowledgement = CancellationAcknowledgement(self._request.reason_code, safe_point)
            return self._acknowledgement

    def settle(self, outcome: JobOutcome) -> JobSettlement:
        """Record the terminal outcome once. A later call receives the first outcome unchanged."""
        if type(outcome) is not JobOutcome:
            raise JobEventError("invalid_outcome")
        with self._lock:
            if self._outcome is not None:
                return JobSettlement(self._outcome, False)
            if outcome.state is JobState.CANCELLED and self._request is None:
                raise JobCancellationError("cancellation_not_requested")
            # An acknowledged operation has moved to `cancelling`, whose only exit is `cancelled`.
            if self._acknowledgement is not None and outcome.state is not JobState.CANCELLED:
                raise JobCancellationError("outcome_contradicts_cancellation")
            self._outcome = outcome
            return JobSettlement(outcome, True)
