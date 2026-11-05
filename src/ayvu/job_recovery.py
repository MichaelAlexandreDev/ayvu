"""Lists, inspects and resumes interrupted translations, for any consumer.

Listing and inspecting only read. Resuming never reopens a job that ended: the old job gets its one
terminal outcome if it has none, and a new job links to it. Whether a stage can be reused is decided
here from evidence, and today there is none in production, because the v1 cache and the lineage cannot
say what produced a text. So every resume is a full re-execution, and that is what a result says.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from .job import JobError, JobState, TranslationJob
from .job_execution import CancellationSource, CancellationToken
from .job_service import JobSnapshot
from .translate_project import (
    INTERRUPTED, INTERRUPTED_AFTER_PUBLICATION, OWN_JOB_STATES, PUBLICATION_UNCOMMITTED, ContinuationTicket, TranslateProjectResult,
)

RECOVERY_BETWEEN_CHUNKS = "recovery_between_chunks"
RECOVERY_SAFE_POINTS = (RECOVERY_BETWEEN_CHUNKS,)
REUSE_DIMENSIONS = ("input", "configuration", "provider", "route", "capability", "format", "locator", "pipeline", "stage")
MAX_LISTED_ENTRIES = 1024
# A job that ended this way did not finish its work. One that merely has warnings or gaps did.
_INTERRUPTED_REASONS = frozenset({INTERRUPTED, INTERRUPTED_AFTER_PUBLICATION, PUBLICATION_UNCOMMITTED, "cancelled", "lease_expired"})


class CompatibilityStatus(StrEnum):
    DURABLE = "durable"
    LEGACY_ONLY = "legacy_only"
    UNSUPPORTED = "unsupported"
    JOB_ONLY = "job_only"


class RecoveryReason(StrEnum):
    RECOVERABLE_INTERRUPTION = "recoverable_interruption"
    ACTIVE_LEASE = "active_lease"
    LEASE_OWNER_GONE = "lease_owner_gone"
    EXPIRED_LEASE = "expired_lease"
    TERMINAL_JOB = "terminal_job"
    JOB_STATE_NOT_RESUMABLE = "job_state_not_resumable"
    SOURCE_CHANGED = "source_changed"
    SOURCE_MISSING = "source_missing"
    SOURCE_UNREADABLE = "source_unreadable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_STATE_FILE = "invalid_state_file"
    MISSING_CONFIGURATION = "missing_configuration"
    INCOMPATIBLE_PLAN = "incompatible_plan"
    PROJECT_UNAVAILABLE = "project_unavailable"
    INVOCATION_UNAVAILABLE = "invocation_unavailable"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_PARTIAL = "artifact_partial"
    ARTIFACT_CORRUPT = "artifact_corrupt"
    PROVENANCE_UNPROVEN = "provenance_unproven"
    DESTINATION_CONFLICT = "destination_conflict"
    POLICY_DENIED = "policy_denied"
    LEGACY_ONLY = "legacy_only"


class RequiredDecision(StrEnum):
    """What a person has to accept before the resume starts."""

    FULL_REEXECUTION = "full_reexecution"
    REPLACE_DESTINATION = "replace_destination"


class ResumeStage(StrEnum):
    TRANSLATE = "translate"
    RECONSTRUCT = "reconstruct"


_STAGE_ORDER = (ResumeStage.TRANSLATE, ResumeStage.RECONSTRUCT)


class ArtifactIntegrity(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    NOT_READY = "not_ready"
    # A record says it is ready, and nobody read the bytes. That is never enough to reuse it.
    UNCHECKED = "unchecked"


class SourceState(StrEnum):
    SAME = "same"
    CHANGED = "changed"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    # The file is there, and nothing recorded what it should hold.
    UNKNOWN = "unknown"


class ResumeStatus(StrEnum):
    STARTED = "started"
    LEGACY_REEXECUTION = "legacy_reexecution"
    ALREADY_REQUESTED = "already_requested"
    REFUSED = "refused"


class RecoveryCancelled(Exception):
    """Raised through a long read when cancellation is observed."""


class RecoveryError(ValueError):
    """A bounded code. The message never holds a path or a value from a file."""


# ---------- Reuse: pure rules over evidence.

@dataclass(frozen=True, repr=False)
class StageEvidence:
    """What is known about one stage's artifact. A dimension nobody recorded is None, which proves nothing."""

    stage: ResumeStage
    integrity: ArtifactIntegrity
    owned: bool = True
    schema_valid: bool = True
    expected: tuple[tuple[str, str | None], ...] = ()
    produced: tuple[tuple[str, str | None], ...] = ()

    def __repr__(self) -> str:
        return f"StageEvidence(stage={self.stage.value!r}, integrity={self.integrity.value!r})"


