"""One active project translation through its terminal outcome."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from ..artifacts import Artifact, ArtifactError, ArtifactRequest
from ..jobs import JobState, JobTerminalSummary, TranslationJob
from ..jobs import (
    CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobOutcome, JobProgress,
    JobEventAcceptance,
)
from ..jobs import DEFAULT_EVENT_LOG_CAPACITY, CancellationSource, InMemoryJobEventLog
from ..projects import ProjectManifest
from ..provenance import ActorKind, ProvenanceRequest, SourceKind
from .engine import EngineObserver, EngineRequest, EngineResult, TextStats, TranslationEngine
from ..kernel import ChapterSelectionError, JobError, JobFailure, JobFailureFamily, JobWarning
from .project_contracts import (
    ExecutionPlan, INTERRUPTED, INTERRUPTED_AFTER_PUBLICATION, OUTPUT_ROLE, OutputValidation,
    PUBLICATION_UNCOMMITTED, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION, RuntimeContext,
    TRANSLATE_BEFORE_START, TRANSLATE_COMMAND_TYPE, TranslateObserver, TranslateProjectCommand,
    TranslateProjectInterrupted, TranslateProjectPorts, TranslateProjectResult,
)
from .project_lease import _LeaseKeeper


# Matches the translation document's own bound on how far back a continuation looks.
MAX_CONTINUATION_CHAIN = 32


# Room kept in the event log, so that progress can never crowd out the terminal outcome.
_RESERVED_EVENTS = 8


def _counters(stats: TextStats, chapters: int, validation: OutputValidation | None) -> tuple[tuple[str, int], ...]:
    return (("chapters", chapters), ("translated", stats.translated), ("from_cache", stats.from_cache),
            ("from_memory", stats.from_memory), ("missing", stats.missing), ("errors", len(stats.errors)),
            ("validation_warnings", 0 if validation is None else len(validation.warnings)))


class _Run:
    """One execution, from a job with a lease to its terminal outcome. Everything runs on one thread."""

    def __init__(self, ports: TranslateProjectPorts, command: TranslateProjectCommand,
                 project: ProjectManifest, plan: ExecutionPlan, job: TranslationJob, cancellation: CancellationSource,
                 observer: TranslateObserver, *, clock: Callable[[], datetime], new_id: Callable[[], str],
                 lease_duration: Callable[[], timedelta], lease_renewal: Callable[[], timedelta],
                 append_provenance: Callable[..., bool], append_unit_provenance: Callable[..., bool]) -> None:
        self._clock = clock
        self._new_id = new_id
        self._append_provenance = append_provenance
        self._append_unit_provenance = append_unit_provenance
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
            lambda: ports.jobs.renew_lease(job.job_id, self._token, lease_duration()), lease_renewal())

    def execute(self) -> TranslateProjectResult:
        self._keeper.start()
        try:
            self._drive()
        except KeyboardInterrupt:
            self._interrupted()
            raise TranslateProjectInterrupted(self._result()) from None
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
        if self._command.producer is not None:
            ports.provenance.append(ProvenanceRequest(
                self._command.project_id, self._job.job_id, "execution_identity", ActorKind.SYSTEM, SourceKind.PIPELINE,
                self._command.producer, outcome_code="execution_planned", idempotency_key=f"execution:{self._job.job_id}"))
        self._progress("preflight", "step", 0, 1)
        failure = ports.runtime.preflight()
        if failure is not None:
            self._fail(failure)
            return
        self._progress("preflight", "step", 1, 1)
        self._move(JobState.RUNNING)
        options = self._command.options
        publication = ports.publications.publication(self._request, self._token, self._validate_staged)
        context = RuntimeContext(self._job, self._cancellation, self,
                                 current_job=lambda: ports.jobs.get(self._job.job_id).job,
                                 reusable_jobs=self._continuation_chain())
        with ports.runtime.resources(context) as resources:
            engine = TranslationEngine(ports.formats, resources.executor, pool=resources.pool, observer=self._engine_observer(),
                                       cancellation=self._cancellation.token, prepare_document=resources.prepare_document)
            self._engine = engine.run(EngineRequest(
                self._command.adapter_id, ports.source, publication, self._command.project_id, ports.runtime.facts(),
                options.translate_metadata, options.translate_alt_text, options.chapter_selection, options.fail_fast,
                options.cache_only and options.require_full_cache, options.workers))
        self._conclude()

    def _continuation_chain(self) -> tuple[str, ...]:
        """The jobs this one continues, nearest first. Each ended before its continuation existed and stays as it ended."""
        chain: list[str] = []
        job = self._job
        while job.continuation_of is not None and len(chain) < MAX_CONTINUATION_CHAIN:
            try:
                parent = self._ports.jobs.get(job.continuation_of).job
            except JobError:
                break
            if (parent.request.project_id, parent.request.command_type, parent.request.command_version) != (
                    self._job.request.project_id, TRANSLATE_COMMAND_TYPE, self._job.request.command_version):
                break
            chain.append(parent.job_id)
            job = parent
        return tuple(chain)

    def emit(self, event: JobEvent) -> JobEventAcceptance:
        """Forward warnings. The engine owns chapter progress and settlement owns cancellation.

        Document-local unit progress cannot be interleaved with chapter progress: doing so
        would return to a completed phase. Neither local sequence nor correlation is reused.
        """
        if isinstance(event.payload, JobWarning):
            self._emit(event.payload)
        return JobEventAcceptance.APPLIED

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
        if self._command.accepted_output is not None:
            self._command.accepted_output.publication_guard()

    def _conclude(self) -> None:
        engine = self._engine
        if engine.failure is not None:
            self._fail(engine.failure)
        elif not engine.output_written:
            # A required cache had gaps: the current flow's refusal, and nothing was published.
            self._terminal(JobState.PARTIALLY_COMPLETED, "cache_incomplete")
        else:
            self._artifact = engine.artifact
            if self._command.accepted_output is not None:
                self._command.accepted_output.mark_published()
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
        return self._clock()

    def _move(self, target: JobState) -> None:
        self._job = self._ports.jobs.transition(self._job.job_id, target, expected_revision=self._job.revision)

    def _terminal(self, state: JobState, reason: str, failure: JobFailure | None = None) -> None:
        recorded = self._append_provenance(self._command.project_id, self._job.job_id, reason, self._artifact,
                                                     self._project.configuration_ref, self._command.operation_id,
                                                     self._command.producer)
        if self._artifact is not None and state in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED):
            recorded = self._append_unit_provenance(self._command.project_id, self._job.job_id,
                                                               self._artifact, self._engine,
                                                               self._command.output.path, self._command.producer) and recorded
        self._recorded = self._recorded and recorded
        if not recorded and state in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED):
            # Review export requires complete machine lineage, including for partial output.
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
        event = JobEvent(self._new_id(), self._job.job_id, self._sequence + 1, self._clock_now(), self._correlation, payload)
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
            self._engine, self._validation, self._log.replay(), self._recorded and self._job.terminal, self._raised,
            self._command.output.path, self._project.revision,
            self._job.job_id if self._engine.failure is None and self._recorded
            and self._job.state in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED) else None)
