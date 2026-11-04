"""Translates one project's book as one durable job, for any consumer.

This is the application's whole answer to "translate this project": it checks the project and its
source revision, then owns the job from creation to its one terminal outcome, while the engine does
the book and the injected ports do everything concrete. It imports no framework, store or provider.

Dry run and preview are refused here on purpose. They need no project and create none, and identity
must never be made up to fit them, so they stay on the current path behind their own adapter.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Protocol
from uuid import uuid4

from .artifact import Artifact, ArtifactError, ArtifactRequest
from .domain import ChapterSelectionError, OutputPlan, TranslationOptions
from .format_adapter import FormatError
from .format_service import FormatPublication, FormatService, FormatSource
from .job import (
    MAX_LEASE_DURATION, JobError, JobLeaseError, JobRequest, JobState, JobTerminalSummary, TranslationJob,
    validate_job_identity,
)
from .job_events import (
    CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobFailure, JobFailureFamily, JobOutcome,
    JobProgress, JobWarning,
)
from .job_execution import DEFAULT_EVENT_LOG_CAPACITY, CancellationSource, InMemoryJobEventLog
from .job_service import JobRepository
from .project import ProjectError, ProjectManifest
from .provenance import (
    UNKNOWN, ActorKind, ConfigurationRef, ProducerIdentity, ProvenanceError, ProvenanceRequest, SourceKind,
)
from .translation_engine import (
    ENGINE_SAFE_POINTS, BookFacts, EngineObserver, EngineRequest, EngineResult, TextStats, TranslationEngine,
    TranslationResources,
)

TRANSLATE_COMMAND_TYPE = "translate_project"
TRANSLATE_COMMAND_VERSION = 1
OUTPUT_ROLE = "translated_output"
RECONSTRUCT_STAGE = "reconstruct"
RECONSTRUCT_STAGE_VERSION = 1
EPUB_MEDIA_TYPE = "application/epub+zip"
TRANSLATE_BEFORE_START = "translate_before_start"
INTERRUPTED = "interrupted"
INTERRUPTED_AFTER_PUBLICATION = "interrupted_after_publication"
PUBLICATION_UNCOMMITTED = "publication_uncommitted"
DEFAULT_LEASE_RENEWAL = timedelta(minutes=5)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)
# The states this use case leaves a job in. Any other nonterminal state belongs to someone else.
_OWN_STATES = (JobState.QUEUED, JobState.PREFLIGHT, JobState.RUNNING, JobState.CANCELLING, JobState.COMPLETED, JobState.VALIDATING)
# Room kept in the event log, so that progress can never crowd out the terminal outcome.
_RESERVED_EVENTS = 8


class TranslateCommandError(ValueError):
    """A bounded code. The message never holds a path or a language."""


@dataclass(frozen=True, repr=False)
class TranslateProjectCommand:
    project_id: str
    source_sha256: str
    options: TranslationOptions
    output: OutputPlan
    overwrite: bool = False
    adapter_id: str = "epub"
    media_type: str = EPUB_MEDIA_TYPE
    operation_id: str | None = None
    schema_version: int = TRANSLATE_COMMAND_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRANSLATE_COMMAND_VERSION:
            raise TranslateCommandError("unsupported_command_version")
        try:
            validate_job_identity(self.project_id)
            if self.operation_id is not None:
                validate_job_identity(self.operation_id)
        except JobError:
            raise TranslateCommandError("invalid_identity") from None
        if not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256):
            raise TranslateCommandError("invalid_source_revision")
        if type(self.options) is not TranslationOptions or type(self.output) is not OutputPlan:
            raise TranslateCommandError("invalid_command")
        if self.options.dry_run or self.output.dry_run:
            raise TranslateCommandError("dry_run_needs_no_project")
        if self.options.max_documents is not None:
            raise TranslateCommandError("preview_needs_no_project")
        if type(self.options.workers) is not int or self.options.workers < 1:
            raise TranslateCommandError("invalid_workers")

    def __repr__(self) -> str:
        return f"TranslateProjectCommand(project_id={self.project_id!r}, schema_version={self.schema_version})"


@dataclass(frozen=True, repr=False)
class ExecutionPlan:
    """Everything that decides the result. Changing any of it gives a different fingerprint."""

    project_id: str
    source_sha256: str
    input_fingerprint: str

    def __repr__(self) -> str:
        return f"ExecutionPlan(project_id={self.project_id!r})"


def resolve_plan(command: TranslateProjectCommand, project: ProjectManifest) -> ExecutionPlan:
    options = command.options
    memory = options.translation_memory
    inputs = {
        "command": [TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION], "source": command.source_sha256,
        "adapter": command.adapter_id, "media_type": command.media_type,
        "languages": [options.source, options.target], "fail_fast": options.fail_fast, "chunk_limit": options.chunk_limit,
        "workers": options.workers, "metadata": options.translate_metadata, "alt_text": options.translate_alt_text,
        "selection": None if options.chapter_selection is None else options.chapter_selection.source,
        "cache_only": options.cache_only, "require_full_cache": options.require_full_cache,
        "memory": None if memory is None else [memory.apply_threshold, memory.suggest_threshold, memory.max_candidates],
        "references": [project.profile_ref, project.provider_ref, project.policy_ref, project.glossary_ref, project.configuration_ref],
        "output": str(command.output.path), "overwrite": command.overwrite,
    }
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return ExecutionPlan(command.project_id, command.source_sha256, f"sha256:{digest}")


@dataclass(frozen=True, repr=False)
class OutputValidation:
    """``warnings`` is text for the report. Events and records only ever hold how many there were."""

    ok: bool
    warnings: tuple[str, ...] = ()
    document_count: int = 0

    def __repr__(self) -> str:
        return f"OutputValidation(ok={self.ok}, warnings={len(self.warnings)})"


class ProjectReader(Protocol):
    def load(self, project_id: str) -> ProjectManifest: ...


class TranslationRuntime(Protocol):
    """The language side: everything that knows a provider, a cache or a glossary."""

    def preflight(self) -> JobFailure | None: ...
    def facts(self) -> BookFacts: ...
    def resources(self) -> AbstractContextManager[TranslationResources]: ...
    def classify(self, error: Exception) -> JobFailure: ...


class OutputValidator(Protocol):
    """``staged`` is opaque here: it comes from the store and goes back to its adapter."""

    def validate(self, staged: object, on_progress: Callable[[int, int, str], None] | None) -> OutputValidation: ...


class OutputPublications(Protocol):
    def publication(self, request: ArtifactRequest, owner_token: str, on_staged: Callable[[object], None]) -> FormatPublication: ...
    def find_ready(self, request: ArtifactRequest) -> Artifact | None: ...
    def reconcile(self) -> None: ...


class ProvenanceWriter(Protocol):
    def append(self, request: ProvenanceRequest) -> object: ...


class RunGuard(Protocol):
    """Says, for as long as it is held, whether this is the only live run of a plan.

    It has to end with its process, however that process ends. A store that cannot tell raises ``ProjectError``.
    """

    def exclusive(self, input_fingerprint: str) -> AbstractContextManager[bool]: ...


@dataclass(frozen=True, repr=False)
class TranslateProjectPorts:
    projects: ProjectReader
    jobs: JobRepository
    provenance: ProvenanceWriter
    formats: FormatService
    publications: OutputPublications
    validator: OutputValidator
    source: FormatSource
    runtime: TranslationRuntime
    runs: RunGuard

    def __repr__(self) -> str:
        return "TranslateProjectPorts()"


@dataclass(frozen=True)
class TranslateObserver:
    """What a consumer watches. ``engine`` is the per-text detail, which events do not carry."""

    engine: EngineObserver = EngineObserver()
    on_event: Callable[[JobEvent], None] | None = None
    on_validation_progress: Callable[[int, int, str], None] | None = None


@dataclass(frozen=True, repr=False)
class TranslateProjectResult:
    """``state`` is what the job record says. It is None only when no job was created.

    ``raised`` is the exception behind a failure, for the presentation that chose this book. It is
    never stored, and it stays out of the repr, because its message may quote the book.
    """

    project_id: str
    job_id: str | None = None
    state: JobState | None = None
    outcome: JobOutcome | None = None
    failure: JobFailure | None = None
    artifact: Artifact | None = None
    engine: EngineResult = field(default_factory=EngineResult)
    validation: OutputValidation | None = None
    events: tuple[JobEvent, ...] = ()
    recorded: bool = True
    raised: Exception | None = None

    def __repr__(self) -> str:
        state = None if self.state is None else self.state.value
        return f"TranslateProjectResult(job_id={self.job_id!r}, state={state!r})"

    @property
    def output_ready(self) -> bool:
        return self.artifact is not None and self.recorded and self.state in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)


def _counters(stats: TextStats, chapters: int, validation: OutputValidation | None) -> tuple[tuple[str, int], ...]:
    return (("chapters", chapters), ("translated", stats.translated), ("from_cache", stats.from_cache),
            ("from_memory", stats.from_memory), ("missing", stats.missing), ("errors", len(stats.errors)),
            ("validation_warnings", 0 if validation is None else len(validation.warnings)))


class _LeaseKeeper:
    """Renews from its own thread, since one chapter or one blocked call can outlast any safe point.

    Losing the lease stops nothing by itself: the artifact store checks it, so a run that lost it can
    never publish. A store that is busy for a moment is simply tried again on the next round.
    """

    def __init__(self, renew: Callable[[], None], interval: timedelta) -> None:
        self._renew = renew
        self._interval = interval.total_seconds()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="ayvu-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._renew()
            except JobLeaseError:
                return
            except JobError:
                continue


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
        return CancellationSource((TRANSLATE_BEFORE_START, *ENGINE_SAFE_POINTS, *self._ports.formats.safe_points(adapter_id)))

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
        self._append_provenance(job.request.project_id, job.job_id, reason, artifact, None, None)
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
                           configuration_ref: str | None, operation_id: str | None) -> bool:
        references = () if configuration_ref is None else (ConfigurationRef("configuration", configuration_ref),)
        try:
            # The key allows one outcome per job, whichever run or reconciliation writes it first.
            self._ports.provenance.append(ProvenanceRequest(
                project_id, job_id, TRANSLATE_COMMAND_TYPE, ActorKind.SYSTEM, SourceKind.PIPELINE, _UNKNOWN_PRODUCER,
                outcome_code=reason, configuration_refs=references, artifacts=() if artifact is None else (artifact.artifact_id,),
                correlation_id=operation_id, idempotency_key=f"job:{job_id}:outcome"))
        except ProvenanceError:
            return False
        return True

    def execute(self, command: TranslateProjectCommand, *, cancellation: CancellationSource | None = None,
                observer: TranslateObserver | None = None) -> TranslateProjectResult:
        """Everything that can be refused is refused before a job exists or a provider is touched."""
        if type(command) is not TranslateProjectCommand:
            raise TranslateCommandError("invalid_command")

        def refused(family: JobFailureFamily, code: str) -> TranslateProjectResult:
            return TranslateProjectResult(command.project_id, failure=JobFailure(family, code))

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
        plan = resolve_plan(command, project)
        try:
            with ports.runs.exclusive(plan.input_fingerprint) as alone:
                if not alone:
                    return refused(JobFailureFamily.POLICY, "job_in_progress")
                try:
                    self._take_over(command.project_id, plan.input_fingerprint)
                    job = ports.jobs.create(JobRequest(command.project_id, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION,
                                                       plan.input_fingerprint, (OUTPUT_ROLE,)))
                    job = ports.jobs.acquire_lease(job.job_id, self._lease_duration)
                except (JobError, ArtifactError) as error:
                    return refused(JobFailureFamily.RESOURCE, _safe_code(error, "job_store_unavailable"))
                return _Run(self, ports, command, project, plan, job, source, observer or TranslateObserver()).execute()
        except ProjectError as error:
            return refused(JobFailureFamily.RESOURCE, _safe_code(error, "job_store_unavailable"))

    def _take_over(self, project_id: str, input_fingerprint: str) -> None:
        """Holding the guard proves that whoever held a live lease on this plan is gone, so the lease is released.

        A queued job keeps its lease until it expires, because it holds nothing and reconciliation leaves it alone.
        """
        ports = self._ports
        for snapshot in ports.jobs.list_jobs(project_id, states=_OWN_STATES):
            job = snapshot.job
            if (job.request.input_fingerprint == input_fingerprint and job.lease is not None
                    and not snapshot.requires_reconciliation and job.state is not JobState.QUEUED):
                ports.jobs.release_lease(job.job_id, job.lease.owner_token)
        self.reconcile(project_id)


def _safe_code(error: Exception, fallback: str) -> str:
    code = str(error)
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else fallback


class _Run:
    """One execution, from a job with a lease to its terminal outcome. Everything runs on one thread."""

    def __init__(self, use_case: TranslateProject, ports: TranslateProjectPorts, command: TranslateProjectCommand,
                 project: ProjectManifest, plan: ExecutionPlan, job: TranslationJob, cancellation: CancellationSource,
                 observer: TranslateObserver) -> None:
        self._use_case = use_case
        self._ports = ports
        self._command = command
        self._project = project
        self._plan = plan
        self._job = job
        self._cancellation = cancellation
        self._observer = observer
        self._token = job.lease.owner_token
        self._log = InMemoryJobEventLog(job.job_id, capacity=DEFAULT_EVENT_LOG_CAPACITY)
        self._sequence = 0
        self._correlation = JobCorrelation(command.project_id, command.operation_id)
        self._cancellation_requested_sent = False
        self._cancellation_acknowledged_sent = False
        self._engine = EngineResult()
        self._validation: OutputValidation | None = None
        self._artifact: Artifact | None = None
        self._raised: Exception | None = None
        self._failure: JobFailure | None = None
        self._outcome: JobOutcome | None = None
        self._recorded = True
        self._request = ArtifactRequest(command.project_id, job.job_id, OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION,
                                        plan.input_fingerprint, command.media_type)
        self._keeper = _LeaseKeeper(
            lambda: ports.jobs.renew_lease(job.job_id, self._token, use_case._lease_duration), use_case._lease_renewal)

    def execute(self) -> TranslateProjectResult:
        self._keeper.start()
        try:
            self._drive()
        except KeyboardInterrupt:
            self._interrupted()
            raise
        except ChapterSelectionError as error:
            self._raised = error
            self._attempt(lambda: self._fail(JobFailure(JobFailureFamily.INPUT, "no_chapters_matched")))
        except Exception as error:
            self._raised = error
            self._attempt(lambda: self._fail(self._classified(error)))
        finally:
            self._keeper.stop()
        return self._result()

    def _classified(self, error: Exception) -> JobFailure:
        try:
            return self._ports.runtime.classify(error)
        except Exception:
            return JobFailure(JobFailureFamily.INTERNAL, "unexpected_error")

    def _attempt(self, step: Callable[[], None]) -> None:
        """Past this point the job store itself may be what failed, and that has to be said, not raised."""
        try:
            step()
        except (JobError, ArtifactError):
            self._recorded = False

    def _drive(self) -> None:
        ports = self._ports
        if self._cancellation.token.checkpoint(TRANSLATE_BEFORE_START) is not None:
            self._terminal(JobState.CANCELLED, "cancelled")
            return
        self._move(JobState.PREFLIGHT)
        self._progress("preflight", "step", 0, 1)
        failure = ports.runtime.preflight()
        if failure is not None:
            self._fail(failure)
            return
        self._progress("preflight", "step", 1, 1)
        self._move(JobState.RUNNING)
        options = self._command.options
        publication = ports.publications.publication(self._request, self._token, self._validate_staged)
        with ports.runtime.resources() as resources:
            engine = TranslationEngine(ports.formats, resources.executor, pool=resources.pool, observer=self._engine_observer(),
                                       cancellation=self._cancellation.token)
            self._engine = engine.run(EngineRequest(
                self._command.adapter_id, ports.source, publication, self._command.project_id, ports.runtime.facts(),
                options.translate_metadata, options.translate_alt_text, options.chapter_selection, options.fail_fast,
                options.cache_only and options.require_full_cache, options.workers))
        self._conclude()

    def _engine_observer(self) -> EngineObserver:
        watching = self._observer.engine

        def on_chapter_done(index: int, total: int, name: str, stats: TextStats) -> None:
            self._progress("translating", "chapter", index, total)
            if watching.on_chapter_done:
                watching.on_chapter_done(index, total, name, stats)

        return EngineObserver(watching.on_chapter_start, on_chapter_done, watching.on_text_processed)

    def _validate_staged(self, staged: object) -> None:
        """Warnings keep the output, as they do today. The old file at the destination is still untouched."""
        def on_progress(index: int, total: int, name: str) -> None:
            self._progress("validating", "document", index, total)
            if self._observer.on_validation_progress:
                self._observer.on_validation_progress(index, total, name)

        try:
            self._validation = self._ports.validator.validate(staged, on_progress)
        except Exception:
            self._validation = OutputValidation(False)
            self._emit(JobWarning("validation_unavailable"))
        else:
            if self._validation.warnings or not self._validation.ok:
                self._emit(JobWarning("validation_warnings"))
        self._progress("publishing", "step", 0, 1)

    def _conclude(self) -> None:
        engine = self._engine
        if engine.failure is not None:
            self._fail(engine.failure)
        elif not engine.output_written:
            # A required cache had gaps: the current flow's refusal, and nothing was published.
            self._terminal(JobState.PARTIALLY_COMPLETED, "cache_incomplete")
        else:
            self._artifact = engine.artifact
            self._progress("publishing", "step", 1, 1)
            self._move(JobState.COMPLETED)
            self._move(JobState.VALIDATING)
            reason = self._partial_reason()
            self._terminal(JobState.VALIDATED if reason is None else JobState.PARTIALLY_COMPLETED, reason or "validated")

    def _partial_reason(self) -> str | None:
        stats = self._engine.stats
        if self._validation is not None and not (self._validation.ok and not self._validation.warnings):
            return "validation_warnings"
        if stats.errors:
            return "texts_failed"
        if stats.missing:
            return "texts_missing"
        return None

    def _cancel(self) -> None:
        if self._job.state is JobState.RUNNING:
            self._move(JobState.CANCELLING)
        self._terminal(JobState.CANCELLED, "cancelled")

    def _fail(self, failure: JobFailure) -> None:
        """A failed publication may still have reached the destination, so that is settled before anything is claimed."""
        # A worker can acknowledge a cancellation while another one fails, and the acknowledgement is a commitment.
        if self._cancellation.acknowledgement is not None or failure.family is JobFailureFamily.CANCELLED:
            self._cancel()
            return
        self._failure = failure
        ready = None
        if self._job.state is JobState.RUNNING:
            ready = self._settle_publication()
        if ready is not None:
            self._artifact = ready
            self._terminal(JobState.PARTIALLY_COMPLETED, PUBLICATION_UNCOMMITTED)
        else:
            self._terminal(JobState.FAILED, failure.code, failure)

    def _settle_publication(self) -> Artifact | None:
        # Reconciliation leaves a job with a live lease alone, and the lease is what says "publishing now".
        self._keeper.stop()
        try:
            self._ports.jobs.release_lease(self._job.job_id, self._token)
        except JobError:
            pass
        try:
            self._ports.publications.reconcile()
            return self._ports.publications.find_ready(self._request)
        except ArtifactError:
            return None

    def _interrupted(self) -> None:
        """Ctrl+C can land anywhere, including in the middle of publishing."""
        self._attempt(self._settle_interrupt)

    def _settle_interrupt(self) -> None:
        self._cancellation.request(INTERRUPTED, self._clock_now())
        ready = self._settle_publication() if self._job.state is JobState.RUNNING else None
        if ready is not None:
            self._artifact = ready
            self._terminal(JobState.PARTIALLY_COMPLETED, INTERRUPTED_AFTER_PUBLICATION)
        elif self._job.state in (JobState.COMPLETED, JobState.VALIDATING):
            if self._job.state is JobState.COMPLETED:
                self._move(JobState.VALIDATING)
            self._terminal(JobState.PARTIALLY_COMPLETED, INTERRUPTED_AFTER_PUBLICATION)
        elif self._job.state is JobState.PREFLIGHT:
            self._failure = JobFailure(JobFailureFamily.RESOURCE, INTERRUPTED)
            self._terminal(JobState.FAILED, INTERRUPTED, self._failure)
        else:
            self._cancel()

    def _clock_now(self) -> datetime:
        return self._use_case._clock()

    def _move(self, target: JobState) -> None:
        self._job = self._ports.jobs.transition(self._job.job_id, target, expected_revision=self._job.revision)

    def _terminal(self, state: JobState, reason: str, failure: JobFailure | None = None) -> None:
        recorded = self._use_case._append_provenance(self._command.project_id, self._job.job_id, reason, self._artifact,
                                                     self._project.configuration_ref, self._command.operation_id)
        if not recorded and state is JobState.VALIDATED:
            # The output is real and the lineage is not there, so it cannot be called fully done.
            state, reason = JobState.PARTIALLY_COMPLETED, "provenance_failed"
        summary = JobTerminalSummary(reason, _counters(self._engine.stats, self._engine.chapters_processed, self._validation))
        outcome = JobOutcome(state, summary, failure)
        self._cancellation.settle(outcome)
        self._keeper.stop()
        self._job = self._ports.jobs.transition(self._job.job_id, state, expected_revision=self._job.revision, summary=summary)
        self._outcome = outcome
        self._send_cancellation_events()
        self._emit(outcome)

    def _send_cancellation_events(self) -> None:
        request = self._cancellation.requested
        if request is not None and not self._cancellation_requested_sent:
            self._cancellation_requested_sent = True
            self._emit(request)
        acknowledgement = self._cancellation.acknowledgement
        if acknowledgement is not None and not self._cancellation_acknowledged_sent:
            self._cancellation_acknowledged_sent = True
            self._emit(acknowledgement)

    def _progress(self, phase: str, unit: str, completed: int, total: int) -> None:
        if self._sequence < DEFAULT_EVENT_LOG_CAPACITY - _RESERVED_EVENTS:
            self._emit(JobProgress(phase, unit, completed, total))

    def _emit(self, payload: JobProgress | JobWarning | CancellationRequest | CancellationAcknowledgement | JobOutcome) -> None:
        """A consumer that fails can only hurt itself. It cannot change how the job ends."""
        event = JobEvent(self._use_case._new_id(), self._job.job_id, self._sequence + 1, self._clock_now(), self._correlation, payload)
        try:
            self._log.emit(event)
        except JobError:
            return
        self._sequence += 1
        if self._observer.on_event is not None:
            try:
                self._observer.on_event(event)
            except Exception:
                pass

    def _result(self) -> TranslateProjectResult:
        if not self._recorded:
            # Whatever went wrong, the record is what counts, and it may say something else by now.
            try:
                self._job = self._ports.jobs.get(self._job.job_id).job
            except JobError:
                pass
        artifact = self._artifact if self._recorded and self._job.terminal else None
        return TranslateProjectResult(
            self._command.project_id, self._job.job_id, self._job.state, self._outcome, self._failure or self._engine.failure, artifact,
            self._engine, self._validation, self._log.replay(), self._recorded and self._job.terminal, self._raised)