@dataclass(frozen=True)
class StageSelection:
    """``resume_from`` is the earliest stage that must run again. None means every stage was proved."""

    resume_from: ResumeStage | None
    reusable: tuple[ResumeStage, ...] = ()
    reason: RecoveryReason | None = None
    changed: tuple[str, ...] = ()
    not_provable: tuple[str, ...] = ()


_INTEGRITY_REASONS = {
    ArtifactIntegrity.MISSING: RecoveryReason.ARTIFACT_MISSING,
    ArtifactIntegrity.PARTIAL: RecoveryReason.ARTIFACT_PARTIAL,
    ArtifactIntegrity.CORRUPT: RecoveryReason.ARTIFACT_CORRUPT,
    ArtifactIntegrity.NOT_READY: RecoveryReason.ARTIFACT_MISSING,
}


def select_resume_stage(evidence: tuple[StageEvidence, ...]) -> StageSelection:
    """A stage is reusable only if its artifact is verified and owned, and every dimension is proved equal."""
    by_stage = {item.stage: item for item in evidence}
    reusable: list[ResumeStage] = []
    for stage in _STAGE_ORDER:
        item = by_stage.get(stage)
        if item is None:
            return StageSelection(stage, tuple(reusable), RecoveryReason.ARTIFACT_MISSING)
        if item.integrity is ArtifactIntegrity.UNCHECKED:
            return StageSelection(stage, tuple(reusable), RecoveryReason.PROVENANCE_UNPROVEN, not_provable=REUSE_DIMENSIONS)
        if item.integrity is not ArtifactIntegrity.VERIFIED:
            return StageSelection(stage, tuple(reusable), _INTEGRITY_REASONS[item.integrity])
        if not item.owned or not item.schema_valid:
            return StageSelection(stage, tuple(reusable), RecoveryReason.ARTIFACT_CORRUPT)
        expected, produced = dict(item.expected), dict(item.produced)
        changed = tuple(name for name in REUSE_DIMENSIONS
                        if expected.get(name) is not None and produced.get(name) is not None and expected[name] != produced[name])
        if changed:
            return StageSelection(stage, tuple(reusable), RecoveryReason.INCOMPATIBLE_PLAN, changed=changed)
        unproved = tuple(name for name in REUSE_DIMENSIONS if expected.get(name) is None or produced.get(name) is None)
        if unproved:
            return StageSelection(stage, tuple(reusable), RecoveryReason.PROVENANCE_UNPROVEN, not_provable=unproved)
        reusable.append(stage)
    return StageSelection(None, tuple(reusable))


# ---------- What the use cases read, through a port.

@dataclass(frozen=True, repr=False)
class RecoveryProgress:
    total_chapters: int | None = None
    completed_chapters: int = 0
    failed_chapters: int = 0
    failed_segments: int = 0

    def __repr__(self) -> str:
        return f"RecoveryProgress(completed={self.completed_chapters}, total={self.total_chapters})"


