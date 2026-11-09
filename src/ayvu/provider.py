"""Versioned, interface-neutral requests and results of a translation provider.

A request carries the text a provider needs to do its work, so it is sensitive:
default representations and diagnostics are allowlists that never include the text,
context, references or provider payloads. A result cannot be a success without
text, and it says explicitly what is not known.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Protocol

from .content import Constraint, ContentError, ProtectedPart, validate_protected_parts
from .job import JobError, is_slug, validate_job_identity, validate_utc
from .job_events import JobFailure, JobFailureFamily, JobWarning
from .provenance import ProducerIdentity

PROVIDER_SCHEMA_VERSION = 1
MAX_TEXT_CHARS = 1024 * 1024
MAX_CONTEXT_ITEMS = 16
MAX_CONTEXT_CHARS = 256 * 1024
MAX_WARNINGS = 32
_LOCALE = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*")
_OPAQUE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}")
_DOCUMENT_ID = re.compile(r"doc:v[0-9]+:[0-9a-f]{64}")
_REVISION_ID = re.compile(r"rev:v[0-9]+:[0-9a-f]{64}")
_UNIT_ID = re.compile(r"unit:v[0-9]+:[0-9a-f]{64}")


class ProviderError(ValueError):
    """A bounded error: messages never include caller data."""


class ProviderValidationError(ProviderError):
    pass


class Requirement(StrEnum):
    REQUIRED = "required"
    ADVISORY = "advisory"


class ProviderStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryState(StrEnum):
    """What a retry can assume. A timeout is ``unknown``: the server may have processed the request."""

    NOT_SENT = "not_sent"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class CancellationObserver(Protocol):
    """The part of the shared cancellation token a provider uses."""

    def checkpoint(self, safe_point: str) -> object | None: ...


def _valid_text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_identity(value: object) -> bool:
    try:
        validate_job_identity(value)
    except JobError:
        return False
    return True


def _valid_locale(value: object) -> bool:
    return isinstance(value, str) and _LOCALE.fullmatch(value) is not None


def valid_reference(value: object) -> bool:
    return value is None or (isinstance(value, str) and _REFERENCE.fullmatch(value) is not None)


@dataclass(frozen=True)
class ProviderRoute:
    source: str
    target: str
    intermediate: str | None = None

    def __post_init__(self) -> None:
        if (not _valid_locale(self.source) or not _valid_locale(self.target)
                or (self.intermediate is not None and not _valid_locale(self.intermediate))):
            raise ProviderValidationError("invalid_route")


@dataclass(frozen=True, repr=False)
class UnitReference:
    """Identity from the content model. It is transported as is and never parsed."""

    document_id: str
    revision_id: str
    unit_id: str

    def __post_init__(self) -> None:
        for pattern, value in ((_DOCUMENT_ID, self.document_id), (_REVISION_ID, self.revision_id),
                               (_UNIT_ID, self.unit_id)):
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise ProviderValidationError("invalid_unit_reference")

    def __repr__(self) -> str:
        return "UnitReference(<redacted>)"


def _validate_requirement(value: object) -> None:
    if type(value) is not Requirement:
        raise ProviderValidationError("invalid_requirement")


@dataclass(frozen=True, repr=False)
class ProtectedPartsFeature:
    name: ClassVar[str] = "protected_parts"
    parts: tuple[ProtectedPart, ...]
    requirement: Requirement

    def __post_init__(self) -> None:
        _validate_requirement(self.requirement)
        if type(self.parts) is not tuple or not self.parts:
            raise ProviderValidationError("invalid_protected_parts")

    def __repr__(self) -> str:
        return f"ProtectedPartsFeature(requirement={self.requirement.value!r})"


@dataclass(frozen=True, repr=False)
class ContextItem:
    role: str
    text: str

    def __post_init__(self) -> None:
        if not is_slug(self.role) or not _valid_text(self.text, MAX_CONTEXT_CHARS):
            raise ProviderValidationError("invalid_context")

    def __repr__(self) -> str:
        return f"ContextItem(role={self.role!r})"


@dataclass(frozen=True, repr=False)
class ContextFeature:
    name: ClassVar[str] = "context"
    items: tuple[ContextItem, ...]
    requirement: Requirement

    def __post_init__(self) -> None:
        _validate_requirement(self.requirement)
        if (type(self.items) is not tuple or not 1 <= len(self.items) <= MAX_CONTEXT_ITEMS
                or any(type(item) is not ContextItem for item in self.items)
                or sum(len(item.text) for item in self.items) > MAX_CONTEXT_CHARS):
            raise ProviderValidationError("invalid_context")

    def __repr__(self) -> str:
        return f"ContextFeature(requirement={self.requirement.value!r})"


@dataclass(frozen=True, repr=False)
class ConstraintsFeature:
    name: ClassVar[str] = "constraints"
    constraints: tuple[Constraint, ...]
    requirement: Requirement

    def __post_init__(self) -> None:
        _validate_requirement(self.requirement)
        if (type(self.constraints) is not tuple or not self.constraints
                or any(type(item) is not Constraint for item in self.constraints)):
            raise ProviderValidationError("invalid_constraints")

    def __repr__(self) -> str:
        return f"ConstraintsFeature(requirement={self.requirement.value!r})"


@dataclass(frozen=True, repr=False)
class DeadlineFeature:
    name: ClassVar[str] = "deadline"
    at: datetime
    requirement: Requirement

    def __post_init__(self) -> None:
        _validate_requirement(self.requirement)
        try:
            validate_utc(self.at)
        except JobError:
            raise ProviderValidationError("invalid_deadline") from None

    def __repr__(self) -> str:
        return f"DeadlineFeature(requirement={self.requirement.value!r})"


_FEATURE_TYPES = (ProtectedPartsFeature, ContextFeature, ConstraintsFeature, DeadlineFeature)


@dataclass(frozen=True, repr=False)
class TranslationProviderRequest:
    request_id: str
    text: str
    source_locale: str
    target_locale: str
    idempotency_key: str | None = None
    unit: UnitReference | None = None
    route: ProviderRoute | None = None
    features: tuple = ()
    policy_ref: str | None = None
    configuration_ref: str | None = None
    cancellation: CancellationObserver | None = field(default=None, compare=False)
    schema_version: int = PROVIDER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PROVIDER_SCHEMA_VERSION:
            raise ProviderValidationError("unsupported_provider_schema")
        if not _valid_identity(self.request_id):
            raise ProviderValidationError("invalid_request_id")
        if not _valid_text(self.text, MAX_TEXT_CHARS):
            raise ProviderValidationError("invalid_text")
        if not _valid_locale(self.source_locale) or not _valid_locale(self.target_locale):
            raise ProviderValidationError("invalid_locale")
        if self.idempotency_key is not None and (not isinstance(self.idempotency_key, str)
                                                 or not _OPAQUE_KEY.fullmatch(self.idempotency_key)):
            raise ProviderValidationError("invalid_idempotency_key")
        if self.unit is not None and type(self.unit) is not UnitReference:
            raise ProviderValidationError("invalid_unit_reference")
        if self.route is not None and (type(self.route) is not ProviderRoute or (
                self.route.source, self.route.target) != (self.source_locale, self.target_locale)):
            raise ProviderValidationError("invalid_route")
        if not valid_reference(self.policy_ref) or not valid_reference(self.configuration_ref):
            raise ProviderValidationError("invalid_reference")
        if self.cancellation is not None and not callable(getattr(self.cancellation, "checkpoint", None)):
            raise ProviderValidationError("invalid_cancellation")
        self._normalize_features()

    def _normalize_features(self) -> None:
        features = self.features
        if type(features) is not tuple or any(type(item) not in _FEATURE_TYPES for item in features):
            raise ProviderValidationError("invalid_features")
        if len({item.name for item in features}) != len(features):
            raise ProviderValidationError("duplicate_feature")
        for item in features:
            if type(item) is ProtectedPartsFeature:
                try:
                    validate_protected_parts(item.parts, len(self.text))
                except ContentError:
                    raise ProviderValidationError("invalid_protected_parts") from None
        object.__setattr__(self, "features", tuple(sorted(features, key=lambda item: item.name)))

    def __repr__(self) -> str:
        return f"TranslationProviderRequest(request_id={self.request_id!r})"

    def feature(self, kind: type) -> object | None:
        return next((item for item in self.features if type(item) is kind), None)


@dataclass(frozen=True)
class ProviderUsage:
    """``None`` states that a value is not known; it never means zero."""

    characters_submitted: int | None = None
    requests: int | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        for value in (self.characters_submitted, self.requests, self.latency_ms):
            if value is not None and (type(value) is not int or not 0 <= value < 2**63):
                raise ProviderValidationError("invalid_usage")


@dataclass(frozen=True, repr=False)
class TranslationProviderResult:
    request_id: str
    status: ProviderStatus
    producer: ProducerIdentity
    delivery: DeliveryState
    text: str | None = None
    failure: JobFailure | None = None
    route: ProviderRoute | None = None
    warnings: tuple[JobWarning, ...] = ()
    usage: ProviderUsage = ProviderUsage()
    schema_version: int = PROVIDER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PROVIDER_SCHEMA_VERSION:
            raise ProviderValidationError("unsupported_provider_schema")
        if not _valid_identity(self.request_id):
            raise ProviderValidationError("invalid_request_id")
        if (type(self.status) is not ProviderStatus or type(self.producer) is not ProducerIdentity
                or type(self.delivery) is not DeliveryState or type(self.usage) is not ProviderUsage):
            raise ProviderValidationError("invalid_result")
        if self.route is not None and type(self.route) is not ProviderRoute:
            raise ProviderValidationError("invalid_route")
        if (type(self.warnings) is not tuple or len(self.warnings) > MAX_WARNINGS
                or any(type(item) is not JobWarning for item in self.warnings)):
            raise ProviderValidationError("invalid_result")
        self._validate_outcome()

    def _validate_outcome(self) -> None:
        if self.status is ProviderStatus.SUCCEEDED:
            if (not _valid_text(self.text, MAX_TEXT_CHARS * 4) or self.failure is not None
                    or self.delivery is not DeliveryState.COMPLETED):
                raise ProviderValidationError("invalid_success")
            return
        if self.text is not None or type(self.failure) is not JobFailure:
            raise ProviderValidationError("invalid_failure_result")
        cancelled = self.failure.family is JobFailureFamily.CANCELLED
        if cancelled != (self.status is ProviderStatus.CANCELLED):
            raise ProviderValidationError("invalid_failure_result")
        if cancelled and self.delivery is DeliveryState.COMPLETED:
            raise ProviderValidationError("invalid_failure_result")

    def __repr__(self) -> str:
        return f"TranslationProviderResult(request_id={self.request_id!r}, status={self.status.value!r})"


def request_diagnostics(request: TranslationProviderRequest) -> dict:
    """An allowlist. Text, context, identifiers of content and references are absent on purpose."""
    return {
        "schema_version": request.schema_version,
        "request_id": request.request_id,
        "source_locale": request.source_locale,
        "target_locale": request.target_locale,
        "text_chars": len(request.text),
        "features": [[item.name, item.requirement.value] for item in request.features],
    }


def result_diagnostics(result: TranslationProviderResult) -> dict:
    """An allowlist. The translated text is absent on purpose."""
    failure = result.failure
    route = result.route
    return {
        "schema_version": result.schema_version,
        "request_id": result.request_id,
        "status": result.status.value,
        "delivery": result.delivery.value,
        "failure": None if failure is None else {"family": failure.family.value, "code": failure.code,
                                                 "retryable": failure.retryable},
        "warnings": [item.code for item in result.warnings],
        "text_chars": None if result.text is None else len(result.text),
        "route": None if route is None else [route.source, route.intermediate, route.target],
        "producer": [result.producer.provider, result.producer.model, result.producer.adapter,
                     result.producer.pipeline],
        "usage": [result.usage.characters_submitted, result.usage.requests, result.usage.latency_ms],
    }
