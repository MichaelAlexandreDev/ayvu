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

from ayvu.artifact import Artifact, ArtifactRequest, ArtifactState
from ayvu.job import JobState
from ayvu.job_events import JobCorrelation, JobEvent, JobFailure, JobFailureFamily, JobOutcome, JobProgress
from ayvu.job_execution import CancellationSource, InMemoryJobEventLog
from ayvu.review_application import (
    ApplyReviewPackage,
    ExportReviewPackage,
    MAX_REVIEW_PACKAGE_BYTES,
    ReviewApplyCommand,
    ReviewApplicationError,
    ReviewApplyResult,
    ReviewCounts,
    ReviewExportCommand,
    ReviewExportResult,
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
from ayvu.review_composition import LocalReviewOperations, _OperationState
from ayvu.translation_engine import EngineResult
from ayvu.translate_project import TranslateProjectResult


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


def test_export_orchestrates_v2_publish_and_content_free_terminal_result():
    ports, packages, operations = _ports()
    exported = ExportReviewPackage(ports).execute(
        ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)))

    assert exported.outcome is ReviewOutcome.READY
    assert exported.artifact is not None and exported.artifact.state is ArtifactState.READY
    assert packages.exports == operations.publish_calls == 1
    assert operations.complete_calls[0][1] == (UNIT_ID,)
    assert exported.events == operations.events
    assert "PRIVATE_REVIEW_TEXT_CANARY" not in repr(exported)
    assert "PRIVATE_OUTPUT_PATH_CANARY" not in repr(
        ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)))


@pytest.mark.parametrize("identity_change", ["project", "machine_artifact"])
def test_export_rejects_identity_that_does_not_match_the_translation(identity_change):
    class MismatchedPackagePort(PackagePort):
        def export(self, translation, destination):
            self.exports += 1
            identity = _identity()
            if identity_change == "project":
                identity = replace(identity, project_id="00000000-0000-4000-8000-000000000160")
            else:
                identity = replace(identity, machine_artifact_id="00000000-0000-4000-8000-000000000161")
            return ReviewPackageExport(identity, PACKAGE, (UNIT_ID,))

    packages, operations = MismatchedPackagePort(), Operations()
    result = ExportReviewPackage(ReviewUseCasePorts(packages, operations)).execute(
        ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)))

    assert result.outcome is ReviewOutcome.FAILED and result.code == "translation_identity_mismatch"
    assert operations.begin_calls == [] and operations.publish_calls == 0


def test_export_refuses_translation_without_stable_unit_evidence():
    ports, packages, operations = _ports()
    translation = replace(_translation(), engine=EngineResult(documents=(), translations=()))

    result = ExportReviewPackage(ports).execute(
        ReviewExportCommand(translation, DESTINATION, _destination_fingerprint(DESTINATION)))

    assert result.code == "translation_identity_unavailable"
    assert operations.begin_calls == [] and packages.exports == 0


def test_validate_is_side_effect_free_and_has_instrumented_network_tripwires(tmp_path, monkeypatch):
    ports, packages, operations = _ports()
    sentinel = tmp_path / "unchanged.bin"
    sentinel.write_bytes(b"snapshot")
    before = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
    transports = []

    def blocked(*args, **kwargs):
        transports.append((args, kwargs))
        raise AssertionError("network transport must not be used")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)

    result = ValidateReviewPackage(ports).execute(ReviewValidationCommand(PACKAGE))

    assert result.valid and result.validation_evidence_digest
    assert (sentinel.read_bytes(), sentinel.stat().st_mtime_ns) == before
    assert packages.validations == 1
    assert not operations.begin_calls
    assert operations.publish_calls == operations.reconstruct_calls == 0
    assert not transports


