"""Concrete review adapters: v2 codec, local project stores, EPUB format work and D106 publication."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..artifacts import Artifact, ArtifactCancelledError, ArtifactError, ArtifactIntegrityError, ArtifactState
from ..formats import AliasVerdict, ReconstructionRequest
from ..formats.local import LocalFormatSource, LocalOutputGuard
from ..formats import FormatService
from ..jobs import JobState, JobTerminalSummary, MAX_LEASE_DURATION
from ..jobs import CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobOutcome, JobProgress
from ..jobs import CancellationSource
from ..projects.store import ProjectJsonRepository, file_digest
from ..provenance import ActorKind, ProvenanceError, ProvenanceRequest, SourceKind, SubjectRef
from . import REVIEW_APPLY_COMMAND_TYPE, REVIEW_EXPORT_COMMAND_TYPE, REVIEW_SAFE_POINTS, ReviewApplicationError, ReviewCounts, ReviewOperation, ReviewOperationResult, ReviewPackageIdentity, ReviewValidationMaterial
from . import ReviewExchangeError, decode_review_package
from ..translation import TRANSLATE_COMMAND_TYPE
from ..kernel import JobFailure, JobFailureFamily, JobWarning, CancellationToken

from .local_files import _safe_code, read_review_input
from .local_context import _MappedReview
from .local_packages import LocalReviewPackagePort
from .local_start import _OperationState, _UNKNOWN_PRODUCER, _begin_review_operation, _operation_subject, _utc_now


def _lineage_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((kind, *parts)).encode("utf-8")).hexdigest()
    return f"review-{kind}:{digest}"


def _has_recorded_terminal_outcome(state: _OperationState, expected_code: str) -> bool:
    outcome = state.events.outcome
    return (state.job.state is JobState.VALIDATED and outcome is not None
            and outcome.state is JobState.VALIDATED and outcome.summary.reason_code == expected_code)


class LocalReviewOperations:
    """Concrete job/artifact/provenance owner. Files publish through D106's journal and reconciliation."""

    def __init__(self, processing_dir: Path, formats: FormatService, *, clock: Callable[[], datetime] = _utc_now,
                 new_id: Callable[[], str] = lambda: str(uuid4()), lease_duration: timedelta = MAX_LEASE_DURATION) -> None:
        self._processing_dir = Path(processing_dir)
        self._projects = ProjectJsonRepository(self._processing_dir)
        self._formats = formats
        self._clock = clock
        self._new_id = new_id
        self._lease_duration = lease_duration

    def __repr__(self) -> str:
        return "LocalReviewOperations()"

    def cancellation(self) -> CancellationSource:
        return CancellationSource(tuple(dict.fromkeys((*REVIEW_SAFE_POINTS, *self._formats.safe_points("epub")))))

    def begin(self, project_id: str, command_type: str, input_fingerprint: str, idempotency_key: str,
              destination: object, overwrite: bool) -> ReviewOperation:
        return _begin_review_operation(self, project_id, command_type, input_fingerprint, idempotency_key,
                                       destination, overwrite)

    def publish_package(self, operation: ReviewOperation, package: bytes, identity: ReviewPackageIdentity,
                        destination: object,
                        cancellation: CancellationToken | None = None) -> Artifact:
        state = self._state(operation)
        try:
            def validate_staged(staged: object) -> None:
                actual = read_review_input(Path(staged))
                if actual != package:
                    raise ArtifactIntegrityError("review_package_changed")
                try:
                    decoded = decode_review_package(actual)
                    measured = LocalReviewPackagePort._identity(decoded, actual)
                except ReviewExchangeError:
                    raise ArtifactIntegrityError("invalid_review_package") from None
                if measured != identity:
                    raise ArtifactIntegrityError("review_package_changed")

            state.publications.publication(state.request, state.owner_token, validate_staged)
            self._emit(state, JobProgress("publishing", "step", 0, 1))
            artifact = state.publications.publish(state.request, (package,), owner_token=state.owner_token,
                                                  cancellation=cancellation)
            state.published_artifact = artifact
            self._emit(state, JobProgress("publishing", "step", 1, 1))
            return artifact
        except ArtifactError as error:
            raise ReviewApplicationError(_safe_code(error, "publication_failed")) from None

    def reconstruct(self, operation: ReviewOperation, validation: ReviewValidationMaterial, source: object,
                    destination: object, overwrite: bool,
                    cancellation: CancellationToken | None = None) -> Artifact:
        state = self._state(operation)
        mapped = validation.plan
        if type(mapped) is not _MappedReview or not validation.valid:
            raise ReviewApplicationError("invalid_review_plan")
        source = mapped.source if source is None else source
        output_path = Path(destination)
        source_guard = LocalOutputGuard(output_path)
        if source_guard.verdict(source) is not AliasVerdict.DISTINCT:
            raise ReviewApplicationError("output_alias_unknown")
        # The machine artifact is external to project storage; its original bytes were digest-checked
        # by reconstructing its immutable package rows during validation.
        if output_path.exists():
            try:
                if file_digest(output_path) == (mapped.machine_artifact.size, mapped.machine_artifact.digest):
                    raise ReviewApplicationError("output_is_machine_artifact")
            except ReviewApplicationError:
                raise
            except Exception:
                raise ReviewApplicationError("output_alias_unknown") from None
        if cancellation is not None and cancellation.checkpoint("review_before_reconstruction") is not None:
            raise ReviewApplicationError("cancelled")

        def validate_staged(staged: object) -> None:
            # Staging is the end of reconstruction: the phase cannot be reported after validation starts.
            self._emit(state, JobProgress("reconstructing", "unit", validation.counts.units, validation.counts.units))
            self._emit(state, JobProgress("validating", "step", 0, 1))
            current = self._projects.load_read_only(mapped.project.project_id)
            current_artifact = state.artifacts.get(mapped.machine_artifact.artifact_id)
            if (current.revision != mapped.identity.project_revision
                    or current.source_sha256 != mapped.identity.source_sha256
                    or current.source_size != mapped.identity.source_size
                    or current_artifact.revision != mapped.identity.machine_artifact_revision
                    or current_artifact.digest != mapped.identity.machine_artifact_digest
                    or current_artifact.state is not ArtifactState.READY):
                raise ArtifactIntegrityError("stale_review_identity")
            current_job = state.jobs.get(mapped.machine_artifact.request.job_id).job
            if (current_job.request.command_type != TRANSLATE_COMMAND_TYPE
                    or current_job.state not in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)):
                raise ArtifactIntegrityError("stale_review_identity")
            if source.fingerprint() != f"sha256:{mapped.identity.source_sha256}":
                raise ArtifactIntegrityError("source_changed")
            checked = state.format_service.validate("epub", LocalFormatSource(Path(staged)), cancellation=cancellation)
            if checked.failure is not None:
                raise ArtifactIntegrityError(checked.failure.code)
            if not checked.ok:
                raise ArtifactIntegrityError("invalid_epub")
            self._emit(state, JobProgress("validating", "step", 1, 1))
            if cancellation is not None and cancellation.checkpoint("review_before_publication") is not None:
                raise ArtifactCancelledError("cancelled")

        publication = state.publications.publication(state.request, state.owner_token, validate_staged)
        self._emit(state, JobProgress("reconstructing", "unit", 0, validation.counts.units))
        materialized = state.format_service.reconstruct(
            "epub", source,
            ReconstructionRequest(mapped.plan.documents, mapped.plan.translations),
            publication, cancellation=cancellation)
        if materialized.failure is not None or materialized.artifact is None:
            raise ReviewApplicationError(materialized.failure.code if materialized.failure else "reconstruction_failed")
        state.published_artifact = materialized.artifact
        return materialized.artifact

    def complete(self, operation: ReviewOperation, identity: ReviewPackageIdentity, artifact: Artifact,
                 *, reviewed_unit_ids: tuple[str, ...] = (), warnings: tuple[str, ...] = (),
                 counts: ReviewCounts = ReviewCounts()) -> ReviewOperationResult:
        state = self._state(operation)
        if artifact.state is not ArtifactState.READY:
            return self.fail(operation, "artifact_not_ready")
        try:
            self._write_lineage(state, identity, artifact, reviewed_unit_ids)
        except Exception:
            result = self._finish(state, JobState.PARTIALLY_COMPLETED, "provenance_failed",
                                  JobFailure(JobFailureFamily.RESOURCE, "provenance_failed"), counts)
            return result
        try:
            for warning in warnings[:64]:
                self._emit(state, JobWarning(warning, (("count", 1),)))
            state.job = state.jobs.transition(state.job.job_id, JobState.COMPLETED,
                                              expected_revision=state.job.revision)
            state.job = state.jobs.transition(state.job.job_id, JobState.VALIDATING,
                                              expected_revision=state.job.revision)
            return self._finish(state, JobState.VALIDATED,
                                "review_package_exported" if state.command_type == REVIEW_EXPORT_COMMAND_TYPE else "review_applied",
                                None, counts, artifact=artifact)
        except Exception:
            # The final provenance marker and ready artifact are restart-recoverable. Report success
            # here only if the terminal event also reached the ordered stream; otherwise recovery
            # can surface the committed job without inventing a missing event.
            try:
                state.job = state.jobs.get(state.job_id).job
            except Exception:
                pass
            self._close_guard(state)
            expected_code = ("review_package_exported" if state.command_type == REVIEW_EXPORT_COMMAND_TYPE
                             else "review_applied")
            if _has_recorded_terminal_outcome(state, expected_code):
                result = ReviewOperationResult(state.job_id, JobState.VALIDATED,
                                               expected_code,
                                               artifact, True, state.events.replay())
            else:
                result = ReviewOperationResult(state.job_id, state.job.state, "job_commit_pending", None,
                                               False, state.events.replay())
            state.terminal = result if result.committed else None
            return result

    def fail(self, operation: ReviewOperation, code: str, failure: JobFailure | None = None) -> ReviewOperationResult:
        state = self._state(operation)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "review_operation_failed"
        if state.terminal is not None:
            return state.terminal
        if "cancel" in code and state.job.state is JobState.RUNNING and state.published_artifact is None:
            try:
                state.job = state.jobs.transition(state.job.job_id, JobState.CANCELLING,
                                                  expected_revision=state.job.revision)
                # The ordered stream refuses a cancelled outcome that no request and acknowledgement explain.
                self._emit(state, CancellationRequest("cancelled", self._clock()))
                self._emit(state, CancellationAcknowledgement("cancelled", "review_operation"))
                return self._finish(state, JobState.CANCELLED, "cancelled",
                                    JobFailure(JobFailureFamily.CANCELLED, "cancelled"), ReviewCounts())
            except Exception as error:
                raise ReviewApplicationError(_safe_code(error, "job_commit_failed")) from None
        if state.published_artifact is not None:
            return self._finish(state, JobState.PARTIALLY_COMPLETED, "publication_uncommitted",
                                failure or JobFailure(JobFailureFamily.INTEGRITY, code), ReviewCounts())
        return self._finish(state, JobState.FAILED, code,
                            failure or JobFailure(JobFailureFamily.INPUT, code), ReviewCounts())

    @staticmethod
    def _state(operation: ReviewOperation) -> _OperationState:
        if type(operation) is not ReviewOperation or not isinstance(operation.token, _OperationState):
            raise ReviewApplicationError("invalid_operation")
        return operation.token

    def _write_lineage(self, state: _OperationState, identity: ReviewPackageIdentity, artifact: Artifact,
                       reviewed_unit_ids: tuple[str, ...]) -> None:
        package_value = identity.package_fingerprint
        if state.command_type == REVIEW_EXPORT_COMMAND_TYPE:
            operation, actor, kind = "review_export", ActorKind.SYSTEM, SourceKind.PIPELINE
            package_outcome, unit_outcome, committed_outcome = "review_package_staged", "review_unit_staged", "review_exported"
        else:
            operation, actor, kind = "apply_review", ActorKind.HUMAN, SourceKind.HUMAN
            package_outcome, unit_outcome, committed_outcome = "review_package_validated", "human_edit_validated", "review_applied"
        package_subject = SubjectRef("review_package", 1, package_value)
        package_record = state.provenance.append(ProvenanceRequest(
            state.project.project_id, state.job_id, operation, actor, kind, _UNKNOWN_PRODUCER,
            subject=package_subject, outcome_code=package_outcome,
            artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
            operation_id=state.operation_id, idempotency_key=_lineage_key("package", state.idempotency_key, state.job_id)))
        source_record = state.provenance.append(ProvenanceRequest(
            state.project.project_id, state.job_id, operation, actor, SourceKind.VALIDATION, _UNKNOWN_PRODUCER,
            subject=SubjectRef("source_revision", 1, f"sha256:{identity.source_sha256}"), outcome_code="source_verified",
            parents=(package_record.record_id,),
            artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
            operation_id=state.operation_id, idempotency_key=_lineage_key("source", state.idempotency_key, state.job_id)))
        for unit_id in reviewed_unit_ids:
            parents: tuple[str, ...] = ()
            if state.command_type == REVIEW_APPLY_COMMAND_TYPE:
                try:
                    machine = state.provenance.list_by_subject(SubjectRef("translation_unit", 1, unit_id), limit=64)
                    if machine.truncated:
                        raise ReviewApplicationError("unit_provenance_history_incomplete")
                    match = next((node.record for node in machine.nodes
                                  if node.record.request.operation == "translate_unit"
                                  and node.record.request.source_kind in (SourceKind.PROVIDER, SourceKind.CACHE,
                                                                        SourceKind.TRANSLATION_MEMORY, SourceKind.PIPELINE)
                                  and identity.machine_artifact_id in node.record.request.artifacts), None)
                    if match is None:
                        raise ReviewApplicationError("unit_provenance_unavailable")
                    parents = (match.record_id,)
                except ProvenanceError as error:
                    raise ReviewApplicationError(_safe_code(error, "unit_provenance_unavailable")) from None
            state.provenance.append(ProvenanceRequest(
                state.project.project_id, state.job_id, "review_unit", actor, kind, _UNKNOWN_PRODUCER,
                subject=SubjectRef("translation_unit", 1, unit_id), outcome_code=unit_outcome,
                parents=tuple(sorted({*parents, package_record.record_id})),
                artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
                operation_id=state.operation_id,
                idempotency_key=_lineage_key("unit", state.idempotency_key, state.job_id, unit_id)))
        state.provenance.append(ProvenanceRequest(
            state.project.project_id, state.job_id, operation, actor, SourceKind.VALIDATION, _UNKNOWN_PRODUCER,
            subject=_operation_subject(state.idempotency_key), outcome_code=committed_outcome,
            parents=(package_record.record_id, source_record.record_id),
            artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
            operation_id=state.operation_id, idempotency_key=_lineage_key("complete", state.idempotency_key, state.job_id)))

    def _finish(self, state: _OperationState, target: JobState, code: str, failure: JobFailure | None,
                counts: ReviewCounts, *, artifact: Artifact | None = None) -> ReviewOperationResult:
        if state.terminal is not None:
            return state.terminal
        job = state.job
        summary = JobTerminalSummary(code, (("documents", counts.documents), ("units", counts.units),
                                             ("reviewed", counts.reviewed), ("pending", counts.pending)))
        # Only a FAILED outcome carries a failure; the other terminal states state their reason in the summary.
        outcome = JobOutcome(target, summary, failure if target is JobState.FAILED else None)
        try:
            job = state.jobs.transition(job.job_id, target, expected_revision=job.revision, summary=summary)
        except Exception as error:
            self._close_guard(state)
            raise ReviewApplicationError(_safe_code(error, "job_commit_failed")) from None
        state.job = job
        self._emit(state, outcome)
        result = ReviewOperationResult(state.job_id, target, code,
                                       artifact if target is JobState.VALIDATED else None,
                                       target is JobState.VALIDATED and artifact is not None,
                                       state.events.replay(), failure)
        state.terminal = result
        self._close_guard(state)
        return result

    def _emit(self, state: _OperationState, payload: object) -> None:
        event = JobEvent(self._new_id(), state.job_id, state.event_sequence + 1, self._clock(),
                         JobCorrelation(state.project.project_id, state.operation_id), payload)
        state.events.emit(event)
        state.event_sequence += 1

    @staticmethod
    def _close_guard(state: _OperationState) -> None:
        if state.guard_open:
            state.guard_open = False
            state.guard.__exit__(None, None, None)
