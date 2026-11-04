"""Puts a real ``TranslateProject`` together for one book of a stored project.

Only this module knows both sides: the use case and its ports on one, and the project files, the EPUB
adapter, the v1 cache and the current validator on the other. A failure to open the project's storage
raises here, so a caller learns of it before any translator is asked anything.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .artifact import ArtifactError
from .artifact_destination import DestinationFileOps, DestinationPublications, LocalDestinationPayloads
from .domain import OutputPlan, TranslationOptions
from .epub_format import EPUB_REGISTRATION, epub_format_adapter
from .epub_io import (  # noqa: F401 - the CLI's only door to the current EPUB path
    SelectedChapter,
    TranslationReport,
    _validate_worker_options,
    detect_epub_language,
    resolve_chapter_selection,
    translate_epub,
)
from .format_local import LocalFormatSource
from .format_service import FormatRegistry, FormatService
from .job import JobError
from .job_events import JobFailure, JobFailureFamily
from .job_recovery import InspectJobRecoverability, ListInterruptedJobs, RecoveryEntry, RequestJobResume
from .job_recovery_store import LocalRecoveryEvidence
from .legacy_translation import LegacyExecutorPool, LegacyUnitExecutor, read_package, to_report, to_review_segment
from .project import ProjectError
from .project_schema import open_artifact_service, open_job_repository, open_provenance_repository
from .project_store import ProjectJsonRepository
from .provenance import ProvenanceError
from .resume import (  # noqa: F401 - the CLI still keeps this file while it runs, through this door only
    InvalidResumeState,
    ResumeStateError,
    ResumeStateStore,
    TranslationResumeState,
    default_processing_dir,
)
from .review_export import ReviewSegment
from .translate_project import (
    ContinuationTicket,
    OutputValidation,
    TranslateObserver,
    TranslateProject,
    TranslateProjectCommand,
    TranslateProjectPorts,
    TranslateProjectResult,
)
from .translation_engine import BookFacts, TranslationResources
from .translation_memory import TranslationMemory
from .translator import TranslatorError
from .validation import ValidationResult, validate_output_epub


# What the adapter refuses by policy, on any path. A book it merely cannot read is left to the current checks.
BOOK_REFUSALS = frozenset({
    "archive_too_large", "document_too_large", "duplicate_member", "encrypted_content", "encrypted_member", "entity_declaration",
    "malformed_encryption", "member_name_too_long", "member_too_large", "protected_member", "source_too_large", "suspicious_compression", "too_many_members",
    "unsafe_manifest_href", "unsafe_member_name",
})


class BookRefused(RuntimeError):
    """The book is refused before anything starts. The message is the code, never a name from the book."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def epub_format_service() -> FormatService:
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def check_book(source_path: Path) -> None:
    """Refuses a book the adapter refuses by policy, including on the paths that never reach the adapter."""
    failure = epub_format_service().inspect("epub", LocalFormatSource(source_path)).failure
    if failure is not None and failure.code in BOOK_REFUSALS:
        raise BookRefused(failure.code)


@dataclass(frozen=True, repr=False)
class LegacyResources:
    """What the current preflight prepares, in the shape it already has."""

    translator: object
    glossary: object | None = None
    translator_factory: Callable[[], object] | None = None

    def __repr__(self) -> str:
        return "LegacyResources()"