@pytest.mark.parametrize("safe_point", ["review_before_work", "review_before_validation"])
def test_validation_cancellation_stops_before_more_package_work(safe_point):
    ports, packages, operations = _ports()

    class Cancellation:
        def checkpoint(self, requested):
            return object() if requested == safe_point else None

    result = ValidateReviewPackage(ports).execute(
        ReviewValidationCommand(PACKAGE, cancellation=Cancellation()))

    assert result.outcome is ReviewOutcome.CANCELLED
    assert packages.validations == 0
    if safe_point == "review_before_work":
        assert packages.inspections == 0
    else:
        assert packages.inspections == 1
    assert not operations.begin_calls and operations.reconstruct_calls == 0


def test_apply_revalidates_changed_package_and_rejects_stale_preview_evidence():
    ports, packages, operations = _ports()
    preview = ValidateReviewPackage(ports).execute(ReviewValidationCommand(PACKAGE))
    assert preview.valid
    changed = PACKAGE + b" changed"

    applied = ApplyReviewPackage(ports).execute(
        _apply_command(changed, evidence=preview.validation_evidence_digest))

    assert applied.outcome is ReviewOutcome.INVALID
    assert applied.code == "stale_validation_evidence"
    assert packages.validations == 2
    assert operations.reconstruct_calls == 0
    assert operations.fail_calls == ["stale_validation_evidence"]


def test_validation_rejects_package_identity_changed_by_the_mapper():
    class ChangedIdentityPort(PackagePort):
        def validate(self, package, identity, source, cancellation=None):
            self.validations += 1
            changed = _identity(b"changed package identity")
            return ReviewValidationMaterial(changed, True, ReviewCounts(1, 1, 1, 0), plan=object(),
                                            reviewed_unit_ids=(UNIT_ID,))

    packages, operations = ChangedIdentityPort(), Operations()
    result = ValidateReviewPackage(ReviewUseCasePorts(packages, operations)).execute(
        ReviewValidationCommand(PACKAGE))

    assert result.outcome is ReviewOutcome.INVALID and result.code == "package_changed"
    assert result.validation_evidence_digest is None
    assert operations.begin_calls == [] and operations.reconstruct_calls == 0


def test_structural_conflicts_are_fatal_before_reconstruction():
    ports, _packages, operations = _ports(conflicts=("unknown_unit",))

    result = ApplyReviewPackage(ports).execute(_apply_command())

    assert result.outcome is ReviewOutcome.INVALID and result.code == "unknown_unit"
    assert operations.reconstruct_calls == 0
    assert operations.fail_calls == ["unknown_unit"]


def test_v1_is_inspectable_but_apply_stops_before_operation_context():
    ports, packages, operations = _ports(schema_version=1)
    validator = ValidateReviewPackage(ports).execute(ReviewValidationCommand(PACKAGE))
    applied = ApplyReviewPackage(ports).execute(_apply_command())

    assert validator.code == applied.code == "legacy_reexport_required"
    assert validator.package.schema_version == applied.package.schema_version == 1
    assert (validator.counts.documents, validator.counts.units) == (1, 1)
    assert operations.begin_calls == [] and operations.reconstruct_calls == 0
    assert packages.validations == 0


def test_identical_idempotent_apply_reuses_result_and_overwrite_is_separate():
    ports, _packages, operations = _ports()
    use_case = ApplyReviewPackage(ports)
    first = use_case.execute(_apply_command())
    replay = use_case.execute(_apply_command())
    conflicting = use_case.execute(_apply_command(key="review-other-149"))
    overwritten = use_case.execute(_apply_command(key="review-overwrite-149", overwrite=True))

    assert first.success and replay.success and first.artifact == replay.artifact
    assert conflicting.outcome is ReviewOutcome.CONFLICT and conflicting.code == "output_exists"
    assert overwritten.success
    assert operations.reconstruct_calls == 2
    assert len(operations.complete_calls) == 2


def test_explicit_idempotency_key_is_redacted_before_the_operation_boundary():
    ports, _packages, operations = _ports()
    secret_key = "PRIVATE_REVIEW_IDEMPOTENCY_CANARY_149"
    command = _apply_command(key=secret_key)

    result = ApplyReviewPackage(ports).execute(command)

    assert result.success
    recorded_key = operations.begin_calls[0][1]
    assert recorded_key != secret_key and recorded_key.startswith("review-apply:")
    assert secret_key not in repr(command) + repr(result) + repr(operations.begin_calls)


