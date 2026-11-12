"""Commands, plans, ports and results for durable project translation."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from ..artifacts import Artifact, ArtifactRequest
from ..formats import FormatPublication, FormatService, FormatSource
from ..jobs import JobState, TranslationJob
from ..jobs import JobEvent, JobOutcome
from ..jobs import CancellationSource, JobEventSink
from ..jobs import JobRepository
from ..projects import ProjectManifest
from ..provenance import ProducerIdentity, ProvenanceRequest
from ..formats import BookFacts
from .engine import EngineObserver, EngineResult, TranslationResources
from ..kernel import OutputPlan, TranslationOptions, JobError, validate_job_identity, JobFailure


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
_SHA256 = re.compile(r"[0-9a-f]{64}")


# The states this use case leaves a job in. Any other nonterminal state belongs to someone else.
_OWN_STATES = (JobState.QUEUED, JobState.PREFLIGHT, JobState.RUNNING, JobState.CANCELLING, JobState.COMPLETED, JobState.VALIDATING)
OWN_JOB_STATES = _OWN_STATES


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
    accepted_output: AcceptedOutputPlan | None = field(default=None, repr=False, compare=False)
    execution_revision: int = 1
    producer: ProducerIdentity | None = field(default=None, repr=False)
    additional_output_roles: tuple[str, ...] = ()
    context_revision: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.context_revision is not None and (type(self.context_revision) is not str
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', self.context_revision)):
            raise TranslateCommandError('invalid_context_revision')
        if self.schema_version != TRANSLATE_COMMAND_VERSION:
            raise TranslateCommandError("unsupported_command_version")
        if (type(self.execution_revision) is not int or not 1 <= self.execution_revision < 2**31
                or (self.producer is not None and type(self.producer) is not ProducerIdentity)):
            raise TranslateCommandError("invalid_execution_identity")
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
        if self.accepted_output is not None and (self.accepted_output.execution_output != self.output
                                                 or self.accepted_output.overwrite != self.overwrite):
            raise TranslateCommandError("output_plan_mismatch")

    def __repr__(self) -> str:
        return f"TranslateProjectCommand(project_id={self.project_id!r}, schema_version={self.schema_version})"


@dataclass(frozen=True, repr=False)
class ContinuationTicket:
    """Continue this job, if it is still as the caller saw it. A job that ended is never reopened."""

    job_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        try:
            validate_job_identity(self.job_id)
        except JobError:
            raise TranslateCommandError("invalid_identity") from None
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise TranslateCommandError("invalid_command")

    def __repr__(self) -> str:
        return f"ContinuationTicket(job_id={self.job_id!r})"


@dataclass(frozen=True, repr=False)
class ExecutionPlan:
    """Everything that decides the result. Changing any of it gives a different fingerprint."""

    project_id: str
    source_sha256: str
    input_fingerprint: str

    def __repr__(self) -> str:
        return f"ExecutionPlan(project_id={self.project_id!r})"


def resolve_execution_plan(command: TranslateProjectCommand, project: ProjectManifest) -> ExecutionPlan:
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
    if command.execution_revision != 1 or command.producer is not None or command.additional_output_roles:
        producer = command.producer
        inputs["execution"] = [command.execution_revision, command.additional_output_roles,
                               None if producer is None else [producer.provider, producer.model, producer.adapter, producer.pipeline]]
    if memory is not None and (memory.conservative_auto_apply or memory.legacy_cache_compatibility):
        inputs["memory_policy"] = [memory.conservative_auto_apply, memory.legacy_cache_compatibility]
    if command.context_revision is not None:
        inputs['translation_context'] = command.context_revision
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


@dataclass(frozen=True, repr=False)
class RuntimeContext:
    job: TranslationJob
    cancellation: CancellationSource
    events: JobEventSink
    current_job: Callable[[], TranslationJob] | None = field(default=None, repr=False)
    # Earlier jobs of this continuation chain, nearest first. A runtime may reuse only what it proves from them.
    reusable_jobs: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return "RuntimeContext()"


class TranslationRuntime(Protocol):
    """The language side: everything that knows a provider, a cache or a glossary."""

    def preflight(self) -> JobFailure | None: ...
    def facts(self) -> BookFacts: ...
    def resources(self, context: RuntimeContext) -> AbstractContextManager[TranslationResources]: ...
    def classify(self, error: Exception) -> JobFailure: ...


class OutputValidator(Protocol):
    """``staged`` is opaque here: it comes from the store and goes back to its adapter."""

    def validate(self, staged: object, on_progress: Callable[[int, int, str], None] | None) -> OutputValidation: ...


class OutputPublications(Protocol):
    def publication(self, request: ArtifactRequest, owner_token: str, on_staged: Callable[[object], None]) -> FormatPublication: ...
    def find_ready(self, request: ArtifactRequest) -> Artifact | None: ...
    def reconcile(self) -> None: ...


class AcceptedOutputPlan(Protocol):
    """Opaque accepted revision; the output decision and publication guard travel together."""

    @property
    def output(self) -> OutputPlan: ...
    @property
    def execution_output(self) -> OutputPlan: ...
    @property
    def overwrite(self) -> bool: ...
    def revalidate(self) -> None: ...
    def publication_guard(self) -> None: ...
    def mark_published(self) -> None: ...


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
    runtime_safe_points: tuple[str, ...] = ()
    recorded_producer: Callable[[TranslationJob], ProducerIdentity | None] | None = field(default=None, repr=False)

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
    artifact_destination: object | None = field(default=None, repr=False, compare=False)
    project_revision: int | None = field(default=None, repr=False)
    report_ref: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        state = None if self.state is None else self.state.value
        return f"TranslateProjectResult(job_id={self.job_id!r}, state={state!r})"

    @property
    def output_ready(self) -> bool:
        return self.artifact is not None and self.recorded and self.state in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)


class TranslateProjectInterrupted(KeyboardInterrupt):
    """The interruption and the durable result settled before it escaped the active item."""

    def __init__(self, result: TranslateProjectResult) -> None:
        super().__init__("interrupted")
        self.result = result
