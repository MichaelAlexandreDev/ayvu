"""Framework-neutral review export, validation and application use cases.

The package codec, project storage, format adapters and publication protocol are
ports here. Review text is carried only in bounded opaque values whose reprs are
redacted; events and terminal values contain codes, counts and identities only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .artifact import Artifact, ArtifactState
from .job import JobState, is_slug, validate_job_identity
from .job_events import JobEvent, JobFailure
from .job_execution import CancellationToken
from .translate_project import TranslateProjectResult

REVIEW_EXPORT_COMMAND_TYPE = "export_review_package"
REVIEW_VALIDATE_COMMAND_TYPE = "validate_review_package"
REVIEW_APPLY_COMMAND_TYPE = "apply_review_package"
REVIEW_COMMAND_VERSION = 1
REVIEW_PACKAGE_SCHEMA_VERSION = 2
MAX_REVIEW_ROWS = 100_000
MAX_REVIEW_DOCUMENTS = 32_768
MAX_REVIEW_PACKAGE_BYTES = 64 * 1024 * 1024
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PACKAGE_ID = re.compile(r"rpk:v1:[0-9a-f]{64}\Z")
_UNIT_ID = re.compile(r"unit:v1:[0-9a-f]{64}\Z")

REVIEW_SAFE_POINTS = (
    "review_before_work",
    "review_before_validation",
    "review_before_reconstruction",
    "review_before_publication",
)


class ReviewApplicationError(ValueError):
    """Bounded application failure; never includes package contents or paths."""

    def __init__(self, code: str) -> None:
        self.code = code if is_slug(code) else "review_operation_failed"
        super().__init__(self.code)


class ReviewOutcome(StrEnum):
    READY = "ready"
    INVALID = "invalid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, repr=False)
class ReviewPackageIdentity:
    schema_version: int
    package_id: str | None
    package_fingerprint: str
    project_id: str | None = None
    project_revision: int | None = None
    source_sha256: str | None = None
    source_size: int | None = None
    machine_artifact_id: str | None = None
    machine_artifact_revision: int | None = None
    machine_artifact_digest: str | None = None
    document_count: int = 0
    unit_count: int = 0

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in (1, REVIEW_PACKAGE_SCHEMA_VERSION):
            raise ReviewApplicationError("unsupported_schema_version")
        if not isinstance(self.package_fingerprint, str) or not _FINGERPRINT.fullmatch(self.package_fingerprint):
            raise ReviewApplicationError("invalid_package_fingerprint")
        if self.schema_version == REVIEW_PACKAGE_SCHEMA_VERSION:
            if (not isinstance(self.package_id, str) or not _PACKAGE_ID.fullmatch(self.package_id)
                    or not isinstance(self.project_id, str) or not isinstance(self.machine_artifact_id, str)
                    or type(self.project_revision) is not int or self.project_revision < 1
                    or type(self.machine_artifact_revision) is not int or self.machine_artifact_revision < 1
                    or not isinstance(self.source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256)
                    or not isinstance(self.machine_artifact_digest, str) or not _FINGERPRINT.fullmatch(self.machine_artifact_digest)):
                raise ReviewApplicationError("invalid_package_identity")
            try:
                validate_job_identity(self.project_id)
                validate_job_identity(self.machine_artifact_id)
            except ValueError:
                raise ReviewApplicationError("invalid_package_identity") from None
            if type(self.source_size) is not int or self.source_size < 0:
                raise ReviewApplicationError("invalid_package_identity")
        if (type(self.document_count) is not int or not 0 <= self.document_count <= MAX_REVIEW_DOCUMENTS
                or type(self.unit_count) is not int or not 0 <= self.unit_count <= MAX_REVIEW_ROWS):
            raise ReviewApplicationError("invalid_package_counts")

    def __repr__(self) -> str:
        return f"ReviewPackageIdentity(schema_version={self.schema_version}, rows={self.unit_count})"


@dataclass(frozen=True, repr=False)
class ReviewPackageExport:
    identity: ReviewPackageIdentity
    data: bytes = field(repr=False)
    unit_ids: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if (type(self.identity) is not ReviewPackageIdentity or type(self.data) is not bytes
                or len(self.data) > MAX_REVIEW_PACKAGE_BYTES
                or type(self.unit_ids) is not tuple or len(self.unit_ids) > MAX_REVIEW_ROWS
                or (type(self.identity) is ReviewPackageIdentity and len(self.unit_ids) != self.identity.unit_count)
                or any(not isinstance(unit_id, str) or not _UNIT_ID.fullmatch(unit_id) for unit_id in self.unit_ids)
                or len(set(self.unit_ids)) != len(self.unit_ids)):
            raise ReviewApplicationError("invalid_export_result")

    def __repr__(self) -> str:
        return f"ReviewPackageExport(identity={self.identity!r}, bytes={len(self.data)})"


@dataclass(frozen=True, repr=False)
class ReviewCounts:
    documents: int = 0
    units: int = 0
    reviewed: int = 0
    pending: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 0 <= value <= MAX_REVIEW_ROWS
               for value in (self.documents, self.units, self.reviewed, self.pending)):
            raise ReviewApplicationError("invalid_package_counts")

    def __repr__(self) -> str:
        return f"ReviewCounts(documents={self.documents}, units={self.units}, reviewed={self.reviewed}, pending={self.pending})"


@dataclass(frozen=True, repr=False)
class ReviewValidationMaterial:
    """The structural mapper's result. ``plan`` is opaque to this layer."""

    identity: ReviewPackageIdentity
    valid: bool
    counts: ReviewCounts
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    plan: object | None = field(default=None, repr=False, compare=False)
    reviewed_unit_ids: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if (type(self.identity) is not ReviewPackageIdentity or type(self.valid) is not bool
                or type(self.counts) is not ReviewCounts or type(self.conflicts) is not tuple
                or type(self.warnings) is not tuple or type(self.reviewed_unit_ids) is not tuple
                or len(self.conflicts) > 64 or len(self.warnings) > 64
                or len(self.reviewed_unit_ids) > MAX_REVIEW_ROWS
                or any(not is_slug(item) for item in self.conflicts + self.warnings)
                or any(not isinstance(item, str) or not _UNIT_ID.fullmatch(item) for item in self.reviewed_unit_ids)
                or len(set(self.reviewed_unit_ids)) != len(self.reviewed_unit_ids)):
            raise ReviewApplicationError("invalid_validation_result")
        if self.valid != (not self.conflicts):
            raise ReviewApplicationError("invalid_validation_result")

    def __repr__(self) -> str:
        return (f"ReviewValidationMaterial(valid={self.valid}, counts={self.counts!r}, "
                f"conflicts={len(self.conflicts)}, warnings={len(self.warnings)})")