@pytest.mark.parametrize("safe_point", [
    "review_before_work", "review_before_validation", "review_before_reconstruction", "review_before_publication",
])
def test_cancellation_at_each_application_safe_point_never_reconstructs(safe_point):
    ports, _packages, operations = _ports()

    class Cancellation:
        def checkpoint(self, requested):
            return object() if requested == safe_point else None

    result = ApplyReviewPackage(ports).execute(
        replace(_apply_command(), cancellation=Cancellation()))

    assert result.outcome is ReviewOutcome.CANCELLED
    if safe_point == "review_before_publication":
        assert operations.reconstruct_calls == 1 and not operations.publication_started
    else:
        assert operations.reconstruct_calls == 0


def test_cancellation_racing_a_completed_publish_never_returns_success():
    ports, _packages, operations = _ports()

    class Cancellation:
        publications = 0

        def checkpoint(self, requested):
            if requested != "review_before_publication":
                return None
            self.publications += 1
            return object() if self.publications == 2 else None

    result = ApplyReviewPackage(ports).execute(
        replace(_apply_command(), cancellation=Cancellation()))

    assert result.outcome is ReviewOutcome.CANCELLED
    assert result.artifact is None and not result.success
    assert operations.publication_started and operations.complete_calls == []
    assert operations.fail_calls == ["cancelled"]


def test_concurrent_cancellation_after_publish_never_returns_success():
    ports, _packages, operations = _ports()
    cancellation = CancellationSource(REVIEW_SAFE_POINTS)
    published, release = Event(), Event()

    def publish_then_pause(operation, validation, source, destination, overwrite, cancellation=None):
        operations.reconstruct_calls += 1
        operations.publication_started = True
        artifact = _artifact(OUTPUT_ARTIFACT_ID, REVIEW_JOB_ID, "sha256:" + "2" * 64)
        operations.destinations[str(destination)] = artifact
        published.set()
        if not release.wait(5):
            raise RuntimeError("cancellation race synchronization timed out")
        return artifact

    operations.reconstruct = publish_then_pause
    command = replace(_apply_command(), cancellation=cancellation.token)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(ApplyReviewPackage(ports).execute, command)
        try:
            assert published.wait(5)
            requested = cancellation.request("user_requested", NOW)
            assert requested.status.value == "accepted"
        finally:
            release.set()
        result = future.result(timeout=10)

    assert result.outcome is ReviewOutcome.CANCELLED
    assert result.artifact is None and not result.success
    assert operations.destinations and operations.complete_calls == []
    assert operations.fail_calls == ["cancelled"]


def test_export_cancellation_after_publish_never_returns_ready():
    ports, _packages, operations = _ports()

    class Cancellation:
        publications = 0

        def checkpoint(self, requested):
            if requested != "review_before_publication":
                return None
            self.publications += 1
            return object() if self.publications == 2 else None

    result = ExportReviewPackage(ports).execute(
        replace(ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)),
                cancellation=Cancellation()))

    assert result.outcome is ReviewOutcome.CANCELLED
    assert result.artifact is None and operations.package_published
    assert operations.complete_calls == [] and operations.fail_calls == ["cancelled"]


def test_apply_result_failure_events_and_reprs_are_structured_and_redacted():
    ports, _packages, operations = _ports(conflicts=("review_not_approved",))
    result = ApplyReviewPackage(ports).execute(_apply_command())

    assert result.failure is not None and result.failure.code == "review_not_approved"
    assert result.events == operations.events
    projection = repr(result) + repr(result.failure) + repr(ReviewValidationCommand(PACKAGE))
    assert "PRIVATE_REVIEW_TEXT_CANARY" not in projection
    assert "PRIVATE_OUTPUT_PATH_CANARY" not in projection