class LegacyTranslationRuntime:
    """The current language work behind the runtime port: the v1 cache, the glossary and the translator.

    ``prepare`` is whatever checks the provider and the cache, and it runs once, inside the job. Its
    exception is kept for the presentation, which has the exact words to say about it.
    """

    def __init__(self, source_path: Path, options: TranslationOptions, prepare: Callable[[], LegacyResources],
                 cache_factory: Callable[[], object]) -> None:
        self._source_path = source_path
        self._options = options
        self._prepare = prepare
        self._cache_factory = cache_factory
        self._prepared: LegacyResources | None = None
        self.preflight_error: Exception | None = None

    def __repr__(self) -> str:
        return "LegacyTranslationRuntime()"

    def preflight(self) -> JobFailure | None:
        try:
            self._prepared = self._prepare()
            _validate_worker_options(self._options, translator_factory=self._prepared.translator_factory, cache_factory=self._cache_factory)
        except BookRefused as error:
            self.preflight_error = error
            return JobFailure(JobFailureFamily.INPUT, error.code)
        except Exception as error:
            self.preflight_error = error
            return JobFailure(JobFailureFamily.RESOURCE, "preflight_failed")
        return None

    def facts(self) -> BookFacts:
        return read_package(self._source_path).facts

    @contextmanager
    def resources(self) -> Iterator[TranslationResources]:
        prepared, options = self._prepared, self._options
        pool = None
        if options.workers > 1:
            pool = LegacyExecutorPool(prepared.translator_factory, self._cache_factory, options, glossary=prepared.glossary)
        with self._cache_factory() as cache:
            memory = TranslationMemory(cache, options.translation_memory) if options.translation_memory is not None else None
            yield TranslationResources(LegacyUnitExecutor(prepared.translator, cache, options, glossary=prepared.glossary, memory=memory), pool)

    def classify(self, error: Exception) -> JobFailure:
        # The message is never read: it may quote the book or the translator's address.
        if isinstance(error, TranslatorError):
            return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_failed", retryable=True)
        return JobFailure(JobFailureFamily.INTERNAL, "text_failed")

    def report(self, result: TranslateProjectResult, destination: Path) -> TranslationReport:
        """The result in the current report's terms, which the current presentation already knows how to show."""
        glossary = None if self._prepared is None else self._prepared.glossary
        return to_report(result.engine, self._source_path, destination, read_package(self._source_path), self._options, glossary)

    def review_segments(self, result: TranslateProjectResult, destination: Path) -> list[ReviewSegment]:
        return [to_review_segment(entry, self._source_path, destination, self._options) for entry in result.engine.review]


class LegacyOutputValidator:
    """The current validator, reading the staged file. A warning names the output, never the temporary name."""

    def __init__(self, destination: Path) -> None:
        self._destination = destination

    def __repr__(self) -> str:
        return "LegacyOutputValidator()"

    def validate(self, staged: object, on_progress: Callable[[int, int, str], None] | None) -> OutputValidation:
        result = validate_output_epub(staged, on_progress)
        return OutputValidation(result.ok, tuple(warning.replace(str(staged), str(self._destination)) for warning in result.warnings),
                                result.document_count)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def book_refusal(source_path: Path) -> str | None:
    """``check_book`` as an answer. It is the same check on every path, so recovery cannot go around it."""
    try:
        check_book(source_path)
    except BookRefused as error:
        return error.code
    except OSError:
        # A book that cannot be read is not refused by policy. The caller already reports that it is gone.
        return None
    return None


@dataclass(frozen=True, repr=False)
class JobRecovery:
    """The three recovery use cases over one project folder, and the files behind their references."""

    evidence: LocalRecoveryEvidence
    list_interrupted: ListInterruptedJobs
    inspect: InspectJobRecoverability
    request_resume: RequestJobResume

    def __repr__(self) -> str:
        return "JobRecovery()"


def open_job_recovery(processing_dir: Path, *, clock: Callable[[], datetime] = _utc_now,
                      policy_refusal: Callable[[Path], str | None] = book_refusal) -> JobRecovery:
    """Opens nothing and creates nothing. Every read happens when a use case asks for it."""
    evidence = LocalRecoveryEvidence(processing_dir, policy_refusal=policy_refusal, clock=clock)
    return JobRecovery(evidence, ListInterruptedJobs(evidence), InspectJobRecoverability(evidence), RequestJobResume(evidence))


@dataclass(frozen=True, repr=False)
class InterruptedTranslations:
    """The files behind a listing, for a presentation that shows their paths. A job with no file has nothing to show here."""

    running: tuple[TranslationResumeState, ...] = ()
    invalid: tuple[InvalidResumeState, ...] = ()
    entries: tuple[RecoveryEntry, ...] = ()

    def __repr__(self) -> str:
        return f"InterruptedTranslations(running={len(self.running)}, invalid={len(self.invalid)})"

    @property
    def has_findings(self) -> bool:
        return bool(self.running or self.invalid)

    def entry_of(self, state: TranslationResumeState) -> RecoveryEntry:
        return self.entries[self.running.index(state)]


def interrupted_translations(recovery: JobRecovery) -> InterruptedTranslations:
    running: list[TranslationResumeState] = []
    invalid: list[InvalidResumeState] = []
    entries: list[RecoveryEntry] = []
    for entry in recovery.list_interrupted.execute().entries:
        file = recovery.evidence.legacy_entry(entry.reference)
        if file is None:
            continue
        if file.state is None:
            invalid.append(InvalidResumeState(path=file.path, message=file.message))
        else:
            running.append(file.state)
            entries.append(entry)
    return InterruptedTranslations(tuple(running), tuple(invalid), tuple(entries))