@dataclass(frozen=True, repr=False)
class ReviewExportCommand:
    translation: TranslateProjectResult
    destination: object = field(repr=False, compare=False)
    destination_fingerprint: str
    overwrite: bool = False
    schema_version: int = REVIEW_COMMAND_VERSION
    cancellation: CancellationToken | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_command_version(self.schema_version)
        if type(self.translation) is not TranslateProjectResult or type(self.overwrite) is not bool:
            raise ReviewApplicationError("invalid_command")
        _validate_fingerprint(self.destination_fingerprint)

    def __repr__(self) -> str:
        return "ReviewExportCommand(<destination redacted>)"


@dataclass(frozen=True, repr=False)
class ReviewValidationCommand:
    package: bytes = field(repr=False)
    source: object | None = field(default=None, repr=False, compare=False)
    schema_version: int = REVIEW_COMMAND_VERSION
    cancellation: CancellationToken | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_command_version(self.schema_version)
        if type(self.package) is not bytes or len(self.package) > MAX_REVIEW_PACKAGE_BYTES:
            raise ReviewApplicationError("invalid_package")

    def __repr__(self) -> str:
        return f"ReviewValidationCommand(bytes={len(self.package)})"


@dataclass(frozen=True, repr=False)
class ReviewApplyCommand:
    package: bytes = field(repr=False)
    source: object | None = field(repr=False, compare=False)
    destination: object = field(repr=False, compare=False)
    destination_fingerprint: str
    overwrite: bool = False
    validation_evidence_digest: str | None = None
    idempotency_key: str | None = None
    schema_version: int = REVIEW_COMMAND_VERSION
    cancellation: CancellationToken | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_command_version(self.schema_version)
        if (type(self.package) is not bytes or len(self.package) > MAX_REVIEW_PACKAGE_BYTES
                or type(self.overwrite) is not bool):
            raise ReviewApplicationError("invalid_command")
        _validate_fingerprint(self.destination_fingerprint)
        if self.validation_evidence_digest is not None:
            _validate_fingerprint(self.validation_evidence_digest)
        if self.idempotency_key is not None and (not isinstance(self.idempotency_key, str)
                                                 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.idempotency_key)):
            raise ReviewApplicationError("invalid_idempotency_key")

    def __repr__(self) -> str:
        return f"ReviewApplyCommand(bytes={len(self.package)}, destination=<redacted>)"