@dataclass(frozen=True, repr=False)
class InvocationView:
    """One recorded way of running a translation. It proves nothing by itself.

    ``fingerprints`` are the plans it could have run under, for matching against a job. The project is the one
    whose identity it recomputes to. ``problem`` says why there is no project, or why the file could not be used.
    """

    reference: str
    running: bool
    progress: RecoveryProgress = RecoveryProgress()
    project_id: str | None = None
    fingerprints: tuple[str, ...] = ()
    problem: RecoveryReason | None = None
    unusable: bool = False

    def __repr__(self) -> str:
        return f"InvocationView(reference={self.reference!r})"


@dataclass(frozen=True, repr=False)
class RecoveryScan:
    invocations: tuple[InvocationView, ...] = ()
    project_ids: tuple[str, ...] = ()
    failures: int = 0

    def __repr__(self) -> str:
        return f"RecoveryScan(invocations={len(self.invocations)}, projects={len(self.project_ids)})"


@dataclass(frozen=True, repr=False)
class OutputEvidence:
    """``integrity`` is about a recorded output and is None without a record. ``conflict`` is a file with no record."""

    integrity: ArtifactIntegrity | None = None
    conflict: bool = False
    exists: bool = False

    def __repr__(self) -> str:
        return f"OutputEvidence(integrity={self.integrity}, conflict={self.conflict})"


class RecoveryEvidence(Protocol):
    """Everything here reads. Nothing creates, locks or changes a file, and nothing reaches a provider."""

    def scan(self) -> RecoveryScan: ...
    def jobs(self, project_id: str) -> tuple[JobSnapshot, ...]: ...
    def run_active(self, project_id: str, input_fingerprint: str) -> bool: ...
    def source(self, reference: str, project_id: str | None, token: CancellationToken | None) -> SourceState: ...
    def policy_refusal(self, reference: str, project_id: str | None) -> str | None: ...
    def output(self, reference: str, project_id: str | None, job_id: str | None, token: CancellationToken | None) -> OutputEvidence: ...
    def stages(self, project_id: str, job_id: str) -> tuple[StageEvidence, ...]: ...
    def producer_identified(self, project_id: str, job_id: str) -> bool: ...


# ---------- Results.

@dataclass(frozen=True, repr=False)
class RecoveryEntry:
    """One interrupted translation, in terms any consumer can show. It holds no path, text or address."""

    reference: str
    compatibility: CompatibilityStatus
    reasons: tuple[RecoveryReason, ...]
    resumable: bool
    progress: RecoveryProgress = RecoveryProgress()
    project_id: str | None = None
    job_id: str | None = None
    job_state: JobState | None = None
    expected_revision: int | None = None

    def __repr__(self) -> str:
        return f"RecoveryEntry(reference={self.reference!r}, compatibility={self.compatibility.value!r})"


@dataclass(frozen=True, repr=False)
class InterruptedJobs:
    entries: tuple[RecoveryEntry, ...] = ()
    unreadable_projects: int = 0

    def __repr__(self) -> str:
        return f"InterruptedJobs(entries={len(self.entries)}, unreadable_projects={self.unreadable_projects})"


@dataclass(frozen=True, repr=False)
class JobRecoverability:
    """The answer to "may this resume, and what would that mean".

    ``provider_calls_may_repeat`` is always true while a stage has to run again: a call whose completion was
    never recorded is made again, and a cache hit proves nothing about what produced the text it holds.
    """

    entry: RecoveryEntry
    required_decisions: tuple[RequiredDecision, ...] = ()
    selection: StageSelection | None = None
    provider_calls_may_repeat: bool = True
    cancelled: bool = False

    def __repr__(self) -> str:
        return f"JobRecoverability(entry={self.entry!r}, cancelled={self.cancelled})"


