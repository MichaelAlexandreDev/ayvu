"""Versioned, content-free job events, terminal outcomes and ordering rules.

Pure values: callers supply time and identifiers. Codes come from code, never
from documents or user input; numbers are the only free-form diagnostics, so
there is no field through which book text, paths or credentials could travel.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .job import (
    TERMINAL_STATES, JobError, JobState, JobTerminalSummary, TranslationJob, is_slug, validate_job_identity,
    validate_utc,
)

JOB_EVENT_SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 16 * 1024
MAX_DIAGNOSTICS = 16
MAX_DIAGNOSTICS_BYTES = 1024
MAX_METRIC = 2**63
DIAGNOSTIC_KEYS = frozenset({
    "attempt", "bytes_processed", "documents_done", "documents_total", "elapsed_ms",
    "partial_output_retained", "retry_after_ms", "units_done", "units_skipped", "units_total",
})


class JobEventError(JobError):
    """An invalid event value or serialized form."""


class JobEventOrderError(JobError):
    """An event that the per-job stream cannot accept."""


class JobFailureFamily(StrEnum):
    INPUT = "input"
    CAPABILITY = "capability"
    POLICY = "policy"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    RESOURCE = "resource"
    INTEGRITY = "integrity"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class JobEventKind(StrEnum):
    PROGRESS = "progress"
    WARNING = "warning"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLATION_ACKNOWLEDGED = "cancellation_acknowledged"
    OUTCOME = "outcome"


class JobEventAcceptance(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"


def _valid_metric(value: object) -> bool:
    if type(value) is int:
        return 0 <= value < MAX_METRIC
    # Negative zero would equal 0.0 yet serialize differently, breaking duplicate detection.
    return (type(value) is float and math.isfinite(value) and 0 <= value < MAX_METRIC
            and math.copysign(1.0, value) > 0)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def job_diagnostics(**values: int | float | bool) -> tuple[tuple[str, int | float | bool], ...]:
    return tuple(sorted(values.items()))


def _validate_diagnostics(items: tuple) -> None:
    if type(items) is not tuple or len(items) > MAX_DIAGNOSTICS:
        raise JobEventError("invalid_diagnostics")
    previous = ""
    for entry in items:
        if type(entry) is not tuple or len(entry) != 2:
            raise JobEventError("invalid_diagnostics")
        key, value = entry
        if not isinstance(key, str) or key not in DIAGNOSTIC_KEYS:
            raise JobEventError("unknown_diagnostic_key")
        if key <= previous or not (type(value) is bool or _valid_metric(value)):
            raise JobEventError("invalid_diagnostics")
        previous = key
    if len(_canonical(dict(items))) > MAX_DIAGNOSTICS_BYTES:
        raise JobEventError("invalid_diagnostics")


@dataclass(frozen=True, repr=False)
class JobProgress:
    """``total=None`` states explicitly that the amount of work is not known yet."""

    phase: str
    unit: str
    completed: int | float
    total: int | float | None = None
    diagnostics: tuple = ()

    def __post_init__(self) -> None:
        if not is_slug(self.phase) or not is_slug(self.unit):
            raise JobEventError("invalid_progress_label")
        if not _valid_metric(self.completed) or (self.total is not None and not _valid_metric(self.total)):
            raise JobEventError("invalid_progress_metric")
        if self.total is not None and self.completed > self.total:
            raise JobEventError("progress_exceeds_total")
        _validate_diagnostics(self.diagnostics)

    def __repr__(self) -> str:
        return f"JobProgress(phase={self.phase!r}, completed={self.completed!r}, total={self.total!r})"


@dataclass(frozen=True, repr=False)
class JobWarning:
    code: str
    diagnostics: tuple = ()

    def __post_init__(self) -> None:
        if not is_slug(self.code):
            raise JobEventError("invalid_code")
        _validate_diagnostics(self.diagnostics)

    def __repr__(self) -> str:
        return f"JobWarning(code={self.code!r})"


@dataclass(frozen=True, repr=False)
class JobFailure:
    """The sanitized envelope; a raw exception never crosses into this value."""

    family: JobFailureFamily
    code: str
    retryable: bool = False
    diagnostics: tuple = ()

    def __post_init__(self) -> None:
        if type(self.family) is not JobFailureFamily or type(self.retryable) is not bool:
            raise JobEventError("invalid_failure")
        if not is_slug(self.code) and self.code != "v2-admin-required":
            raise JobEventError("invalid_code")
        _validate_diagnostics(self.diagnostics)

    def __repr__(self) -> str:
        return f"JobFailure(family={self.family.value!r}, code={self.code!r})"


class JobOperationError(JobError):
    """Raised by adapters that have already reduced a failure to its envelope."""

    def __init__(self, failure: JobFailure) -> None:
        if type(failure) is not JobFailure:
            raise JobEventError("invalid_failure")
        self.failure = failure
        super().__init__(failure.code)


def normalize_failure(error: BaseException) -> JobFailure:
    """Never reads the message of an unknown exception: it may quote the book or a URL."""
    if isinstance(error, JobOperationError):
        return error.failure
    if isinstance(error, JobError) and is_slug(str(error)):
        return JobFailure(JobFailureFamily.INTERNAL, str(error))
    return JobFailure(JobFailureFamily.INTERNAL, "unexpected_error")


@dataclass(frozen=True, repr=False)
class CancellationRequest:
    reason_code: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not is_slug(self.reason_code):
            raise JobEventError("invalid_code")
        validate_utc(self.requested_at)

    def __repr__(self) -> str:
        return f"CancellationRequest(reason_code={self.reason_code!r})"


@dataclass(frozen=True, repr=False)
class CancellationAcknowledgement:
    """The operation reached this safe point and committed to stopping."""

    reason_code: str
    safe_point: str

    def __post_init__(self) -> None:
        if not is_slug(self.reason_code) or not is_slug(self.safe_point):
            raise JobEventError("invalid_code")

    def __repr__(self) -> str:
        return f"CancellationAcknowledgement(safe_point={self.safe_point!r})"


@dataclass(frozen=True, repr=False)
class JobOutcome:
    """Terminal payload: one of the four terminal job states and nothing else."""

    state: JobState
    summary: JobTerminalSummary
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not JobState or self.state not in TERMINAL_STATES:
            raise JobEventError("outcome_requires_terminal_state")
        if type(self.summary) is not JobTerminalSummary:
            raise JobEventError("invalid_outcome")
        if self.state is JobState.FAILED:
            if (type(self.failure) is not JobFailure or self.failure.family is JobFailureFamily.CANCELLED
                    or self.failure.code != self.summary.reason_code):
                raise JobEventError("invalid_outcome")
        elif self.failure is not None:
            raise JobEventError("invalid_outcome")

    def __repr__(self) -> str:
        return f"JobOutcome(state={self.state.value!r}, reason_code={self.summary.reason_code!r})"

    def matches(self, job: TranslationJob) -> bool:
        return job.state is self.state and job.terminal_summary == self.summary


_PAYLOAD_KINDS = {
    JobProgress: JobEventKind.PROGRESS,
    JobWarning: JobEventKind.WARNING,
    CancellationRequest: JobEventKind.CANCELLATION_REQUESTED,
    CancellationAcknowledgement: JobEventKind.CANCELLATION_ACKNOWLEDGED,
    JobOutcome: JobEventKind.OUTCOME,
}


@dataclass(frozen=True, repr=False)
class JobCorrelation:
    project_id: str
    operation_id: str | None = None

    def __post_init__(self) -> None:
        validate_job_identity(self.project_id)
        if self.operation_id is not None:
            validate_job_identity(self.operation_id)

    def __repr__(self) -> str:
        return f"JobCorrelation(project_id={self.project_id!r})"


@dataclass(frozen=True, repr=False)
class JobEvent:
    event_id: str
    job_id: str
    sequence: int
    occurred_at: datetime
    correlation: JobCorrelation
    payload: JobProgress | JobWarning | CancellationRequest | CancellationAcknowledgement | JobOutcome
    schema_version: int = JOB_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != JOB_EVENT_SCHEMA_VERSION or type(self.schema_version) is not int:
            raise JobEventError("unsupported_event_schema")
        validate_job_identity(self.event_id)
        validate_job_identity(self.job_id)
        if type(self.sequence) is not int or not 1 <= self.sequence < MAX_METRIC:
            raise JobEventError("invalid_sequence")
        validate_utc(self.occurred_at)
        if type(self.correlation) is not JobCorrelation or type(self.payload) not in _PAYLOAD_KINDS:
            raise JobEventError("invalid_event")

    def __repr__(self) -> str:
        return f"JobEvent(job_id={self.job_id!r}, sequence={self.sequence}, kind={self.kind.value!r})"

    @property
    def kind(self) -> JobEventKind:
        return _PAYLOAD_KINDS[type(self.payload)]


def _encode_failure(failure: JobFailure | None) -> dict | None:
    if failure is None:
        return None
    return {"code": failure.code, "diagnostics": dict(failure.diagnostics),
            "family": failure.family.value, "retryable": failure.retryable}


def _encode_payload(payload: object) -> dict:
    if type(payload) is JobProgress:
        return {"completed": payload.completed, "diagnostics": dict(payload.diagnostics),
                "phase": payload.phase, "total": payload.total, "unit": payload.unit}
    if type(payload) is JobWarning:
        return {"code": payload.code, "diagnostics": dict(payload.diagnostics)}
    if type(payload) is CancellationRequest:
        return {"reason_code": payload.reason_code,
                "requested_at": payload.requested_at.isoformat(timespec="microseconds")}
    if type(payload) is CancellationAcknowledgement:
        return {"reason_code": payload.reason_code, "safe_point": payload.safe_point}
    # Pairs, not an object: the summary's counter order must survive the round trip.
    return {"counters": [list(entry) for entry in payload.summary.counters],
            "failure": _encode_failure(payload.failure),
            "reason_code": payload.summary.reason_code, "state": payload.state.value}


def encode_event(event: JobEvent) -> str:
    """One canonical text per event; duplicate detection relies on it."""
    text = _canonical({
        "correlation": {"operation_id": event.correlation.operation_id, "project_id": event.correlation.project_id},
        "event_id": event.event_id, "job_id": event.job_id, "kind": event.kind.value,
        "occurred_at": event.occurred_at.isoformat(timespec="microseconds"),
        "payload": _encode_payload(event.payload),
        "schema_version": event.schema_version, "sequence": event.sequence,
    })
    if len(text) > MAX_EVENT_BYTES:
        raise JobEventError("event_too_large")
    return text


def _fields(data: object, *names: str) -> tuple:
    if type(data) is not dict or set(data) != set(names):
        raise ValueError
    return tuple(data[name] for name in names)


def _decode_diagnostics(data: object) -> tuple:
    if type(data) is not dict:
        raise ValueError
    return tuple(sorted(data.items()))


def _decode_failure(data: object) -> JobFailure | None:
    if data is None:
        return None
    code, diagnostics, family, retryable = _fields(data, "code", "diagnostics", "family", "retryable")
    return JobFailure(JobFailureFamily(family), code, retryable, _decode_diagnostics(diagnostics))


def _decode_payload(kind: JobEventKind, data: object) -> object:
    if kind is JobEventKind.PROGRESS:
        completed, diagnostics, phase, total, unit = _fields(data, "completed", "diagnostics", "phase", "total", "unit")
        return JobProgress(phase, unit, completed, total, _decode_diagnostics(diagnostics))
    if kind is JobEventKind.WARNING:
        code, diagnostics = _fields(data, "code", "diagnostics")
        return JobWarning(code, _decode_diagnostics(diagnostics))
    if kind is JobEventKind.CANCELLATION_REQUESTED:
        reason_code, requested_at = _fields(data, "reason_code", "requested_at")
        return CancellationRequest(reason_code, datetime.fromisoformat(requested_at))
    if kind is JobEventKind.CANCELLATION_ACKNOWLEDGED:
        return CancellationAcknowledgement(*_fields(data, "reason_code", "safe_point"))
    counters, failure, reason_code, state = _fields(data, "counters", "failure", "reason_code", "state")
    if type(counters) is not list:
        raise ValueError
    summary = JobTerminalSummary(reason_code, tuple(tuple(entry) for entry in counters))
    return JobOutcome(JobState(state), summary, _decode_failure(failure))


def decode_event(text: str) -> JobEvent:
    """Strict inverse of ``encode_event``: anything but its exact output is rejected."""
    if not isinstance(text, str) or len(text) > MAX_EVENT_BYTES:
        raise JobEventError("invalid_event")
    try:
        data = json.loads(text)
        if type(data) is dict and "schema_version" in data and data["schema_version"] != JOB_EVENT_SCHEMA_VERSION:
            raise JobEventError("unsupported_event_schema")
        correlation, event_id, job_id, kind, occurred_at, payload, schema_version, sequence = _fields(
            data, "correlation", "event_id", "job_id", "kind", "occurred_at", "payload", "schema_version", "sequence")
        project_id, operation_id = _fields(correlation, "project_id", "operation_id")
        event = JobEvent(event_id, job_id, sequence, datetime.fromisoformat(occurred_at),
                         JobCorrelation(project_id, operation_id), _decode_payload(JobEventKind(kind), payload),
                         schema_version)
        if encode_event(event) != text:
            raise ValueError
        return event
    except JobEventError as error:
        if str(error) == "unsupported_event_schema":
            raise
    except (ValueError, TypeError, RecursionError):
        pass
    raise JobEventError("invalid_event") from None


class JobEventSequence:
    """The ordering rules of one job's stream. Not thread-safe; a rejected event changes nothing."""

    def __init__(self, job_id: str) -> None:
        validate_job_identity(job_id)
        self.job_id = job_id
        self.last_sequence = 0
        self.outcome: JobOutcome | None = None
        self._encoded: dict[str, str] = {}
        self._project_id: str | None = None
        self._phases: set[str] = set()
        self._progress: JobProgress | None = None
        self._cancellation_requested = False
        self._cancellation_acknowledged = False

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._encoded

    def accept(self, event: JobEvent) -> JobEventAcceptance:
        if type(event) is not JobEvent:
            raise JobEventError("invalid_event")
        if event.job_id != self.job_id:
            raise JobEventOrderError("foreign_job")
        encoded = encode_event(event)
        known = self._encoded.get(event.event_id)
        if known is not None:
            if known != encoded:
                raise JobEventOrderError("conflicting_duplicate")
            return JobEventAcceptance.DUPLICATE
        if self.outcome is not None:
            raise JobEventOrderError("event_after_terminal")
        if event.sequence <= self.last_sequence:
            raise JobEventOrderError("sequence_regression")
        if event.sequence != self.last_sequence + 1:
            raise JobEventOrderError("sequence_gap")
        if self._project_id is not None and event.correlation.project_id != self._project_id:
            raise JobEventOrderError("correlation_changed")
        self._check_payload(event.payload)
        self._apply_payload(event.payload)
        self._encoded[event.event_id] = encoded
        self._project_id = event.correlation.project_id
        self.last_sequence = event.sequence
        return JobEventAcceptance.APPLIED

    def _check_payload(self, payload: object) -> None:
        if type(payload) is JobProgress:
            self._check_progress(payload)
        elif type(payload) is CancellationRequest:
            if self._cancellation_requested:
                raise JobEventOrderError("duplicate_cancellation")
        elif type(payload) is CancellationAcknowledgement:
            if not self._cancellation_requested:
                raise JobEventOrderError("cancellation_not_requested")
            if self._cancellation_acknowledged:
                raise JobEventOrderError("duplicate_cancellation")
        elif type(payload) is JobOutcome:
            if payload.state is JobState.CANCELLED and not self._cancellation_requested:
                raise JobEventOrderError("cancellation_not_requested")
            if self._cancellation_acknowledged and payload.state is not JobState.CANCELLED:
                raise JobEventOrderError("outcome_contradicts_cancellation")

    def _check_progress(self, progress: JobProgress) -> None:
        current = self._progress
        if current is None or current.phase != progress.phase:
            if progress.phase in self._phases:
                raise JobEventOrderError("phase_regression")
            return
        if current.unit != progress.unit:
            raise JobEventOrderError("progress_unit_changed")
        if progress.completed < current.completed or (current.total is not None and progress.total is None):
            raise JobEventOrderError("progress_regression")

    def _apply_payload(self, payload: object) -> None:
        if type(payload) is JobProgress:
            self._phases.add(payload.phase)
            self._progress = payload
        elif type(payload) is CancellationRequest:
            self._cancellation_requested = True
        elif type(payload) is CancellationAcknowledgement:
            self._cancellation_acknowledged = True
        elif type(payload) is JobOutcome:
            self.outcome = payload