@dataclass(frozen=True, repr=False)
class ReviewOperation:
    project_id: str
    job_id: str
    input_fingerprint: str
    idempotency_key: str
    token: object = field(repr=False, compare=False)
    already_committed: bool = False
    artifact: Artifact | None = None

    def __post_init__(self) -> None:
        try:
            validate_job_identity(self.project_id)
            validate_job_identity(self.job_id)
        except ValueError:
            raise ReviewApplicationError("invalid_operation") from None
        _validate_fingerprint(self.input_fingerprint)
        if not isinstance(self.idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.idempotency_key):
            raise ReviewApplicationError("invalid_idempotency_key")
        if type(self.already_committed) is not bool or (self.already_committed and
                (type(self.artifact) is not Artifact or self.artifact.state is not ArtifactState.READY)):
            raise ReviewApplicationError("invalid_operation")

    def __repr__(self) -> str:
        return f"ReviewOperation(job_id={self.job_id!r}, committed={self.already_committed})"


@dataclass(frozen=True, repr=False)
class ReviewOperationResult:
    job_id: str
    state: JobState
    code: str
    artifact: Artifact | None
    committed: bool
    events: tuple[JobEvent, ...] = ()
    failure: JobFailure | None = None
    schema_version: int = REVIEW_COMMAND_VERSION

    def __post_init__(self) -> None:
        try:
            validate_job_identity(self.job_id)
        except ValueError:
            raise ReviewApplicationError("invalid_operation_result") from None
        if (type(self.schema_version) is not int or self.schema_version != REVIEW_COMMAND_VERSION
                or type(self.state) is not JobState or not is_slug(self.code) or type(self.committed) is not bool
                or type(self.events) is not tuple or any(type(event) is not JobEvent for event in self.events)
                or (self.artifact is not None and type(self.artifact) is not Artifact)):
            raise ReviewApplicationError("invalid_operation_result")

    def __repr__(self) -> str:
        return f"ReviewOperationResult(job_id={self.job_id!r}, state={self.state.value!r}, committed={self.committed})"


@dataclass(frozen=True, repr=False)
class ReviewExportResult:
    outcome: ReviewOutcome
    code: str
    package: ReviewPackageIdentity | None = None
    artifact: Artifact | None = None
    job_id: str | None = None
    events: tuple[JobEvent, ...] = ()
    failure: JobFailure | None = None
    schema_version: int = REVIEW_COMMAND_VERSION

    def __post_init__(self) -> None:
        if (type(self.schema_version) is not int or self.schema_version != REVIEW_COMMAND_VERSION
                or type(self.outcome) is not ReviewOutcome or not is_slug(self.code)
                or (self.package is not None and type(self.package) is not ReviewPackageIdentity)
                or (self.artifact is not None and type(self.artifact) is not Artifact)
                or (self.job_id is not None and not isinstance(self.job_id, str))
                or type(self.events) is not tuple or any(type(event) is not JobEvent for event in self.events)):
            raise ReviewApplicationError("invalid_export_result")
        if self.job_id is not None:
            try:
                validate_job_identity(self.job_id)
            except ValueError:
                raise ReviewApplicationError("invalid_export_result") from None

    def __repr__(self) -> str:
        return f"ReviewExportResult(outcome={self.outcome.value!r}, code={self.code!r}, ready={self.artifact is not None})"


