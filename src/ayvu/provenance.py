"""Framework-neutral, append-only lineage records.

Pure values: callers supply identifiers and time. Every field is a bounded code,
identifier or number, so there is no field for document text, prompts, paths or
credentials. Actors are kinds and never people: a durable history tying a person
to what they read and edited would itself be sensitive.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .job import JobError, is_slug, validate_job_identity, validate_utc

PROVENANCE_SCHEMA_VERSION = 1
MAX_PARENTS = 32
MAX_ARTIFACTS = 16
MAX_CONFIGURATION_REFS = 16
MAX_RECORD_BYTES = 16 * 1024
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PRODUCER = re.compile(r"[a-z0-9][a-z0-9._+@-]{0,63}")
_CONFIGURATION_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}")


class ProvenanceError(ValueError):
    """A bounded error: messages never include caller data."""


class ProvenanceValidationError(ProvenanceError):
    pass


class ProvenanceNotFoundError(ProvenanceError):
    pass


class ProvenanceConflictError(ProvenanceError):
    pass


class ProvenanceReferenceError(ProvenanceError):
    pass


class ProvenanceLimitError(ProvenanceError):
    pass


class ProvenanceStoreError(ProvenanceError):
    pass


class ActorKind(StrEnum):
    SYSTEM = "system"
    HUMAN = "human"
    PROVIDER = "provider"
    AI = "ai"


class SourceKind(StrEnum):
    PIPELINE = "pipeline"
    PROVIDER = "provider"
    CACHE = "cache"
    TRANSLATION_MEMORY = "translation_memory"
    HUMAN = "human"
    AI = "ai"
    VALIDATION = "validation"


def validate_identity(value: str) -> None:
    try:
        validate_job_identity(value)
    except JobError:
        raise ProvenanceValidationError("invalid_identity") from None


def _valid_opaque(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE.fullmatch(value) is not None


@dataclass(frozen=True, repr=False)
class SubjectRef:
    """Supplied by the caller and compared as is; never generated or parsed here."""

    namespace: str
    version: int
    value: str

    def __post_init__(self) -> None:
        if not is_slug(self.namespace) or type(self.version) is not int or not 1 <= self.version < 2**31:
            raise ProvenanceValidationError("invalid_subject")
        if not _valid_opaque(self.value):
            raise ProvenanceValidationError("invalid_subject")

    def __repr__(self) -> str:
        return f"SubjectRef(namespace={self.namespace!r}, version={self.version})"


@dataclass(frozen=True, repr=False)
class ProducerIdentity:
    """All four are required: not knowing a value must be stated, never implied by omission."""

    provider: str
    model: str
    adapter: str
    pipeline: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.model, self.adapter, self.pipeline):
            if not isinstance(value, str) or not _PRODUCER.fullmatch(value):
                raise ProvenanceValidationError("invalid_producer")

    def __repr__(self) -> str:
        return (f"ProducerIdentity(provider={self.provider!r}, model={self.model!r}, "
                f"adapter={self.adapter!r}, pipeline={self.pipeline!r})")

    @property
    def fully_identified(self) -> bool:
        """What is known about the producer. It does not promise reproducing the result."""
        return UNKNOWN not in (self.provider, self.model, self.adapter, self.pipeline)


@dataclass(frozen=True, repr=False)
class ConfigurationRef:
    """A reference to an immutable revision, not a digest of a secret, prompt or text."""

    kind: str
    reference: str

    def __post_init__(self) -> None:
        if (not is_slug(self.kind) or not isinstance(self.reference, str)
                or not _CONFIGURATION_REFERENCE.fullmatch(self.reference)):
            raise ProvenanceValidationError("invalid_configuration_ref")

    def __repr__(self) -> str:
        return f"ConfigurationRef(kind={self.kind!r})"


def _sorted_identities(value: tuple, limit: int, code: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > limit or len(set(value)) != len(value):
        raise ProvenanceValidationError(code)
    for item in value:
        validate_identity(item)
    return tuple(sorted(value))


@dataclass(frozen=True, repr=False)
class ProvenanceRequest:
    """One lineage fact. Reference tuples are stored sorted, so their order carries no meaning."""

    project_id: str
    job_id: str
    operation: str
    actor_kind: ActorKind
    source_kind: SourceKind
    producer: ProducerIdentity
    subject: SubjectRef | None = None
    outcome_code: str | None = None
    configuration_refs: tuple[ConfigurationRef, ...] = ()
    parents: tuple[str, ...] = ()
    supersedes: str | None = None
    artifacts: tuple[str, ...] = ()
    job_sequence: int | None = None
    correlation_id: str | None = None
    operation_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        validate_identity(self.project_id)
        validate_identity(self.job_id)
        if not is_slug(self.operation):
            raise ProvenanceValidationError("invalid_operation")
        if type(self.actor_kind) is not ActorKind or type(self.source_kind) is not SourceKind:
            raise ProvenanceValidationError("invalid_kind")
        if type(self.producer) is not ProducerIdentity:
            raise ProvenanceValidationError("invalid_producer")
        if self.subject is not None and type(self.subject) is not SubjectRef:
            raise ProvenanceValidationError("invalid_subject")
        if self.outcome_code is not None and not is_slug(self.outcome_code):
            raise ProvenanceValidationError("invalid_outcome_code")
        references = self.configuration_refs
        if (type(references) is not tuple or len(references) > MAX_CONFIGURATION_REFS
                or any(type(item) is not ConfigurationRef for item in references)
                or len({item.kind for item in references}) != len(references)):
            raise ProvenanceValidationError("invalid_configuration_ref")
        object.__setattr__(self, "configuration_refs", tuple(sorted(references, key=lambda item: item.kind)))
        object.__setattr__(self, "parents", _sorted_identities(self.parents, MAX_PARENTS, "invalid_parents"))
        object.__setattr__(self, "artifacts", _sorted_identities(self.artifacts, MAX_ARTIFACTS, "invalid_artifacts"))
        for value in (self.supersedes, self.correlation_id, self.operation_id):
            if value is not None:
                validate_identity(value)
        if self.job_sequence is not None and (type(self.job_sequence) is not int
                                              or not 1 <= self.job_sequence < 2**63):
            raise ProvenanceValidationError("invalid_job_sequence")
        if self.idempotency_key is not None and not _valid_opaque(self.idempotency_key):
            raise ProvenanceValidationError("invalid_idempotency_key")

    def __repr__(self) -> str:
        return f"ProvenanceRequest(operation={self.operation!r}, source_kind={self.source_kind.value!r})"


@dataclass(frozen=True, repr=False)
class ProvenanceRecord:
    record_id: str
    recorded_at: datetime
    request: ProvenanceRequest
    schema_version: int = PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PROVENANCE_SCHEMA_VERSION:
            raise ProvenanceValidationError("unsupported_provenance_version")
        validate_identity(self.record_id)
        if type(self.request) is not ProvenanceRequest:
            raise ProvenanceValidationError("invalid_request")
        try:
            validate_utc(self.recorded_at)
        except JobError:
            raise ProvenanceValidationError("invalid_timestamp") from None

    def __repr__(self) -> str:
        return f"ProvenanceRecord(record_id={self.record_id!r}, operation={self.request.operation!r})"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _request_fields(request: ProvenanceRequest) -> dict:
    subject = request.subject
    producer = request.producer
    return {
        "actor_kind": request.actor_kind.value,
        "artifacts": list(request.artifacts),
        "configuration_refs": [[item.kind, item.reference] for item in request.configuration_refs],
        "correlation_id": request.correlation_id,
        "idempotency_key": request.idempotency_key,
        "job_id": request.job_id,
        "job_sequence": request.job_sequence,
        "operation": request.operation,
        "operation_id": request.operation_id,
        "outcome_code": request.outcome_code,
        "parents": list(request.parents),
        "producer": {"adapter": producer.adapter, "model": producer.model, "pipeline": producer.pipeline,
                     "provider": producer.provider},
        "project_id": request.project_id,
        "source_kind": request.source_kind.value,
        "subject": None if subject is None else {"namespace": subject.namespace, "value": subject.value,
                                                 "version": subject.version},
        "supersedes": request.supersedes,
    }


def request_identity(request: ProvenanceRequest) -> str:
    """Canonical payload identity: an idempotent retry has a new ID and time, not new content."""
    return _canonical(_request_fields(request))


def encode_record(record: ProvenanceRecord) -> str:
    text = _canonical({"record_id": record.record_id, "recorded_at": record.recorded_at.isoformat(timespec="microseconds"),
                       "request": _request_fields(record.request), "schema_version": record.schema_version})
    if len(text) > MAX_RECORD_BYTES:
        raise ProvenanceValidationError("record_too_large")
    return text


def _fields(data: object, *names: str) -> tuple:
    if type(data) is not dict or set(data) != set(names):
        raise ValueError
    return tuple(data[name] for name in names)


def _strings(data: object) -> tuple:
    if type(data) is not list:
        raise ValueError
    return tuple(data)


def _decode_request(data: object) -> ProvenanceRequest:
    (actor_kind, artifacts, configuration_refs, correlation_id, idempotency_key, job_id, job_sequence, operation,
     operation_id, outcome_code, parents, producer, project_id, source_kind, subject, supersedes) = _fields(
        data, "actor_kind", "artifacts", "configuration_refs", "correlation_id", "idempotency_key", "job_id",
        "job_sequence", "operation", "operation_id", "outcome_code", "parents", "producer", "project_id",
        "source_kind", "subject", "supersedes")
    adapter, model, pipeline, provider = _fields(producer, "adapter", "model", "pipeline", "provider")
    decoded_subject = None
    if subject is not None:
        namespace, value, version = _fields(subject, "namespace", "value", "version")
        decoded_subject = SubjectRef(namespace, version, value)
    return ProvenanceRequest(
        project_id=project_id, job_id=job_id, operation=operation, actor_kind=ActorKind(actor_kind),
        source_kind=SourceKind(source_kind), producer=ProducerIdentity(provider, model, adapter, pipeline),
        subject=decoded_subject, outcome_code=outcome_code,
        configuration_refs=tuple(ConfigurationRef(*entry) for entry in _strings(configuration_refs)),
        parents=_strings(parents), supersedes=supersedes, artifacts=_strings(artifacts), job_sequence=job_sequence,
        correlation_id=correlation_id, operation_id=operation_id, idempotency_key=idempotency_key)


def decode_record(text: str) -> ProvenanceRecord:
    """Strict inverse of ``encode_record``: anything but its exact output is rejected."""
    if not isinstance(text, str) or len(text) > MAX_RECORD_BYTES:
        raise ProvenanceValidationError("invalid_record")
    try:
        data = json.loads(text)
        if type(data) is dict and data.get("schema_version", PROVENANCE_SCHEMA_VERSION) != PROVENANCE_SCHEMA_VERSION:
            raise ProvenanceValidationError("unsupported_provenance_version")
        record_id, recorded_at, request, schema_version = _fields(
            data, "record_id", "recorded_at", "request", "schema_version")
        record = ProvenanceRecord(record_id, datetime.fromisoformat(recorded_at), _decode_request(request), schema_version)
        if encode_record(record) != text:
            raise ValueError
        return record
    except ProvenanceValidationError as error:
        if str(error) == "unsupported_provenance_version":
            raise
    except (ValueError, TypeError, RecursionError):
        pass
    raise ProvenanceValidationError("invalid_record") from None