@dataclass(frozen=True, repr=False)
class ResumeRequest:
    """``job_id`` and ``expected_revision`` are what the caller saw. ``accepted`` are the decisions it took."""

    reference: str
    job_id: str | None = None
    project_id: str | None = None
    expected_revision: int | None = None
    accepted: tuple[RequiredDecision, ...] = ()
    idempotency_key: str | None = None

    def __repr__(self) -> str:
        return f"ResumeRequest(reference={self.reference!r}, job_id={self.job_id!r})"


@dataclass(frozen=True, repr=False)
class ResumeResult:
    status: ResumeStatus
    reference: str
    reasons: tuple[RecoveryReason, ...] = ()
    code: str | None = None
    job_id: str | None = None
    continuation_of: str | None = None
    result: TranslateProjectResult | None = None
    inspection: JobRecoverability | None = None

    def __repr__(self) -> str:
        return f"ResumeResult(status={self.status.value!r}, job_id={self.job_id!r})"


class ResumeRunner(Protocol):
    """Runs the book. The durable path passes the ticket to ``TranslateProject``, and the legacy path is the current flow."""

    def run_durable(self, reference: str, ticket: ContinuationTicket, operation_id: str) -> TranslateProjectResult: ...
    def run_legacy(self, reference: str) -> None: ...


# ---------- Classification, shared by the three use cases.

def classify_job(snapshot: JobSnapshot, run_active: bool) -> tuple[tuple[RecoveryReason, ...], bool]:
    """The reasons, and whether a resume could go ahead as far as the job's own state goes."""
    job = snapshot.job
    if job.terminal:
        ended_by_interruption = job.state is JobState.FAILED or job.terminal_summary.reason_code in _INTERRUPTED_REASONS
        return ((RecoveryReason.TERMINAL_JOB, RecoveryReason.RECOVERABLE_INTERRUPTION) if ended_by_interruption
                else (RecoveryReason.TERMINAL_JOB,)), True
    if job.state not in OWN_JOB_STATES:
        return (RecoveryReason.JOB_STATE_NOT_RESUMABLE,), False
    if run_active:
        return (RecoveryReason.ACTIVE_LEASE,), False
    if snapshot.requires_reconciliation:
        return (RecoveryReason.EXPIRED_LEASE, RecoveryReason.RECOVERABLE_INTERRUPTION), True
    if job.lease is None:
        return (RecoveryReason.RECOVERABLE_INTERRUPTION,), True
    if job.state is JobState.QUEUED:
        # A queued job holds nothing, and it waits for its lease to expire rather than being taken over.
        return (RecoveryReason.ACTIVE_LEASE,), False
    return (RecoveryReason.LEASE_OWNER_GONE, RecoveryReason.RECOVERABLE_INTERRUPTION), True


def _tips(snapshots: tuple[JobSnapshot, ...]) -> tuple[JobSnapshot, ...]:
    """The last job of each chain, newest first."""
    continued = {item.job.continuation_of for item in snapshots}
    return tuple(item for item in snapshots if item.job.job_id not in continued)


def _is_interrupted(job: TranslationJob) -> bool:
    return not job.terminal or job.state is JobState.FAILED or job.terminal_summary.reason_code in _INTERRUPTED_REASONS


def _job_reference(project_id: str, job_id: str) -> str:
    return f"job:{project_id}:{job_id}"