@dataclass(frozen=True, repr=False)
class ReviewValidationResult:
    outcome: ReviewOutcome
    code: str
    package: ReviewPackageIdentity | None = None
    counts: ReviewCounts = ReviewCounts()
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    validation_evidence_digest: str | None = None
    failure: JobFailure | None = None
    schema_version: int = REVIEW_COMMAND_VERSION

    def __post_init__(self) -> None:
        if (type(self.schema_version) is not int or self.schema_version != REVIEW_COMMAND_VERSION
                or type(self.outcome) is not ReviewOutcome or not is_slug(self.code)
                or (self.package is not None and type(self.package) is not ReviewPackageIdentity)
                or type(self.counts) is not ReviewCounts or type(self.conflicts) is not tuple
                or type(self.warnings) is not tuple or len(self.conflicts) > 64 or len(self.warnings) > 64
                or any(not is_slug(item) for item in self.conflicts + self.warnings)):
            raise ReviewApplicationError("invalid_validation_result")
        if self.validation_evidence_digest is not None:
            _validate_fingerprint(self.validation_evidence_digest)

    @property
    def valid(self) -> bool:
        return self.outcome is ReviewOutcome.READY and not self.conflicts

    def __repr__(self) -> str:
        return f"ReviewValidationResult(outcome={self.outcome.value!r}, code={self.code!r}, conflicts={len(self.conflicts)})"


@dataclass(frozen=True, repr=False)
class ReviewApplyResult:
    outcome: ReviewOutcome
    code: str
    package: ReviewPackageIdentity | None = None
    artifact: Artifact | None = None
    job_id: str | None = None
    counts: ReviewCounts = ReviewCounts()
    events: tuple[JobEvent, ...] = ()
    failure: JobFailure | None = None
    schema_version: int = REVIEW_COMMAND_VERSION

    def __post_init__(self) -> None:
        if (type(self.schema_version) is not int or self.schema_version != REVIEW_COMMAND_VERSION
                or type(self.outcome) is not ReviewOutcome or not is_slug(self.code)
                or (self.package is not None and type(self.package) is not ReviewPackageIdentity)
                or (self.artifact is not None and type(self.artifact) is not Artifact)
                or type(self.counts) is not ReviewCounts or type(self.events) is not tuple
                or any(type(event) is not JobEvent for event in self.events)):
            raise ReviewApplicationError("invalid_apply_result")
        if self.job_id is not None:
            try:
                validate_job_identity(self.job_id)
            except ValueError:
                raise ReviewApplicationError("invalid_apply_result") from None

    @property
    def success(self) -> bool:
        return (self.outcome is ReviewOutcome.READY and self.artifact is not None
                and self.artifact.state is ArtifactState.READY)

    def __repr__(self) -> str:
        return f"ReviewApplyResult(outcome={self.outcome.value!r}, code={self.code!r}, ready={self.success})"


class ReviewPackagePort(Protocol):
    """Codec/mapper adapter. ``validate`` performs full, all-or-nothing mapping and no writes."""

    def destination_fingerprint(self, destination: object) -> str: ...
    def export(self, translation: TranslateProjectResult, destination: object) -> ReviewPackageExport: ...
    def inspect(self, package: bytes) -> ReviewPackageIdentity: ...
    def validate(self, package: bytes, identity: ReviewPackageIdentity, source: object | None,
                 cancellation: CancellationToken | None = None) -> ReviewValidationMaterial: ...


class ReviewOperations(Protocol):
    """Job, artifact, publication, provenance and event boundary for review operations."""

    def begin(self, project_id: str, command_type: str, input_fingerprint: str, idempotency_key: str,
              destination: object, overwrite: bool) -> ReviewOperation: ...
    def publish_package(self, operation: ReviewOperation, package: bytes, identity: ReviewPackageIdentity,
                        destination: object,
                        cancellation: CancellationToken | None = None) -> Artifact: ...
    def reconstruct(self, operation: ReviewOperation, validation: ReviewValidationMaterial, source: object,
                    destination: object, overwrite: bool,
                    cancellation: CancellationToken | None = None) -> Artifact: ...
    def complete(self, operation: ReviewOperation, identity: ReviewPackageIdentity, artifact: Artifact,
                 *, reviewed_unit_ids: tuple[str, ...] = (), warnings: tuple[str, ...] = (),
                 counts: ReviewCounts = ReviewCounts()) -> ReviewOperationResult: ...
    def fail(self, operation: ReviewOperation, code: str, failure: JobFailure | None = None) -> ReviewOperationResult: ...


