"""Framework-neutral identity and lifecycle of artifacts owned by a job.

Pure values: callers supply time and identifiers. Error messages are bounded
codes and never include caller data. Locations derive from the artifact ID, so
no title, file name or other document data can reach the filesystem layout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .job import JobError, is_slug, validate_job_identity, validate_utc

ARTIFACT_SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 4 * 1024**3
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MEDIA_TYPE = re.compile(r"[a-z]+/[a-z0-9][a-z0-9.+-]{0,100}")


class ArtifactError(ValueError):
    """A bounded error: messages never include caller data."""


class ArtifactValidationError(ArtifactError):
    pass


class ArtifactNotFoundError(ArtifactError):
    pass


class ArtifactConflictError(ArtifactError):
    pass


class ArtifactOwnershipError(ArtifactError):
    pass


class ArtifactPathError(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


class ArtifactStoreError(ArtifactError):
    pass


class ArtifactCancelledError(ArtifactError):
    pass


class ArtifactState(StrEnum):
    STAGING = "staging"
    READY = "ready"
    ABANDONED = "abandoned"


def validate_identity(value: str) -> None:
    try:
        validate_job_identity(value)
    except JobError:
        raise ArtifactValidationError("invalid_identity") from None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_size(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_ARTIFACT_BYTES


def staging_location(artifact_id: str) -> str:
    validate_identity(artifact_id)
    return f"artifacts/staging/{artifact_id}.part"


def ready_location(artifact_id: str) -> str:
    validate_identity(artifact_id)
    return f"artifacts/ready/{artifact_id}.bin"


@dataclass(frozen=True, repr=False)
class ArtifactRequest:
    """What must be identical for a stored artifact to stand in for new work."""

    project_id: str
    job_id: str
    role: str
    stage: str
    stage_version: int
    input_fingerprint: str
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        validate_identity(self.project_id)
        validate_identity(self.job_id)
        if not is_slug(self.role) or not is_slug(self.stage):
            raise ArtifactValidationError("invalid_label")
        if type(self.stage_version) is not int or not 1 <= self.stage_version < 2**31:
            raise ArtifactValidationError("invalid_stage_version")
        if not _valid_digest(self.input_fingerprint):
            raise ArtifactValidationError("invalid_fingerprint")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ArtifactValidationError("invalid_media_type")

    def __repr__(self) -> str:
        return f"ArtifactRequest(role={self.role!r}, stage={self.stage!r}, stage_version={self.stage_version})"


@dataclass(frozen=True, repr=False)
class Artifact:
    artifact_id: str
    request: ArtifactRequest
    state: ArtifactState
    revision: int
    created_at: datetime
    updated_at: datetime
    size: int | None = None
    digest: str | None = None
    reason_code: str | None = None
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactValidationError("unsupported_artifact_version")
        validate_identity(self.artifact_id)
        if type(self.request) is not ArtifactRequest or type(self.state) is not ArtifactState:
            raise ArtifactValidationError("invalid_artifact")
        if type(self.revision) is not int or not 1 <= self.revision < 2**63:
            raise ArtifactValidationError("invalid_revision")
        try:
            validate_utc(self.created_at)
            validate_utc(self.updated_at)
        except JobError:
            raise ArtifactValidationError("invalid_timestamp") from None
        if self.updated_at < self.created_at:
            raise ArtifactValidationError("invalid_timestamp_order")
        if (self.size is None) != (self.digest is None):
            raise ArtifactValidationError("invalid_payload_metadata")
        if self.size is not None and not (_valid_size(self.size) and _valid_digest(self.digest)):
            raise ArtifactValidationError("invalid_payload_metadata")
        if self.state is ArtifactState.READY and self.size is None:
            raise ArtifactValidationError("ready_requires_payload_metadata")
        if (self.state is ArtifactState.ABANDONED) != (self.reason_code is not None):
            raise ArtifactValidationError("invalid_reason")
        if self.reason_code is not None and not is_slug(self.reason_code):
            raise ArtifactValidationError("invalid_reason")

    def __repr__(self) -> str:
        return f"Artifact(artifact_id={self.artifact_id!r}, state={self.state.value!r}, revision={self.revision})"


def new_artifact(request: ArtifactRequest, artifact_id: str, now: datetime) -> Artifact:
    return Artifact(artifact_id=artifact_id, request=request, state=ArtifactState.STAGING, revision=1,
                    created_at=now, updated_at=now)


def _next(artifact: Artifact, now: datetime, **changes: object) -> Artifact:
    # updated_at never moves backwards if the clock does.
    return replace(artifact, revision=artifact.revision + 1, updated_at=max(now, artifact.updated_at), **changes)


def _require(artifact: Artifact, state: ArtifactState, expected_revision: int) -> None:
    if type(expected_revision) is not int or expected_revision != artifact.revision:
        raise ArtifactConflictError("stale_revision")
    if artifact.state is not state:
        raise ArtifactConflictError(f"artifact_not_{state.value}")


def record_payload(artifact: Artifact, size: int, digest: str, *, expected_revision: int, now: datetime) -> Artifact:
    """Store the computed metadata while still staged, before the file becomes visible."""
    _require(artifact, ArtifactState.STAGING, expected_revision)
    if artifact.size is not None:
        raise ArtifactConflictError("payload_already_recorded")
    return _next(artifact, now, size=size, digest=digest)


def mark_ready(artifact: Artifact, *, expected_revision: int, now: datetime) -> Artifact:
    _require(artifact, ArtifactState.STAGING, expected_revision)
    return _next(artifact, now, state=ArtifactState.READY)


def abandon(artifact: Artifact, reason_code: str, *, expected_revision: int, now: datetime) -> Artifact:
    _require(artifact, ArtifactState.STAGING, expected_revision)
    return _next(artifact, now, state=ArtifactState.ABANDONED, reason_code=reason_code)


def retire(artifact: Artifact, reason_code: str, *, expected_revision: int, now: datetime) -> Artifact:
    """Withdraw a ready artifact whose payload failed verification."""
    _require(artifact, ArtifactState.READY, expected_revision)
    return _next(artifact, now, state=ArtifactState.ABANDONED, reason_code=reason_code)


def reuse_mismatch(artifact: Artifact, request: ArtifactRequest) -> str | None:
    """The first reason this record cannot stand in for ``request``, or None."""
    if artifact.state is not ArtifactState.READY:
        return "artifact_not_ready"
    current, wanted = artifact.request, request
    if (current.project_id, current.job_id) != (wanted.project_id, wanted.job_id):
        return "ownership_mismatch"
    if current.role != wanted.role or current.media_type != wanted.media_type:
        return "role_mismatch"
    if (current.stage, current.stage_version) != (wanted.stage, wanted.stage_version):
        return "stage_mismatch"
    if current.input_fingerprint != wanted.input_fingerprint:
        return "fingerprint_mismatch"
    return None
