from __future__ import annotations
import hashlib
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID
import pytest
import requests.sessions
from ayvu.artifacts.model import Artifact, ArtifactRequest, ArtifactState
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobCorrelation, JobEvent, JobOutcome, JobProgress
from ayvu.jobs.execution import CancellationSource, InMemoryJobEventLog
from ayvu.review.application import (
    ApplyReviewPackage,
    ExportReviewPackage,
    MAX_REVIEW_PACKAGE_BYTES,
    ReviewApplyCommand,
    ReviewApplicationError,
    ReviewCounts,
    ReviewExportCommand,
    ReviewOperation,
    ReviewOperationResult,
    ReviewOutcome,
    ReviewPackageExport,
    ReviewPackageIdentity,
    REVIEW_SAFE_POINTS,
    ReviewValidationCommand,
    ReviewValidationMaterial,
    ReviewUseCasePorts,
    ValidateReviewPackage,
)
from ayvu.review.local_operations import LocalReviewOperations
from ayvu.review.local_start import _OperationState
from ayvu.translation.engine import EngineResult
from ayvu.translation.project_contracts import TranslateProjectResult
from ayvu.kernel import JobFailure, JobFailureFamily

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


PROJECT_ID = "00000000-0000-4000-8000-000000000148"


JOB_ID = "00000000-0000-4000-8000-000000000149"


MACHINE_ID = "00000000-0000-4000-8000-000000000150"


REVIEW_JOB_ID = "00000000-0000-4000-8000-000000000151"


OUTPUT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000152"


EVENT_ID = "00000000-0000-4000-8000-000000000153"


OPERATION_ID = "00000000-0000-4000-8000-000000000154"


UNIT_ID = "unit:v1:" + "a" * 64


PACKAGE = b"PRIVATE_REVIEW_TEXT_CANARY"


DESTINATION = Path("/private/PRIVATE_OUTPUT_PATH_CANARY/reviewed.epub")


PACKAGE_DIGEST = "sha256:" + hashlib.sha256(PACKAGE).hexdigest()


MACHINE_DIGEST = "sha256:" + "b" * 64


def _identity(raw: bytes = PACKAGE, *, version: int = 2) -> ReviewPackageIdentity:
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if version == 1:
        return ReviewPackageIdentity(1, None, digest, document_count=1, unit_count=1)
    return ReviewPackageIdentity(
        2, "rpk:v1:" + "c" * 64, digest, PROJECT_ID, 3, "d" * 64, 2048,
        MACHINE_ID, 4, MACHINE_DIGEST, 1, 1)


def _destination_fingerprint(destination: object) -> str:
    canonical = str(Path(destination).expanduser().resolve(strict=False))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _artifact(artifact_id: str, job_id: str, digest: str) -> Artifact:
    request = ArtifactRequest(PROJECT_ID, job_id, "reviewed_output", "review_reconstruct", 1,
                              "sha256:" + "e" * 64, "application/epub+zip")
    return Artifact(artifact_id, request, ArtifactState.READY, 2, NOW, NOW, 17, digest)


def _translation() -> TranslateProjectResult:
    machine = Artifact(
        MACHINE_ID,
        ArtifactRequest(PROJECT_ID, JOB_ID, "translated_output", "reconstruct", 1,
                        "sha256:" + "f" * 64, "application/epub+zip"),
        ArtifactState.READY, 4, NOW, NOW, 12, MACHINE_DIGEST)
    return TranslateProjectResult(
        project_id=PROJECT_ID, job_id=JOB_ID, state=JobState.VALIDATED, artifact=machine,
        engine=EngineResult(documents=(object(),), translations=(object(),)),
        artifact_destination=Path("/private/machine.epub"), project_revision=3)