@dataclass(frozen=True, repr=False)
class ReviewUseCasePorts:
    packages: ReviewPackagePort
    operations: ReviewOperations

    def __repr__(self) -> str:
        return "ReviewUseCasePorts()"


class ExportReviewPackage:
    def __init__(self, ports: ReviewUseCasePorts) -> None:
        self._ports = ports

    def execute(self, command: ReviewExportCommand) -> ReviewExportResult:
        if type(command) is not ReviewExportCommand:
            return ReviewExportResult(ReviewOutcome.FAILED, "invalid_command")
        translation = command.translation
        if (not translation.output_ready or translation.job_id is None or translation.artifact is None
                or translation.artifact.state is not ArtifactState.READY
                or not translation.engine.documents or not translation.engine.translations):
            return ReviewExportResult(ReviewOutcome.FAILED, "translation_identity_unavailable")
        if _cancelled(command.cancellation, "review_before_work"):
            return ReviewExportResult(ReviewOutcome.CANCELLED, "cancelled")
        try:
            resolved_destination_fingerprint = self._ports.packages.destination_fingerprint(command.destination)
            if resolved_destination_fingerprint != command.destination_fingerprint:
                return ReviewExportResult(ReviewOutcome.CONFLICT, "destination_policy_mismatch")
            package = self._ports.packages.export(translation, command.destination)
        except ReviewApplicationError as error:
            return ReviewExportResult(ReviewOutcome.FAILED, error.code)
        except Exception:
            return ReviewExportResult(ReviewOutcome.FAILED, "review_export_failed")
        identity = package.identity
        if identity.package_fingerprint != _package_fingerprint(package.data):
            return ReviewExportResult(ReviewOutcome.FAILED, "export_fingerprint_mismatch", identity)
        if identity.schema_version != REVIEW_PACKAGE_SCHEMA_VERSION:
            return ReviewExportResult(ReviewOutcome.FAILED, "unsupported_schema_version", identity)
        if identity.project_id != translation.project_id or identity.machine_artifact_id != translation.artifact.artifact_id:
            return ReviewExportResult(ReviewOutcome.FAILED, "translation_identity_mismatch", identity)
        fingerprint = _request_fingerprint((REVIEW_EXPORT_COMMAND_TYPE, identity.project_id, translation.job_id,
                                            identity.package_fingerprint, command.destination_fingerprint))
        idempotency_key = "review-export:" + _request_fingerprint(
            (identity.package_fingerprint, command.destination_fingerprint))[7:]
        operation = None
        try:
            operation = self._ports.operations.begin(identity.project_id, REVIEW_EXPORT_COMMAND_TYPE, fingerprint,
                                                     idempotency_key, command.destination, command.overwrite)
            if operation.already_committed:
                return ReviewExportResult(ReviewOutcome.READY, "review_package_exported", identity,
                                          operation.artifact, operation.job_id)
            if _cancelled(command.cancellation, "review_before_publication"):
                terminal = self._ports.operations.fail(operation, "cancelled")
                return _export_terminal(ReviewOutcome.CANCELLED, identity, terminal)
            artifact = self._ports.operations.publish_package(operation, package.data, identity,
                                                               command.destination, command.cancellation)
            if artifact.state is not ArtifactState.READY:
                terminal = self._ports.operations.fail(operation, "publication_uncommitted")
                return _export_terminal(ReviewOutcome.FAILED, identity, terminal)
            if _cancelled(command.cancellation, "review_before_publication"):
                terminal = self._ports.operations.fail(operation, "cancelled")
                return _export_terminal(ReviewOutcome.CANCELLED, identity, terminal, code="cancelled")
            terminal = self._ports.operations.complete(operation, identity, artifact,
                                                        reviewed_unit_ids=package.unit_ids)
            if terminal.code == "job_commit_pending":
                return ReviewExportResult(ReviewOutcome.IN_PROGRESS, "job_reconciliation_required", identity,
                                          job_id=terminal.job_id, events=terminal.events)
            return _export_terminal(ReviewOutcome.READY if terminal.committed else ReviewOutcome.FAILED,
                                    identity, terminal)
        except ReviewApplicationError as error:
            if error.code == "job_commit_pending" and operation is not None:
                return ReviewExportResult(ReviewOutcome.IN_PROGRESS, "job_reconciliation_required", identity,
                                          job_id=operation.job_id)
            terminal = _fail_if_started(self._ports.operations, operation, error.code)
            return _export_terminal(_outcome_for_code(error.code), identity, terminal, code=error.code)
        except Exception:
            terminal = _fail_if_started(self._ports.operations, operation, "review_export_failed")
            return _export_terminal(ReviewOutcome.FAILED, identity, terminal, code="review_export_failed")