class _Evaluation:
    """One consistent read of what exists, for one call."""

    def __init__(self, evidence: RecoveryEvidence) -> None:
        self._evidence = evidence
        self._scan = evidence.scan()
        self._jobs: dict[str, tuple[JobSnapshot, ...] | RecoveryReason] = {}

    @property
    def unreadable_projects(self) -> int:
        return self._scan.failures

    def _project_jobs(self, project_id: str) -> tuple[JobSnapshot, ...] | RecoveryReason:
        if project_id not in self._jobs:
            try:
                self._jobs[project_id] = self._evidence.jobs(project_id)
            except JobError as error:
                self._jobs[project_id] = (RecoveryReason.UNSUPPORTED_SCHEMA if str(error) == "version_gap"
                                          else RecoveryReason.PROJECT_UNAVAILABLE)
        return self._jobs[project_id]

    def _durable(self, reference: str, progress: RecoveryProgress, snapshot: JobSnapshot, compatibility: CompatibilityStatus) -> RecoveryEntry:
        job = snapshot.job
        active = False if job.terminal else self._evidence.run_active(job.request.project_id, job.request.input_fingerprint)
        reasons, resumable = classify_job(snapshot, active)
        if compatibility is CompatibilityStatus.JOB_ONLY:
            # Nothing records the address, the cache or the output, so there is nothing to run it again with.
            reasons, resumable = (*reasons, RecoveryReason.INVOCATION_UNAVAILABLE), False
        return RecoveryEntry(reference, compatibility, reasons, resumable, progress, job.request.project_id, job.job_id, job.state, job.revision)

    def _invocation_entry(self, view: InvocationView) -> RecoveryEntry:
        if view.unusable:
            return RecoveryEntry(view.reference, CompatibilityStatus.UNSUPPORTED, (view.problem or RecoveryReason.INVALID_STATE_FILE,), False)
        legacy = RecoveryEntry(view.reference, CompatibilityStatus.LEGACY_ONLY,
                               tuple(dict.fromkeys(item for item in (RecoveryReason.LEGACY_ONLY, view.problem) if item is not None)),
                               True, view.progress, view.project_id)
        if view.project_id is None:
            return legacy
        snapshots = self._project_jobs(view.project_id)
        if isinstance(snapshots, RecoveryReason):
            return RecoveryEntry(view.reference, CompatibilityStatus.LEGACY_ONLY, (RecoveryReason.LEGACY_ONLY, snapshots), True, view.progress, view.project_id)
        for snapshot in _tips(snapshots):
            if snapshot.job.request.input_fingerprint in view.fingerprints:
                return self._durable(view.reference, view.progress, snapshot, CompatibilityStatus.DURABLE)
        return legacy

    def entries(self) -> tuple[RecoveryEntry, ...]:
        found: list[RecoveryEntry] = []
        for view in self._scan.invocations:
            if view.running or view.unusable:
                found.append(self._invocation_entry(view))
        linked = {entry.job_id for entry in found if entry.job_id is not None}
        for project_id in self._scan.project_ids:
            snapshots = self._project_jobs(project_id)
            if isinstance(snapshots, RecoveryReason):
                found.append(RecoveryEntry(f"project:{project_id}", CompatibilityStatus.UNSUPPORTED, (snapshots,), False, project_id=project_id))
                continue
            plans: set[str] = set()
            for snapshot in _tips(snapshots):
                fingerprint = snapshot.job.request.input_fingerprint
                # Only the newest job of a plan counts: a later run of the same plan already answered for it.
                if fingerprint in plans:
                    continue
                plans.add(fingerprint)
                if snapshot.job.job_id not in linked and _is_interrupted(snapshot.job):
                    found.append(self._durable(_job_reference(project_id, snapshot.job.job_id), RecoveryProgress(), snapshot, CompatibilityStatus.JOB_ONLY))
        return tuple(found[:MAX_LISTED_ENTRIES])

    def entry(self, reference: str) -> RecoveryEntry | None:
        for candidate in self.entries():
            if candidate.reference == reference:
                return candidate
        return None

    def tip_of(self, project_id: str, job_id: str) -> str | None:
        """The last job of the chain that starts at ``job_id``, or None if nothing continues it."""
        snapshots = self._project_jobs(project_id)
        if isinstance(snapshots, RecoveryReason):
            return None
        following = {item.job.continuation_of: item.job.job_id for item in snapshots if item.job.continuation_of is not None}
        if job_id not in following:
            return None
        current = job_id
        while current in following:
            current = following[current]
        return current


def _integrity_reason(integrity: ArtifactIntegrity | None) -> RecoveryReason | None:
    return None if integrity is None or integrity is ArtifactIntegrity.VERIFIED else _INTEGRITY_REASONS[integrity]


