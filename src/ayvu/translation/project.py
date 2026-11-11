"""Translates one project's book as one durable job, for any consumer.

This is the application's whole answer to "translate this project": it checks the project and its
source revision, then owns the job from creation to its one terminal outcome, while the engine does
the book and the injected ports do everything concrete. It imports no framework, store or provider.

Dry run and preview are refused here on purpose. They need no project and create none, and identity
must never be made up to fit them, so they stay on the current path behind their own adapter.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ..artifacts import Artifact, ArtifactError, ArtifactRequest
from ..formats import FormatError
from ..jobs import MAX_LEASE_DURATION, JobRequest, JobState, JobTerminalSummary, TranslationJob
from ..jobs import CancellationSource
from ..projects import ProjectError
from ..provenance import (
    UNKNOWN, ActorKind, ConfigurationRef, ProducerIdentity, ProvenanceError, ProvenanceRequest, SourceKind,
    SubjectRef,
)
from .engine import (
    ENGINE_SAFE_POINTS, STATUS_CACHE, STATUS_ERROR, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED,
    EngineResult,
)
from ..kernel import JobError, JobFailure, JobFailureFamily
from .project_contracts import (
    ContinuationTicket, INTERRUPTED, INTERRUPTED_AFTER_PUBLICATION, OUTPUT_ROLE, RECONSTRUCT_STAGE,
    RECONSTRUCT_STAGE_VERSION, TRANSLATE_BEFORE_START, TRANSLATE_COMMAND_TYPE, TranslateCommandError,
    TranslateObserver, TranslateProjectCommand, TranslateProjectPorts, TranslateProjectResult, _OWN_STATES,
    resolve_execution_plan,
)
from .project_run import _Run


DEFAULT_LEASE_RENEWAL = timedelta(minutes=5)
_UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)


class TranslateProject:
    def __init__(self, ports: TranslateProjectPorts, *, clock: Callable[[], datetime], new_id: Callable[[], str] = lambda: str(uuid4()),
                 lease_duration: timedelta = MAX_LEASE_DURATION, lease_renewal: timedelta = DEFAULT_LEASE_RENEWAL) -> None:
        self._ports = ports
        self._clock = clock
        self._new_id = new_id
        self._lease_duration = lease_duration
        self._lease_renewal = lease_renewal

    def __repr__(self) -> str:
        return "TranslateProject()"

    def cancellation(self, adapter_id: str = "epub") -> CancellationSource:
        """The source to give ``execute``, for a caller that may want to stop the run."""
        return CancellationSource(tuple(dict.fromkeys((TRANSLATE_BEFORE_START, *ENGINE_SAFE_POINTS,
            *self._ports.formats.safe_points(adapter_id), *self._ports.runtime_safe_points))))

    def reconcile(self, project_id: str) -> tuple[str, ...]:
        """Gives a terminal outcome to every job whose owner is gone. Returns those jobs, oldest first."""
        ports = self._ports
        ports.publications.reconcile()
        settled: list[str] = []
        for snapshot in ports.jobs.list_jobs(project_id, states=_OWN_STATES):
            job = snapshot.job
            # An owner takes its lease right after creating the job, so a later state without one has none.
            if snapshot.requires_reconciliation or (job.lease is None and job.state is not JobState.QUEUED):
                if self._settle_interrupted(job):
                    settled.append(job.job_id)
        return tuple(settled)

    def _settle_interrupted(self, job: TranslationJob) -> bool:
        ports = self._ports
        artifact = ports.publications.find_ready(ArtifactRequest(
            job.request.project_id, job.job_id, OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION, job.request.input_fingerprint))
        path = {
            JobState.QUEUED: (JobState.CANCELLED,),
            JobState.PREFLIGHT: (JobState.FAILED,),
            JobState.RUNNING: (JobState.PARTIALLY_COMPLETED,) if artifact is not None else (JobState.FAILED,),
            JobState.CANCELLING: (JobState.CANCELLED,),
            JobState.COMPLETED: (JobState.VALIDATING, JobState.PARTIALLY_COMPLETED),
            JobState.VALIDATING: (JobState.PARTIALLY_COMPLETED,),
        }[job.state]
        reason = INTERRUPTED_AFTER_PUBLICATION if artifact is not None else INTERRUPTED
        producer = self._ports.recorded_producer(job) if self._ports.recorded_producer is not None else None
        self._append_provenance(job.request.project_id, job.job_id, reason, artifact, None, None, producer)
        try:
            for state in path:
                terminal = state in (JobState.CANCELLED, JobState.FAILED, JobState.PARTIALLY_COMPLETED)
                job = ports.jobs.transition(job.job_id, state, expected_revision=job.revision,
                                            summary=JobTerminalSummary(reason) if terminal else None)
        except JobError:
            # Another process settled it first, and the record it wrote stands.
            return False
        return True

    def _append_provenance(self, project_id: str, job_id: str, reason: str, artifact: Artifact | None,
                           configuration_ref: str | None, operation_id: str | None,
                           producer: ProducerIdentity | None = None) -> bool:
        references = () if configuration_ref is None else (ConfigurationRef("configuration", configuration_ref),)
        try:
            # The key allows one outcome per job, whichever run or reconciliation writes it first.
            self._ports.provenance.append(ProvenanceRequest(
                project_id, job_id, TRANSLATE_COMMAND_TYPE, ActorKind.SYSTEM, SourceKind.PIPELINE, producer or _UNKNOWN_PRODUCER,
                outcome_code=reason, configuration_refs=references, artifacts=() if artifact is None else (artifact.artifact_id,),
                correlation_id=operation_id, idempotency_key=f"job:{job_id}:outcome"))
        except ProvenanceError:
            return False
        return True

    def _append_unit_provenance(self, project_id: str, job_id: str, artifact: Artifact,
                                engine: EngineResult, destination: object, producer: ProducerIdentity | None = None) -> bool:
        """Record machine origins by stable unit, without retaining any source or translated text."""
        sources = {
            STATUS_CACHE: SourceKind.CACHE,
            STATUS_MEMORY: SourceKind.TRANSLATION_MEMORY,
            STATUS_TRANSLATED: SourceKind.PROVIDER,
            STATUS_MISSING: SourceKind.PIPELINE,
            STATUS_ERROR: SourceKind.PIPELINE,
            # A replay is a machine artifact restored from a prior durable result.
            "replay": SourceKind.PIPELINE,
        }
        destination_path = Path(destination).expanduser().resolve(strict=False)
        destination_digest = hashlib.sha256(str(destination_path).encode("utf-8")).hexdigest()
        try:
            self._ports.provenance.append(ProvenanceRequest(
                project_id, job_id, "translate_output", ActorKind.SYSTEM, SourceKind.PIPELINE, producer or _UNKNOWN_PRODUCER,
                subject=SubjectRef("artifact_destination", 1, f"sha256:{destination_digest}"),
                outcome_code="machine_translation_output", artifacts=(artifact.artifact_id,),
                idempotency_key=f"destination:{job_id}"))
        except ProvenanceError:
            return False
        seen: set[str] = set()
        for unit_id, status in engine.unit_sources:
            if unit_id in seen:
                return False
            seen.add(unit_id)
            source_kind = sources.get(status)
            if source_kind is None:
                return False
            outcome = ("translation_missing" if status == STATUS_MISSING else
                       "translation_failed" if status == STATUS_ERROR else "machine_translation")
            try:
                self._ports.provenance.append(ProvenanceRequest(
                    project_id, job_id, "translate_unit", ActorKind.SYSTEM, source_kind, producer or _UNKNOWN_PRODUCER,
                    subject=SubjectRef("translation_unit", 1, unit_id), outcome_code=outcome,
                    artifacts=(artifact.artifact_id,), idempotency_key=f"machine:{job_id}:{unit_id}"))
            except ProvenanceError:
                return False
        return True

    def execute(self, command: TranslateProjectCommand, *, cancellation: CancellationSource | None = None,
                observer: TranslateObserver | None = None, continuation: ContinuationTicket | None = None) -> TranslateProjectResult:
        """Everything that can be refused is refused before a job exists or a provider is touched.

        With ``continuation`` the new job links to that one, which first gets its one terminal outcome if it has none.
        """
        if type(command) is not TranslateProjectCommand:
            raise TranslateCommandError("invalid_command")

        def refused(family: JobFailureFamily, code: str) -> TranslateProjectResult:
            return TranslateProjectResult(command.project_id, failure=JobFailure(family, code))

        if command.accepted_output is not None:
            try:
                command.accepted_output.revalidate()
            except ValueError:
                return refused(JobFailureFamily.INTEGRITY, "destination_changed")
        ports = self._ports
        try:
            project = ports.projects.load(command.project_id)
        except ProjectError as error:
            return refused(JobFailureFamily.INPUT, _safe_code(error, "project_unavailable"))
        options = command.options
        if (project.source, project.target) != (options.source, options.target):
            return refused(JobFailureFamily.INPUT, "language_pair_mismatch")
        if project.source_sha256 != command.source_sha256:
            return refused(JobFailureFamily.INTEGRITY, "source_changed")
        try:
            fingerprint = ports.source.fingerprint()
        except Exception:
            return refused(JobFailureFamily.RESOURCE, "source_unreadable")
        if fingerprint != f"sha256:{project.source_sha256}":
            return refused(JobFailureFamily.INTEGRITY, "source_changed")
        try:
            source = cancellation or self.cancellation(command.adapter_id)
        except FormatError:
            return refused(JobFailureFamily.CAPABILITY, "unknown_adapter")
        plan = resolve_execution_plan(command, project)
        request = JobRequest(command.project_id, TRANSLATE_COMMAND_TYPE, command.execution_revision,
                             plan.input_fingerprint, (OUTPUT_ROLE, *command.additional_output_roles))
        fingerprints = (plan.input_fingerprint,)
        if continuation is not None:
            try:
                parent = ports.jobs.get(continuation.job_id).job
            except JobError as error:
                return refused(JobFailureFamily.RESOURCE, _safe_code(error, "job_store_unavailable"))
            # The parent's plan comes first: a resume differs from its parent in the overwrite flag, so its own guard says nothing.
            fingerprints = tuple(dict.fromkeys((parent.request.input_fingerprint, plan.input_fingerprint)))
        try:
            with self._guards(fingerprints) as alone:
                if not alone:
                    return refused(JobFailureFamily.POLICY, "job_in_progress")
                try:
                    if continuation is None:
                        self._take_over(command.project_id, plan.input_fingerprint)
                        job = ports.jobs.create(request)
                    else:
                        job = self._continue(continuation, request)
                        if isinstance(job, str):
                            return refused(JobFailureFamily.POLICY, job)
                    job = ports.jobs.acquire_lease(job.job_id, self._lease_duration)
                except (JobError, ArtifactError) as error:
                    return refused(JobFailureFamily.RESOURCE, _safe_code(error, "job_store_unavailable"))
                return _Run(ports, command, project, plan, job, source, observer or TranslateObserver(),
                            clock=lambda: self._clock(), new_id=lambda: self._new_id(),
                            lease_duration=lambda: self._lease_duration, lease_renewal=lambda: self._lease_renewal,
                            append_provenance=lambda *args: self._append_provenance(*args),
                            append_unit_provenance=lambda *args: self._append_unit_provenance(*args)).execute()
        except ProjectError as error:
            return refused(JobFailureFamily.RESOURCE, _safe_code(error, "job_store_unavailable"))

    def _take_over(self, project_id: str, input_fingerprint: str) -> None:
        """Holding the guard proves that whoever held a live lease on this plan is gone, so the lease is released.

        A queued job keeps its lease until it expires, because it holds nothing and reconciliation leaves it alone.
        """
        ports = self._ports
        for snapshot in ports.jobs.list_jobs(project_id, states=_OWN_STATES):
            job = snapshot.job
            if job.request.input_fingerprint != input_fingerprint:
                continue
            if job.lease is not None and not snapshot.requires_reconciliation and job.state is not JobState.QUEUED:
                ports.jobs.release_lease(job.job_id, job.lease.owner_token)
            elif job.lease is None and job.state is JobState.QUEUED:
                # Its creator takes the lease under this same guard, so it died in between. No lease will ever expire here.
                self._settle_interrupted(job)
        self.reconcile(project_id)

    @contextmanager
    def _guards(self, fingerprints: tuple[str, ...]) -> Iterator[bool]:
        """Takes each once, because a second take of one guard by the same run would refuse itself."""
        if not fingerprints:
            yield True
            return
        with self._ports.runs.exclusive(fingerprints[0]) as alone:
            if not alone:
                yield False
                return
            with self._guards(fingerprints[1:]) as rest:
                yield rest

    def _continue(self, ticket: ContinuationTicket, request: JobRequest) -> TranslationJob | str:
        """The new job, or the code that says why not. Runs holding the guards of both plans."""
        jobs = self._ports.jobs
        parent = jobs.get(ticket.job_id).job
        if parent.request.command_version != request.command_version:
            return "runtime_mismatch"
        if parent.revision != ticket.expected_revision:
            return "stale_revision"
        if jobs.get_continuation(parent.job_id) is not None:
            return "continuation_exists"
        for fingerprint in dict.fromkeys((parent.request.input_fingerprint, request.input_fingerprint)):
            self._take_over(request.project_id, fingerprint)
        parent = jobs.get(parent.job_id).job
        if not parent.terminal:
            return "job_not_resumable"
        return jobs.create_continuation(parent.job_id, request)


def _safe_code(error: Exception, fallback: str) -> str:
    code = str(error)
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else fallback