class ValidateReviewPackage:
    def __init__(self, ports: ReviewUseCasePorts) -> None:
        self._packages = ports.packages

    def execute(self, command: ReviewValidationCommand) -> ReviewValidationResult:
        if type(command) is not ReviewValidationCommand:
            return ReviewValidationResult(ReviewOutcome.FAILED, "invalid_command")
        if _cancelled(command.cancellation, "review_before_work"):
            return ReviewValidationResult(ReviewOutcome.CANCELLED, "cancelled")
        try:
            identity = self._packages.inspect(command.package)
            if identity.package_fingerprint != _package_fingerprint(command.package):
                return ReviewValidationResult(ReviewOutcome.INVALID, "package_fingerprint_mismatch", identity,
                                              conflicts=("package_fingerprint_mismatch",))
            if identity.schema_version == 1:
                counts = ReviewCounts(identity.document_count, identity.unit_count, 0, identity.unit_count)
                return ReviewValidationResult(ReviewOutcome.INVALID, "legacy_reexport_required", identity,
                                              counts=counts)
            if _cancelled(command.cancellation, "review_before_validation"):
                return ReviewValidationResult(ReviewOutcome.CANCELLED, "cancelled", identity)
            material = self._packages.validate(command.package, identity, command.source, command.cancellation)
            if material.identity != identity:
                return ReviewValidationResult(ReviewOutcome.INVALID, "package_changed", identity,
                                              material.counts, ("package_changed",))
            evidence = _evidence_digest(identity, material)
            if material.conflicts:
                if material.conflicts[0] == "cancelled":
                    return ReviewValidationResult(ReviewOutcome.CANCELLED, "cancelled", identity,
                                                  material.counts, (), material.warnings, evidence)
                return ReviewValidationResult(ReviewOutcome.INVALID, material.conflicts[0], identity,
                                              material.counts, material.conflicts, material.warnings, evidence)
            return ReviewValidationResult(ReviewOutcome.READY, "validated", identity, material.counts,
                                          (), material.warnings, evidence)
        except ReviewApplicationError as error:
            return ReviewValidationResult(ReviewOutcome.INVALID, error.code, conflicts=(error.code,))
        except Exception:
            return ReviewValidationResult(ReviewOutcome.INVALID, "review_validation_failed",
                                          conflicts=("review_validation_failed",))