_BLOCKING_SOURCE = {
    SourceState.CHANGED: RecoveryReason.SOURCE_CHANGED,
    SourceState.MISSING: RecoveryReason.SOURCE_MISSING,
    SourceState.UNREADABLE: RecoveryReason.SOURCE_UNREADABLE,
}


class ListInterruptedJobs:
    def __init__(self, evidence: RecoveryEvidence) -> None:
        self._evidence = evidence

    def __repr__(self) -> str:
        return "ListInterruptedJobs()"

    def execute(self) -> InterruptedJobs:
        """Reads what is recorded and hashes no book. ``InspectJobRecoverability`` is the one that verifies."""
        evaluation = _Evaluation(self._evidence)
        return InterruptedJobs(evaluation.entries(), evaluation.unreadable_projects)


class InspectJobRecoverability:
    def __init__(self, evidence: RecoveryEvidence) -> None:
        self._evidence = evidence

    def __repr__(self) -> str:
        return "InspectJobRecoverability()"

    def cancellation(self) -> CancellationSource:
        return CancellationSource(RECOVERY_SAFE_POINTS)

    def execute(self, reference: str, *, cancellation: CancellationSource | None = None) -> JobRecoverability | None:
        """None if nothing goes by that reference. It reads the source and the output through, which can take a while."""
        entry = _Evaluation(self._evidence).entry(reference)
        return None if entry is None else self.of(entry, cancellation)

    def of(self, entry: RecoveryEntry, cancellation: CancellationSource | None = None) -> JobRecoverability:
        token = None if cancellation is None else cancellation.token
        if entry.compatibility is CompatibilityStatus.UNSUPPORTED:
            return JobRecoverability(entry, provider_calls_may_repeat=False)
        try:
            return self._verified(entry, token)
        except RecoveryCancelled:
            return JobRecoverability(entry, cancelled=True)

    def _verified(self, entry: RecoveryEntry, token: CancellationToken | None) -> JobRecoverability:
        evidence = self._evidence
        durable = entry.compatibility is CompatibilityStatus.DURABLE
        reasons = list(entry.reasons)
        blocked = False
        decisions = [RequiredDecision.FULL_REEXECUTION]

        if entry.compatibility is not CompatibilityStatus.JOB_ONLY:
            state = evidence.source(entry.reference, entry.project_id, token)
            if state in _BLOCKING_SOURCE:
                reasons.append(_BLOCKING_SOURCE[state])
                # With nothing durable at stake, a book that is gone stays the current flow's to report, in its own words.
                blocked = durable or state is SourceState.CHANGED
            elif evidence.policy_refusal(entry.reference, entry.project_id) is not None:
                reasons.append(RecoveryReason.POLICY_DENIED)
                # The legacy path has no job to protect, and the current flow refuses this same book before any translator.
                blocked = durable
            output = evidence.output(entry.reference, entry.project_id, entry.job_id, token)
            integrity = _integrity_reason(output.integrity)
            if integrity is not None:
                reasons.append(integrity)
            if output.conflict:
                reasons.append(RecoveryReason.DESTINATION_CONFLICT)
            if output.exists:
                decisions.append(RequiredDecision.REPLACE_DESTINATION)

        selection = None
        if entry.job_id is not None and entry.project_id is not None:
            selection = select_resume_stage(evidence.stages(entry.project_id, entry.job_id))
            if not evidence.producer_identified(entry.project_id, entry.job_id):
                reasons.append(RecoveryReason.PROVENANCE_UNPROVEN)
        verified = RecoveryEntry(entry.reference, entry.compatibility, tuple(dict.fromkeys(reasons)), entry.resumable and not blocked,
                                 entry.progress, entry.project_id, entry.job_id, entry.job_state, entry.expected_revision)
        return JobRecoverability(verified, tuple(decisions) if verified.resumable else (), selection)


