"""Acquire review ownership, reconcile history and create the operation state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from ..artifacts import Artifact, ArtifactRequest
from ..artifacts.destination import DestinationPublications, LocalDestinationPayloads
from ..artifacts import ArtifactService
from ..formats.local import LocalOutputGuard
from ..formats import FormatPublication, FormatService
from ..jobs import JobRequest, JobState, JobTerminalSummary
from ..jobs import JobProgress
from ..jobs import InMemoryJobEventLog
from ..jobs import JobRepository
from ..projects import ProjectManifest
from ..projects.schema import open_artifact_service, open_job_repository, open_provenance_repository
from ..provenance import UNKNOWN, ActorKind, ProducerIdentity, ProvenanceError, ProvenanceRequest, SourceKind, SubjectRef
from ..provenance.store import SqliteProvenanceRepository
from . import REVIEW_APPLY_COMMAND_TYPE, REVIEW_EXPORT_COMMAND_TYPE, ReviewApplicationError, ReviewOperation, ReviewOperationResult

from .local_files import _fingerprint, _safe_code, destination_fingerprint
from .local_context import _EPUB_MEDIA_TYPE


_REVIEW_EXPORT_ROLE = "review_package"


_REVIEW_EXPORT_STAGE = "review_export"


_REVIEW_APPLY_ROLE = "reviewed_output"


_REVIEW_APPLY_STAGE = "review_reconstruct"


_CSV_MEDIA_TYPE = "text/csv"


_UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _operation_subject(idempotency_key: str) -> SubjectRef:
    return SubjectRef("review_operation", 1, _fingerprint(idempotency_key.encode("utf-8")))


def _destination_subject(destination: object) -> SubjectRef:
    canonical = str(Path(destination).expanduser().resolve(strict=False))
    return SubjectRef("artifact_destination", 1, _fingerprint(canonical.encode("utf-8")))


class _ReviewRunGuard:
    """Serializes a request key and its destination without conflating their identities."""

    def __init__(self, guard: object, identities: tuple[str, ...]) -> None:
        self._guard = guard
        self._identities = tuple(sorted(set(identities)))
        self._entered: list[object] = []

    def __repr__(self) -> str:
        return "ReviewRunGuard()"

    def __enter__(self) -> bool:
        try:
            for identity in self._identities:
                context = self._guard.exclusive(identity)
                alone = context.__enter__()
                self._entered.append(context)
                if not alone:
                    self.__exit__(None, None, None)
                    return False
            return True
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, error_type: object, error: object, traceback: object) -> bool:
        entered, self._entered = self._entered, []
        for context in reversed(entered):
            context.__exit__(error_type, error, traceback)
        return False


@dataclass(repr=False)
class _OperationState:
    project: ProjectManifest
    jobs: JobRepository
    artifacts: ArtifactService
    provenance: SqliteProvenanceRepository
    publications: DestinationPublications
    request: ArtifactRequest
    job_id: str
    owner_token: str
    job: object
    operation_id: str
    idempotency_key: str
    command_type: str
    guard: object
    guard_open: bool
    events: InMemoryJobEventLog
    event_sequence: int
    clock: Callable[[], datetime]
    new_id: Callable[[], str]
    format_service: FormatService
    terminal: ReviewOperationResult | None = None
    published_artifact: Artifact | None = None


def _begin_review_operation(self, project_id: str, command_type: str, input_fingerprint: str, idempotency_key: str,
          destination: object, overwrite: bool) -> ReviewOperation:
    if command_type not in (REVIEW_EXPORT_COMMAND_TYPE, REVIEW_APPLY_COMMAND_TYPE):
        raise ReviewApplicationError("invalid_operation")
    project, jobs, payloads, artifacts, provenance = _open_review_storage(self, project_id, destination, overwrite)
    role, stage, media = _labels(command_type)
    request = None
    state: _OperationState | None = None
    created_job = None
    guard_entered = False
    target_fingerprint = destination_fingerprint(destination)
    guard = _ReviewRunGuard(self._projects.run_guard(project_id),
                            ("idempotency:" + _fingerprint(idempotency_key.encode("utf-8")),
                             "destination:" + target_fingerprint))
    try:
        alone = guard.__enter__()
        guard_entered = True
        if not alone:
            raise ReviewApplicationError("job_in_progress")
        # Reconcile D106-owned rename records after the previous owner is gone. Active jobs stay untouched.
        artifacts.reconcile()
        _check_review_destination(provenance, destination, command_type)
        request = JobRequest(project_id, command_type, 1, input_fingerprint, (role,))
        committed, key_registered = _find_committed_review(
            jobs, artifacts, provenance, project_id, command_type, input_fingerprint, idempotency_key, role, stage, media)
        if committed is not None:
            prior, ready = committed
            guard.__exit__(None, None, None)
            return ReviewOperation(project_id, prior.job_id, input_fingerprint, idempotency_key,
                                   token=None, already_committed=True, artifact=ready)
        # A prior fully committed request is an idempotent replay, independent of CLI overwrite policy.
        _settle_review_history(self, jobs, artifacts, project_id, command_type, input_fingerprint, role, stage, media)
        job = jobs.create(request)
        created_job = job
        job, owner_token = _activate_review_job(jobs, job, self._lease_duration)
        state = _build_review_state(self, project, jobs, artifacts, provenance, payloads, destination,
                                    project_id, job, owner_token, idempotency_key, command_type, input_fingerprint,
                                    role, stage, media, guard, key_registered)
        self._emit(state, JobProgress("preflight", "step", 1, 1))
        return ReviewOperation(project_id, job.job_id, input_fingerprint, idempotency_key, state)
    except ReviewApplicationError:
        if state is None and guard_entered:
            if created_job is not None:
                _settle_unstarted_job(self, jobs, created_job)
            guard.__exit__(None, None, None)
        raise
    except Exception as error:
        if state is None and guard_entered:
            if created_job is not None:
                _settle_unstarted_job(self, jobs, created_job)
            guard.__exit__(None, None, None)
        raise ReviewApplicationError(_safe_code(error, "review_operation_unavailable")) from None


def _open_review_storage(self, project_id, destination, overwrite):
    project = _load_project(self, project_id)
    paths = self._projects.resolve_database_paths(project_id)
    artifact_root = self._projects.resolve_artifact_root(project_id)
    jobs = open_job_repository(paths.database, paths.backups, project_id, clock=self._clock, new_id=self._new_id)
    payloads = LocalDestinationPayloads(artifact_root, destination=Path(destination), replace_existing=overwrite)
    artifacts = open_artifact_service(paths.database, paths.backups, artifact_root, project_id,
                                      payloads=payloads, clock=self._clock, new_id=self._new_id)
    provenance = open_provenance_repository(paths.database, paths.backups, project_id,
                                            clock=self._clock, new_id=self._new_id)
    return project, jobs, payloads, artifacts, provenance


def _check_review_destination(provenance, destination, command_type):
    if command_type == REVIEW_APPLY_COMMAND_TYPE:
        try:
            destinations = provenance.list_by_subject(_destination_subject(destination), limit=64)
        except ProvenanceError as error:
            raise ReviewApplicationError(_safe_code(error, "destination_history_unavailable")) from None
        if destinations.truncated:
            raise ReviewApplicationError("destination_history_incomplete")
        if any(node.record.request.operation == "translate_output"
               and node.record.request.artifacts
               for node in destinations.nodes):
            raise ReviewApplicationError("output_is_machine_artifact")


def _find_committed_review(jobs, artifacts, provenance, project_id, command_type, input_fingerprint,
                           idempotency_key, role, stage, media):
    try:
        history = provenance.list_by_subject(_operation_subject(idempotency_key), limit=1024)
    except ProvenanceError as error:
        raise ReviewApplicationError(_safe_code(error, "idempotency_history_unavailable")) from None
    if history.truncated:
        raise ReviewApplicationError("idempotency_history_incomplete")
    key_registered = False
    for node in history.nodes:
        record = node.record
        key_registered = True
        prior = jobs.get(record.request.job_id).job
        if (prior.request.command_type != command_type
                or prior.request.input_fingerprint != input_fingerprint):
            raise ReviewApplicationError("idempotency_conflict")
        committed_outcome = "review_exported" if command_type == REVIEW_EXPORT_COMMAND_TYPE else "review_applied"
        if record.request.outcome_code != committed_outcome:
            continue
        prior_request = ArtifactRequest(project_id, prior.job_id, role, stage, 1, input_fingerprint, media)
        ready = artifacts.find_reusable(prior_request).artifact
        if ready is None:
            # The person removed or changed that file, so it can no longer be replayed. This attempt
            # is new work, and an existing destination still goes through the overwrite policy.
            continue
        settled = prior if prior.state is JobState.VALIDATED else _reconcile_success(jobs, prior)
        if settled.state is JobState.VALIDATED:
            return (prior, ready), key_registered
    return None, key_registered


def _settle_review_history(self, jobs, artifacts, project_id, command_type, input_fingerprint, role, stage, media):
    for snapshot in jobs.list_jobs(project_id, limit=256, newest_first=True):
        prior = snapshot.job
        if prior.request.command_type != command_type:
            continue
        if prior.request.input_fingerprint != input_fingerprint:
            continue
        prior_request = ArtifactRequest(project_id, prior.job_id, role, stage, 1, input_fingerprint, media)
        if not prior.terminal:
            # The project run guard proves no live owner remains. Resolve its artifact before deciding
            # whether a prior rename can be reported; a missing human lineage never becomes success.
            try:
                current = jobs.get(prior.job_id).job
                if current.lease is not None:
                    jobs.release_lease(current.job_id, current.lease.owner_token)
                    current = jobs.get(current.job_id).job
                artifacts.reconcile()
                stale_artifact = artifacts.find_ready(prior_request)
                target = JobState.PARTIALLY_COMPLETED if stale_artifact is not None else JobState.FAILED
                reason = "publication_uncommitted" if stale_artifact is not None else "interrupted"
                _terminalize(self, jobs, current, target, reason)
            except Exception:
                raise ReviewApplicationError("job_reconciliation_failed") from None


def _activate_review_job(jobs, job, lease_duration):
    job = jobs.acquire_lease(job.job_id, lease_duration)
    owner_token = job.lease.owner_token
    job = jobs.transition(job.job_id, JobState.PREFLIGHT, expected_revision=job.revision)
    job = jobs.transition(job.job_id, JobState.RUNNING, expected_revision=job.revision)
    return job, owner_token


def _build_review_state(self, project, jobs, artifacts, provenance, payloads, destination, project_id, job, owner_token,
                        idempotency_key, command_type, input_fingerprint, role, stage, media, guard, key_registered):
    publications = DestinationPublications(artifacts, payloads, LocalOutputGuard(Path(destination)), FormatPublication)
    operation_id = self._new_id()
    if not key_registered:
        operation = "review_export" if command_type == REVIEW_EXPORT_COMMAND_TYPE else "apply_review"
        provenance.append(ProvenanceRequest(
            project_id, job.job_id, operation, ActorKind.SYSTEM, SourceKind.VALIDATION, _UNKNOWN_PRODUCER,
            subject=_operation_subject(idempotency_key), outcome_code="review_started",
            operation_id=operation_id, idempotency_key=idempotency_key))
    event_log = InMemoryJobEventLog(job.job_id)
    state = _OperationState(project, jobs, artifacts, provenance, publications,
                            ArtifactRequest(project_id, job.job_id, role, stage, 1, input_fingerprint, media),
                            job.job_id, owner_token, job, operation_id, idempotency_key, command_type,
                            guard, True, event_log, 0, self._clock, self._new_id, self._formats)
    return state


def _labels(command_type: str) -> tuple[str, str, str]:
    if command_type == REVIEW_EXPORT_COMMAND_TYPE:
        return _REVIEW_EXPORT_ROLE, _REVIEW_EXPORT_STAGE, _CSV_MEDIA_TYPE
    return _REVIEW_APPLY_ROLE, _REVIEW_APPLY_STAGE, _EPUB_MEDIA_TYPE


def _load_project(self, project_id: str) -> ProjectManifest:
    try:
        return self._projects.load_read_only(project_id)
    except Exception as error:
        raise ReviewApplicationError(_safe_code(error, "project_unavailable")) from None


def _terminalize(self, jobs: JobRepository, job: object, target: JobState, reason: str) -> object:
    """Settle a job left by a dead review owner through legal lifecycle edges."""
    current = job
    if current.state is JobState.QUEUED:
        current = jobs.transition(current.job_id, JobState.PREFLIGHT, expected_revision=current.revision)
    if current.state is JobState.PREFLIGHT:
        current = jobs.transition(current.job_id, JobState.RUNNING, expected_revision=current.revision)
    if current.state is JobState.COMPLETED:
        current = jobs.transition(current.job_id, JobState.VALIDATING, expected_revision=current.revision)
    if current.state is JobState.CANCELLING:
        summary = JobTerminalSummary("cancelled")
        return jobs.transition(current.job_id, JobState.CANCELLED, expected_revision=current.revision, summary=summary)
    if current.state in (JobState.RUNNING, JobState.VALIDATING):
        summary = JobTerminalSummary(reason)
        legal_target = target
        if current.state is JobState.VALIDATING:
            legal_target = JobState.PARTIALLY_COMPLETED
        return jobs.transition(current.job_id, legal_target, expected_revision=current.revision, summary=summary)
    return current


def _reconcile_success(jobs: JobRepository, job: object) -> object:
    current = job
    if current.lease is not None:
        jobs.release_lease(current.job_id, current.lease.owner_token)
        current = jobs.get(current.job_id).job
    if current.state is JobState.QUEUED:
        current = jobs.transition(current.job_id, JobState.PREFLIGHT, expected_revision=current.revision)
    if current.state is JobState.PREFLIGHT:
        current = jobs.transition(current.job_id, JobState.RUNNING, expected_revision=current.revision)
    if current.state is JobState.RUNNING:
        current = jobs.transition(current.job_id, JobState.COMPLETED, expected_revision=current.revision)
    if current.state is JobState.COMPLETED:
        current = jobs.transition(current.job_id, JobState.VALIDATING, expected_revision=current.revision)
    if current.state is JobState.VALIDATING:
        return jobs.transition(current.job_id, JobState.VALIDATED, expected_revision=current.revision,
                               summary=JobTerminalSummary("review_applied" if current.request.command_type == REVIEW_APPLY_COMMAND_TYPE
                                                          else "review_package_exported"))
    return current


def _settle_unstarted_job(self, jobs: JobRepository, job: object) -> None:
    try:
        current = jobs.get(job.job_id).job
        if current.lease is not None:
            jobs.release_lease(current.job_id, current.lease.owner_token)
            current = jobs.get(current.job_id).job
        if not current.terminal:
            _terminalize(self, jobs, current, JobState.FAILED, "operation_start_failed")
    except Exception:
        return