def test_oversized_command_is_rejected_before_ports_are_called():
    ports, packages, operations = _ports()
    with pytest.raises(ReviewApplicationError, match="invalid_package"):
        ReviewValidationCommand(b"x" * (MAX_REVIEW_PACKAGE_BYTES + 1))
    assert packages.inspections == packages.validations == 0
    assert not operations.begin_calls


def test_destination_policy_fingerprint_must_match_the_resolved_target():
    ports, packages, operations = _ports()

    exported = ExportReviewPackage(ports).execute(
        ReviewExportCommand(_translation(), DESTINATION, "sha256:" + "9" * 64))
    applied = ApplyReviewPackage(ports).execute(
        replace(_apply_command(), destination_fingerprint="sha256:" + "8" * 64))

    assert exported.outcome is applied.outcome is ReviewOutcome.CONFLICT
    assert exported.code == applied.code == "destination_policy_mismatch"
    assert packages.exports == 0 and packages.inspections == 1
    assert operations.begin_calls == []


@pytest.mark.parametrize("event_committed", [False, True], ids=["before-event-commit", "after-event-commit"])
def test_terminal_event_acknowledgement_failure_only_reports_a_recorded_outcome(monkeypatch, event_committed):
    class Jobs:
        def __init__(self):
            self.job = SimpleNamespace(job_id=REVIEW_JOB_ID, revision=0, state=JobState.RUNNING)

        def transition(self, job_id, target, *, expected_revision, summary=None):
            assert job_id == REVIEW_JOB_ID and expected_revision == self.job.revision
            self.job = SimpleNamespace(job_id=job_id, revision=expected_revision + 1, state=target)
            return self.job

        def get(self, job_id):
            assert job_id == REVIEW_JOB_ID
            return SimpleNamespace(job=self.job)

    class Guard:
        def __exit__(self, *_args):
            return False

    jobs = Jobs()
    event_ids = iter((str(UUID(int=155)), str(UUID(int=156))))
    state = _OperationState(
        project=SimpleNamespace(project_id=PROJECT_ID), jobs=jobs, artifacts=None, provenance=None,
        publications=None, request=None, job_id=REVIEW_JOB_ID, owner_token="owner", job=jobs.job,
        operation_id=OPERATION_ID, idempotency_key="event-commit-test", command_type="apply_review_package",
        guard=Guard(), guard_open=True, events=InMemoryJobEventLog(REVIEW_JOB_ID), event_sequence=0,
        clock=lambda: NOW, new_id=lambda: next(event_ids), format_service=None)
    operation = ReviewOperation(PROJECT_ID, REVIEW_JOB_ID, PACKAGE_DIGEST, "event-commit-test", state)
    original_emit = LocalReviewOperations._emit
    def interrupt_emit(self, current, payload):
        if isinstance(payload, JobOutcome) and payload.state is JobState.VALIDATED:
            if event_committed:
                event = JobEvent(str(UUID(int=157)), current.job_id, current.event_sequence + 1, NOW,
                                 JobCorrelation(PROJECT_ID, OPERATION_ID), payload)
                current.events.emit(event)
                current.event_sequence += 1
            raise RuntimeError("event acknowledgement lost")
        return original_emit(self, current, payload)

    monkeypatch.setattr(LocalReviewOperations, "_emit", interrupt_emit)
    monkeypatch.setattr(LocalReviewOperations, "_write_lineage", lambda *_args, **_kwargs: None)
    operations = object.__new__(LocalReviewOperations)

    result = operations.complete(operation, _identity(), _artifact(OUTPUT_ARTIFACT_ID, REVIEW_JOB_ID, MACHINE_DIGEST))

    if event_committed:
        assert result.committed and result.code == "review_applied"
        assert result.events[-1].payload.state is JobState.VALIDATED
    else:
        assert not result.committed and result.code == "job_commit_pending"
        assert not any(isinstance(event.payload, JobOutcome) for event in result.events)