class ApplyReviewPackage:
    def __init__(self, ports: ReviewUseCasePorts) -> None:
        self._ports = ports

    def execute(self, command: ReviewApplyCommand) -> ReviewApplyResult:
        if type(command) is not ReviewApplyCommand:
            return ReviewApplyResult(ReviewOutcome.FAILED, "invalid_command")
        if _cancelled(command.cancellation, "review_before_work"):
            return ReviewApplyResult(ReviewOutcome.CANCELLED, "cancelled")
        operation: ReviewOperation | None = None
        identity: ReviewPackageIdentity | None = None
        try:
            # V1 is inspectable but cannot create a job, lease, staging file or output.
            identity = self._ports.packages.inspect(command.package)
            if identity.package_fingerprint != _package_fingerprint(command.package):
                return ReviewApplyResult(ReviewOutcome.INVALID, "package_fingerprint_mismatch", identity,
                                         counts=ReviewCounts())
            if identity.schema_version == 1:
                return ReviewApplyResult(ReviewOutcome.INVALID, "legacy_reexport_required", identity)
            resolved_destination_fingerprint = self._ports.packages.destination_fingerprint(command.destination)
            if resolved_destination_fingerprint != command.destination_fingerprint:
                return ReviewApplyResult(ReviewOutcome.CONFLICT, "destination_policy_mismatch", identity)
            if command.idempotency_key is not None:
                idempotency_key = "review-apply:" + _request_fingerprint(
                    (command.idempotency_key,))[7:]
            else:
                idempotency_key = "review-apply:" + _request_fingerprint(
                    (identity.package_fingerprint, command.destination_fingerprint))[7:]
            fingerprint = _request_fingerprint((REVIEW_APPLY_COMMAND_TYPE, identity.project_id,
                                                identity.source_sha256, identity.machine_artifact_id,
                                                identity.machine_artifact_revision, identity.machine_artifact_digest,
                                                identity.package_fingerprint, command.destination_fingerprint))
            operation = self._ports.operations.begin(identity.project_id, REVIEW_APPLY_COMMAND_TYPE, fingerprint,
                                                     idempotency_key, command.destination, command.overwrite)
            if operation.already_committed:
                return ReviewApplyResult(ReviewOutcome.READY, "review_applied", identity,
                                         operation.artifact, operation.job_id)
            if _cancelled(command.cancellation, "review_before_validation"):
                return _apply_terminal(ReviewOutcome.CANCELLED, identity, self._ports.operations.fail(operation, "cancelled"))

            # Apply always repeats full mapping under its legal job/operation context.
            material = self._ports.packages.validate(command.package, identity, command.source, command.cancellation)
            if material.identity != identity:
                return _apply_terminal(ReviewOutcome.INVALID, identity,
                                       self._ports.operations.fail(operation, "package_changed"),
                                       counts=material.counts)
            evidence = _evidence_digest(identity, material)
            if command.validation_evidence_digest is not None and evidence != command.validation_evidence_digest:
                return _apply_terminal(ReviewOutcome.INVALID, identity,
                                       self._ports.operations.fail(operation, "stale_validation_evidence"),
                                       counts=material.counts)
            if material.conflicts:
                code = material.conflicts[0]
                conflict_outcome = _outcome_for_code(code) if "cancel" in code else ReviewOutcome.INVALID
                return _apply_terminal(conflict_outcome, identity,
                                       self._ports.operations.fail(operation, code), counts=material.counts, code=code)
            if _cancelled(command.cancellation, "review_before_reconstruction"):
                return _apply_terminal(ReviewOutcome.CANCELLED, identity,
                                       self._ports.operations.fail(operation, "cancelled"), counts=material.counts)

            artifact = self._ports.operations.reconstruct(operation, material, command.source, command.destination,
                                                          command.overwrite, command.cancellation)
            if artifact.state is not ArtifactState.READY:
                return _apply_terminal(ReviewOutcome.FAILED, identity,
                                       self._ports.operations.fail(operation, "publication_uncommitted"), counts=material.counts)
            if _cancelled(command.cancellation, "review_before_publication"):
                return _apply_terminal(ReviewOutcome.CANCELLED, identity,
                                       self._ports.operations.fail(operation, "cancelled"), counts=material.counts,
                                       code="cancelled")
            terminal = self._ports.operations.complete(operation, identity, artifact,
                                                        reviewed_unit_ids=material.reviewed_unit_ids,
                                                        warnings=material.warnings, counts=material.counts)
            if terminal.code == "job_commit_pending":
                return ReviewApplyResult(ReviewOutcome.IN_PROGRESS, "job_reconciliation_required", identity,
                                         job_id=terminal.job_id, counts=material.counts, events=terminal.events)
            return _apply_terminal(ReviewOutcome.READY if terminal.committed else ReviewOutcome.FAILED,
                                   identity, terminal, counts=material.counts)
        except ReviewApplicationError as error:
            if error.code == "job_commit_pending" and operation is not None:
                return ReviewApplyResult(ReviewOutcome.IN_PROGRESS, "job_reconciliation_required", identity,
                                         job_id=operation.job_id)
            terminal = _fail_if_started(self._ports.operations, operation, error.code)
            return _apply_terminal(_outcome_for_code(error.code), identity, terminal, code=error.code)
        except Exception:
            terminal = _fail_if_started(self._ports.operations, operation, "review_apply_failed")
            return _apply_terminal(ReviewOutcome.FAILED, identity, terminal, code="review_apply_failed")