def open_translate_project(processing_dir: Path, project_id: str, source_path: Path, destination: Path, *, overwrite: bool,
                           runtime: LegacyTranslationRuntime, clock: Callable[[], datetime] = _utc_now,
                           ops: DestinationFileOps | None = None, **tuning: object) -> TranslateProject:
    """``tuning`` is the optional new_id and lease timing of the use case. ``ops`` lets a test break the file system."""
    projects = ProjectJsonRepository(processing_dir)
    paths = projects.resolve_database_paths(project_id)
    root = projects.resolve_artifact_root(project_id)
    payloads = LocalDestinationPayloads(root, destination=destination, replace_existing=overwrite, ops=ops)
    jobs = open_job_repository(paths.database, paths.backups, project_id, clock=clock)
    artifacts = open_artifact_service(paths.database, paths.backups, root, project_id, payloads=payloads, clock=clock)
    provenance = open_provenance_repository(paths.database, paths.backups, project_id, clock=clock)
    ports = TranslateProjectPorts(projects, jobs, provenance, epub_format_service(), DestinationPublications(artifacts, payloads, destination),
                                  LegacyOutputValidator(destination), LocalFormatSource(source_path), runtime, projects.run_guard(project_id))
    return TranslateProject(ports, clock=clock, **tuning)


def validate_output(path: Path, on_progress: Callable[[int, int, str], None] | None = None) -> ValidationResult:
    """The current validator, for what does not run through the use case."""
    return validate_output_epub(path, on_progress=on_progress)


def translate_without_project(source_path: Path, destination_path: Path, **arguments: object) -> TranslationReport:
    """The current flow, for what has no project to belong to.

    Dry run and preview create no project on purpose, and a resume file that recomputes to no job is not given one.
    Everything else goes through ``translate_in_project``.
    """
    return translate_epub(source_path, destination_path, **arguments)


class ProjectTranslationUnavailable(RuntimeError):
    """The project's storage could not be opened. Nothing was started, and the message is a bounded code."""


@dataclass(frozen=True, repr=False)
class ProjectTranslation:
    """One book of one stored project, in the current language work's terms."""

    processing_dir: Path
    project_id: str
    source_sha256: str
    source_path: Path
    options: TranslationOptions
    output: OutputPlan
    replace_existing: bool
    prepare: Callable[[], LegacyResources]
    cache_factory: Callable[[], object]
    continuation: ContinuationTicket | None = None
    operation_id: str | None = None

    def __repr__(self) -> str:
        return f"ProjectTranslation(project_id={self.project_id!r})"


@dataclass(frozen=True, repr=False)
class ProjectTranslationRun:
    """``report`` is None when the run ended with nothing to report on, as a refusal or a failure does."""

    result: TranslateProjectResult
    report: TranslationReport | None
    review_segments: list[ReviewSegment]
    preflight_error: Exception | None

    def __repr__(self) -> str:
        return f"ProjectTranslationRun(result={self.result!r})"


def translate_in_project(translation: ProjectTranslation, observer: TranslateObserver) -> ProjectTranslationRun:
    runtime = LegacyTranslationRuntime(translation.source_path, translation.options, translation.prepare, translation.cache_factory)
    destination = translation.output.path
    try:
        use_case = open_translate_project(translation.processing_dir, translation.project_id, translation.source_path, destination,
                                          overwrite=translation.replace_existing, runtime=runtime)
    except (ProjectError, JobError, ArtifactError, ProvenanceError, OSError, sqlite3.Error) as error:
        code = str(error) if isinstance(error, (ProjectError, JobError, ArtifactError, ProvenanceError)) else "project_storage_unavailable"
        raise ProjectTranslationUnavailable(code) from None
    # The plan holds the canonical path, the form a resume file records. The destination stays as given, so a link stays a link.
    planned = replace(translation.output, path=destination.expanduser().resolve())
    result = use_case.execute(TranslateProjectCommand(translation.project_id, translation.source_sha256, translation.options, planned,
                                                      overwrite=translation.replace_existing, operation_id=translation.operation_id),
                              observer=observer,
                              continuation=translation.continuation)
    engine = result.engine
    ended_with_a_report = engine.failure is None and result.raised is None and result.job_id is not None and runtime.preflight_error is None
    report = runtime.report(result, destination) if ended_with_a_report else None
    return ProjectTranslationRun(result, report, runtime.review_segments(result, destination), runtime.preflight_error)
