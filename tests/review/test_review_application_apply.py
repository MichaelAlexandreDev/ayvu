from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest

from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobCorrelation, JobEvent, JobOutcome
from ayvu.jobs.execution import CancellationSource, InMemoryJobEventLog
from ayvu.review.application import ApplyReviewPackage, ExportReviewPackage, MAX_REVIEW_PACKAGE_BYTES, ReviewApplicationError, ReviewCounts, ReviewExportCommand, ReviewOperation, ReviewOutcome, REVIEW_SAFE_POINTS, ReviewValidationCommand, ReviewValidationMaterial, ReviewUseCasePorts, ValidateReviewPackage
from ayvu.composition.builders.review_operations import LocalReviewOperations
from ayvu.composition.builders.review_start import _OperationState

from tests.review.review_application_support import DESTINATION, MACHINE_DIGEST, NOW, OPERATION_ID, OUTPUT_ARTIFACT_ID
from tests.review.review_application_support import Operations, PACKAGE, PACKAGE_DIGEST, PROJECT_ID, PackagePort
from tests.review.review_application_support import REVIEW_JOB_ID, UNIT_ID, _apply_command, _artifact
from tests.review.review_application_support import _identity, _ports, _translation


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