class PackagePort:
    def __init__(self, *, schema_version: int = 2, conflicts: tuple[str, ...] = ()) -> None:
        self.schema_version = schema_version
        self.conflicts = conflicts
        self.exports = 0
        self.inspections = 0
        self.validations = 0

    def destination_fingerprint(self, destination: object) -> str:
        return _destination_fingerprint(destination)

    def export(self, translation: TranslateProjectResult, destination: object) -> ReviewPackageExport:
        self.exports += 1
        identity = _identity()
        return ReviewPackageExport(identity, PACKAGE, (UNIT_ID,))

    def inspect(self, package: bytes) -> ReviewPackageIdentity:
        self.inspections += 1
        return _identity(package, version=self.schema_version)

    def validate(self, package: bytes, identity: ReviewPackageIdentity, source: object,
                 cancellation=None) -> ReviewValidationMaterial:
        self.validations += 1
        counts = ReviewCounts(1, 1, 0 if self.conflicts else 1, 1 if self.conflicts else 0)
        return ReviewValidationMaterial(identity, not self.conflicts, counts, self.conflicts,
                                        plan=object(), reviewed_unit_ids=() if self.conflicts else (UNIT_ID,))


class Operations:
    def __init__(self) -> None:
        self.begin_calls = []
        self.publish_calls = 0
        self.package_published = False
        self.reconstruct_calls = 0
        self.publication_started = False
        self.complete_calls = []
        self.fail_calls = []
        self.completed: dict[str, tuple[str, str, Artifact]] = {}
        self.destinations: dict[str, Artifact] = {}
        self.events = (JobEvent(EVENT_ID, REVIEW_JOB_ID, 1, NOW,
                                JobCorrelation(PROJECT_ID, OPERATION_ID), JobProgress("validating", "step", 1, 1)),)

    def begin(self, project_id, command_type, input_fingerprint, idempotency_key, destination, overwrite):
        self.begin_calls.append((command_type, idempotency_key, destination, overwrite, input_fingerprint))
        if idempotency_key in self.completed:
            prior_input, prior_destination, artifact = self.completed[idempotency_key]
            if (prior_input, prior_destination) != (input_fingerprint, str(destination)):
                raise ReviewApplicationError("idempotency_conflict")
            return ReviewOperation(project_id, REVIEW_JOB_ID, input_fingerprint, idempotency_key,
                                   token=None, already_committed=True, artifact=artifact)
        if str(destination) in self.destinations and not overwrite:
            raise ReviewApplicationError("output_exists")
        return ReviewOperation(project_id, REVIEW_JOB_ID, input_fingerprint, idempotency_key, token=object())

    def publish_package(self, operation, package, identity, destination, cancellation=None):
        self.publish_calls += 1
        self.package_published = True
        return _artifact(OUTPUT_ARTIFACT_ID, REVIEW_JOB_ID, "sha256:" + "1" * 64)

    def reconstruct(self, operation, validation, source, destination, overwrite, cancellation=None):
        self.reconstruct_calls += 1
        if cancellation is not None and cancellation.checkpoint("review_before_publication") is not None:
            raise ReviewApplicationError("cancelled")
        self.publication_started = True
        artifact = _artifact(OUTPUT_ARTIFACT_ID, REVIEW_JOB_ID, "sha256:" + "2" * 64)
        self.destinations[str(destination)] = artifact
        return artifact

    def complete(self, operation, identity, artifact, *, reviewed_unit_ids=(), warnings=(), counts=ReviewCounts()):
        self.complete_calls.append((operation.idempotency_key, tuple(reviewed_unit_ids), counts))
        self.completed[operation.idempotency_key] = (operation.input_fingerprint, str(DESTINATION), artifact)
        return ReviewOperationResult(operation.job_id, JobState.VALIDATED, "review_applied", artifact, True,
                                     self.events)

    def fail(self, operation, code, failure=None):
        self.fail_calls.append(code)
        return ReviewOperationResult(operation.job_id, JobState.FAILED, code, None, False,
                                     self.events, failure or JobFailure(JobFailureFamily.INPUT, code))


def _ports(*, schema_version: int = 2, conflicts: tuple[str, ...] = ()):
    packages, operations = PackagePort(schema_version=schema_version, conflicts=conflicts), Operations()
    return ReviewUseCasePorts(packages, operations), packages, operations


def _apply_command(package: bytes = PACKAGE, *, key: str = "review-test-149", overwrite: bool = False,
                   evidence: str | None = None, destination: Path = DESTINATION) -> ReviewApplyCommand:
    fingerprint = _destination_fingerprint(destination)
    return ReviewApplyCommand(package, None, destination, fingerprint, overwrite,
                              validation_evidence_digest=evidence, idempotency_key=key)