class RequestJobResume:
    def __init__(self, evidence: RecoveryEvidence, *, new_id=lambda: str(uuid4())) -> None:
        self._evidence = evidence
        self._inspect = InspectJobRecoverability(evidence)
        self._new_id = new_id

    def __repr__(self) -> str:
        return "RequestJobResume()"

    def execute(self, request: ResumeRequest, runner: ResumeRunner) -> ResumeResult:
        """Whatever a runner raises, such as Ctrl+C, passes through. The job records it, and this adds nothing."""
        evaluation = _Evaluation(self._evidence)
        if request.project_id is not None and request.job_id is not None:
            # The chain, not the file, says whether this was already asked: the file may have finished by now.
            tip = evaluation.tip_of(request.project_id, request.job_id)
            if tip is not None:
                return ResumeResult(ResumeStatus.ALREADY_REQUESTED, request.reference, job_id=tip, continuation_of=request.job_id)
        entry = evaluation.entry(request.reference)
        if entry is None:
            return ResumeResult(ResumeStatus.REFUSED, request.reference, code="not_found")
        inspection = self._inspect.of(entry)

        def refused(code: str, *reasons: RecoveryReason) -> ResumeResult:
            return ResumeResult(ResumeStatus.REFUSED, request.reference, reasons or inspection.entry.reasons, code,
                                job_id=entry.job_id, inspection=inspection)

        if not inspection.entry.resumable:
            return refused(_refusal_code(inspection.entry.reasons))
        missing = tuple(item for item in inspection.required_decisions if item not in request.accepted)
        if missing:
            return refused("decision_required")
        if entry.compatibility is CompatibilityStatus.LEGACY_ONLY:
            runner.run_legacy(request.reference)
            return ResumeResult(ResumeStatus.LEGACY_REEXECUTION, request.reference, inspection.entry.reasons, inspection=inspection)
        if request.job_id is not None and request.job_id != entry.job_id:
            return refused("stale_revision")
        revision = entry.expected_revision if request.expected_revision is None else request.expected_revision
        if revision != entry.expected_revision:
            return refused("stale_revision")
        ticket = ContinuationTicket(entry.job_id, revision)
        operation_id = request.idempotency_key or self._new_id()
        result = runner.run_durable(request.reference, ticket, operation_id)
        if result.job_id is None:
            code = "unexpected_error" if result.failure is None else result.failure.code
            if code == "continuation_exists":
                tip = _Evaluation(self._evidence).tip_of(entry.project_id, entry.job_id)
                return ResumeResult(ResumeStatus.ALREADY_REQUESTED, request.reference, job_id=tip, continuation_of=entry.job_id)
            return ResumeResult(ResumeStatus.REFUSED, request.reference, inspection.entry.reasons, code,
                                job_id=entry.job_id, result=result, inspection=inspection)
        return ResumeResult(ResumeStatus.STARTED, request.reference, inspection.entry.reasons, job_id=result.job_id,
                            continuation_of=entry.job_id, result=result, inspection=inspection)


_REFUSAL_CODES = (
    (RecoveryReason.ACTIVE_LEASE, "job_in_progress"),
    (RecoveryReason.SOURCE_CHANGED, "source_changed"),
    (RecoveryReason.SOURCE_MISSING, "source_missing"),
    (RecoveryReason.SOURCE_UNREADABLE, "source_unreadable"),
    (RecoveryReason.POLICY_DENIED, "policy_denied"),
    (RecoveryReason.INVOCATION_UNAVAILABLE, "invocation_unavailable"),
    (RecoveryReason.JOB_STATE_NOT_RESUMABLE, "job_not_resumable"),
)


def _refusal_code(reasons: tuple[RecoveryReason, ...]) -> str:
    for reason, code in _REFUSAL_CODES:
        if reason in reasons:
            return code
    return "not_recoverable"