def _validate_command_version(version: int) -> None:
    if type(version) is not int or version != REVIEW_COMMAND_VERSION:
        raise ReviewApplicationError("unsupported_command_version")


def _validate_fingerprint(value: str) -> None:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ReviewApplicationError("invalid_fingerprint")


def _package_fingerprint(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _request_fingerprint(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    return f"sha256:{digest}"


def _evidence_digest(identity: ReviewPackageIdentity, material: ReviewValidationMaterial) -> str:
    payload = {
        "conflicts": material.conflicts,
        "counts": [material.counts.documents, material.counts.units, material.counts.reviewed, material.counts.pending],
        "machine_artifact_digest": identity.machine_artifact_digest,
        "machine_artifact_id": identity.machine_artifact_id,
        "machine_artifact_revision": identity.machine_artifact_revision,
        "package_fingerprint": identity.package_fingerprint,
        "package_id": identity.package_id,
        "project_id": identity.project_id,
        "project_revision": identity.project_revision,
        "source_sha256": identity.source_sha256,
        "valid": material.valid,
        "warnings": material.warnings,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                      separators=(",", ":")).encode("ascii")).hexdigest()
    return f"sha256:{digest}"


def _cancelled(token: CancellationToken | None, point: str) -> bool:
    if token is None:
        return False
    try:
        return token.checkpoint(point) is not None
    except Exception:
        return True


def _outcome_for_code(code: str) -> ReviewOutcome:
    if "cancel" in code:
        return ReviewOutcome.CANCELLED
    if code in {"output_exists", "output_is_machine_artifact", "output_alias_unknown", "destination_policy_mismatch", "job_in_progress",
                "idempotency_conflict", "stale_revision", "stale_project_revision", "destination_conflict"}:
        return ReviewOutcome.CONFLICT
    return ReviewOutcome.FAILED


def _fail_if_started(operations: ReviewOperations, operation: ReviewOperation | None,
                     code: str) -> ReviewOperationResult | None:
    if operation is None or operation.already_committed:
        return None
    try:
        return operations.fail(operation, code)
    except Exception:
        return None


def _export_terminal(outcome: ReviewOutcome, identity: ReviewPackageIdentity,
                     terminal: ReviewOperationResult | None, *, code: str | None = None) -> ReviewExportResult:
    if terminal is None:
        return ReviewExportResult(outcome, code or "review_export_failed", identity)
    actual_outcome = (outcome if outcome in (ReviewOutcome.INVALID, ReviewOutcome.CONFLICT, ReviewOutcome.CANCELLED)
                      else outcome if terminal.committed and terminal.artifact is not None
                      else ReviewOutcome.CANCELLED if "cancel" in terminal.code else ReviewOutcome.FAILED)
    return ReviewExportResult(actual_outcome, code or terminal.code, identity,
                              terminal.artifact if terminal.committed else None,
                              terminal.job_id, terminal.events, terminal.failure)


def _apply_terminal(outcome: ReviewOutcome, identity: ReviewPackageIdentity,
                    terminal: ReviewOperationResult | None, *, counts: ReviewCounts = ReviewCounts(),
                    code: str | None = None) -> ReviewApplyResult:
    if terminal is None:
        return ReviewApplyResult(outcome, code or "review_apply_failed", identity, counts=counts)
    actual_outcome = (outcome if outcome in (ReviewOutcome.INVALID, ReviewOutcome.CONFLICT, ReviewOutcome.CANCELLED)
                      else outcome if terminal.committed and terminal.artifact is not None
                      else ReviewOutcome.CANCELLED if "cancel" in terminal.code else ReviewOutcome.FAILED)
    return ReviewApplyResult(actual_outcome, code or terminal.code, identity,
                             terminal.artifact if terminal.committed else None, terminal.job_id,
                             counts, terminal.events, terminal.failure)
