"""Sanitized failures, warnings and their bounded diagnostics, shared by every module."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum

from .errors import JobError, JobEventError
from .ids import is_slug

MAX_DIAGNOSTICS = 16
MAX_DIAGNOSTICS_BYTES = 1024
MAX_METRIC = 2**63
DIAGNOSTIC_KEYS = frozenset({
    "attempt", "bytes_processed", "documents_done", "documents_total", "elapsed_ms",
    "partial_output_retained", "retry_after_ms", "units_done", "units_skipped", "units_total",
})


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


def valid_metric(value: object) -> bool:
    if type(value) is int:
        return 0 <= value < MAX_METRIC
    # Negative zero would equal 0.0 yet serialize differently, breaking duplicate detection.
    return (type(value) is float and math.isfinite(value) and 0 <= value < MAX_METRIC
            and math.copysign(1.0, value) > 0)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def job_diagnostics(**values: int | float | bool) -> tuple[tuple[str, int | float | bool], ...]:
    return tuple(sorted(values.items()))


def validate_diagnostics(items: tuple) -> None:
    if type(items) is not tuple or len(items) > MAX_DIAGNOSTICS:
        raise JobEventError("invalid_diagnostics")
    previous = ""
    for entry in items:
        if type(entry) is not tuple or len(entry) != 2:
            raise JobEventError("invalid_diagnostics")
        key, value = entry
        if not isinstance(key, str) or key not in DIAGNOSTIC_KEYS:
            raise JobEventError("unknown_diagnostic_key")
        if key <= previous or not (type(value) is bool or valid_metric(value)):
            raise JobEventError("invalid_diagnostics")
        previous = key
    if len(_canonical(dict(items))) > MAX_DIAGNOSTICS_BYTES:
        raise JobEventError("invalid_diagnostics")


@dataclass(frozen=True, repr=False)
class JobWarning:
    code: str
    diagnostics: tuple = ()

    def __post_init__(self) -> None:
        if not is_slug(self.code):
            raise JobEventError("invalid_code")
        validate_diagnostics(self.diagnostics)

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
        validate_diagnostics(self.diagnostics)

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
