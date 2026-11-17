"""Puts a real ``TranslateProject`` together for one book of a stored project.

Only this module knows both sides: the use case and its ports on one, and the project files, the EPUB
adapter, the v1 cache and the current validator on the other. A failure to open the project's storage
raises here, so a caller learns of it before any translator is asked anything.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ...translation.stages import RetrievalPolicy
from ...artifacts import ArtifactError
from ...artifacts.destination import DestinationFileOps, DestinationPublications, LocalDestinationPayloads
from .rich_document_executor import prepare_offline
from .rich_provider import LibreTranslateBinding
from .rich_workers import offline_work, online_work, project_store_options
from ...knowledge.cache import CacheIdentityError
from ...knowledge.cache.repository import SQLiteExactCache
from ...knowledge.cache.schema import CacheSchemaError, SchemaState, classify_cache
from ...translation.context import ContextExecution, ContextError
from ...formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ...formats.epub.io import SelectedChapter, detect_epub_language, resolve_chapter_selection
from ...v1.epub import _validate_worker_options, translate_epub  # noqa: F401 - the same door, for v1
from ...formats.local import LocalFormatSource, LocalOutputGuard
from ...formats import BookFacts, FormatPublication, FormatRegistry, FormatService
from ...jobs import JobState, CancellationSource
from ...recovery import InspectJobRecoverability, ListInterruptedJobs, RecoveryEntry, RequestJobResume
from ...recovery.store import LocalRecoveryEvidence
from ...v1.translation import LegacyExecutorPool, LegacyUnitExecutor
from ...formats.epub.reader import read_package
from ...projects import ProjectError
from ...projects.schema import ProjectDatabaseTurns, open_artifact_service, open_job_repository, open_provenance_repository
from ...projects.store import ProjectJsonRepository
from ...provenance import ProvenanceError
from ...recovery.checkpoint import (  # noqa: F401 - the CLI still keeps this file while it runs, through this door only
    InvalidResumeState,
    ResumeStateError,
    ResumeStateStore,
    TranslationResumeState,
    default_processing_dir,
)
from ...translation import (
    AcceptedOutputPlan,
    ContinuationTicket,
    OutputValidation,
    TranslateObserver,
    TranslateProject,
    TranslateProjectCommand,
    TranslateProjectPorts,
    TranslateProjectResult,
    TranslateCommandError,
    TranslationRuntime, TranslationReport, to_report,
    RuntimeContext,
)
from ...translation import TranslationResources
from ...translation import (
    SAFE_POINTS as DOCUMENT_SAFE_POINTS, RAW_RESULTS_ROLE, TranslationExecutionError,
)
from ...knowledge.memory import TranslationMemory
from ...providers.libretranslate.client import TranslatorError
from ...formats.epub.validation import ValidationResult, validate_output_epub
from ...kernel import OutputPlan, TranslationOptions, JobError, JobFailure, JobFailureFamily
from ...translation import BookRefused, PreflightError, ProjectTranslationUnavailable, TranslationDryRunCancelled

# What the adapter refuses by policy, on any path. A book it merely cannot read is left to the current checks.
BOOK_REFUSALS = frozenset({
    "archive_too_large", "document_too_large", "duplicate_member", "encrypted_content", "encrypted_member", "entity_declaration",
    "malformed_encryption", "member_name_too_long", "member_too_large", "protected_member", "source_too_large", "suspicious_compression", "too_many_members",
    "unsafe_manifest_href", "unsafe_member_name",
})


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
            memory_options = self._options.translation_memory
            if memory_options is not None and (memory_options.conservative_auto_apply or memory_options.legacy_cache_compatibility):
                raise PreflightError("Essas opções de política requerem o runtime rich/v2.",
                    "Configure translator_models.libretranslate com um rótulo de modelo antes de usá-las.")
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
    def resources(self, context: RuntimeContext | None = None) -> Iterator[TranslationResources]:
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
                           runtime: TranslationRuntime, clock: Callable[[], datetime] = _utc_now,
                           ops: DestinationFileOps | None = None, before_publish: Callable[[], None] | None = None,
                           busy_timeout: float = 0.5, turns: ProjectDatabaseTurns | None = None, **tuning: object) -> TranslateProject:
    """``tuning`` is the optional new_id and lease timing of the use case. ``ops`` lets a test break the file system."""
    projects = ProjectJsonRepository(processing_dir)
    paths = projects.resolve_database_paths(project_id)
    root = projects.resolve_artifact_root(project_id)
    payloads = LocalDestinationPayloads(root, destination=destination, replace_existing=overwrite, ops=ops,
                                        before_publish=before_publish)
    jobs = open_job_repository(paths.database, paths.backups, project_id, clock=clock, busy_timeout=busy_timeout, turns=turns)
    artifacts = open_artifact_service(paths.database, paths.backups, root, project_id, payloads=payloads, clock=clock, busy_timeout=busy_timeout, turns=turns)
    provenance = open_provenance_repository(paths.database, paths.backups, project_id, clock=clock, busy_timeout=busy_timeout, turns=turns)
    def recorded_producer(job):
        if job.request.command_version == 1:
            return None
        nodes = provenance.list_by_job(job.job_id, limit=1).nodes
        return nodes[0].record.request.producer if nodes else None
    safe_points = DOCUMENT_SAFE_POINTS if isinstance(runtime, RichTranslationRuntime) else ()
    ports = TranslateProjectPorts(projects, jobs, provenance, epub_format_service(), DestinationPublications(artifacts, payloads, LocalOutputGuard(destination), FormatPublication),
                                  LegacyOutputValidator(destination), LocalFormatSource(source_path), runtime, projects.run_guard(project_id),
                                  runtime_safe_points=safe_points, recorded_producer=recorded_producer)
    return TranslateProject(ports, clock=clock, **tuning)


def validate_output(path: Path, on_progress: Callable[[int, int, str], None] | None = None) -> ValidationResult:
    """The current validator, for what does not run through the use case."""
    return validate_output_epub(path, on_progress=on_progress)


def translate_without_project(source_path: Path, destination_path: Path, **arguments: object) -> TranslationReport:
    """The current flow, for what has no project to belong to.

    Dry run and preview create no project on purpose, and a resume file that recomputes to no job is not given one.
    Everything else goes through ``translate_in_project``.
    """
    cancellation = arguments.pop("cancellation", None)
    if cancellation is None:
        return translate_epub(source_path, destination_path, **arguments)

    def checkpoint() -> None:
        if cancellation.checkpoint("dry_run_safe_point") is not None:
            raise TranslationDryRunCancelled("cancelled")

    def watching(callback):
        def notify(*args):
            checkpoint()
            if callback is not None:
                callback(*args)
        return notify

    checkpoint()
    for name in ("on_chapter_start", "on_chapter_done", "on_text_processed"):
        arguments[name] = watching(arguments.get(name))
    report = translate_epub(source_path, destination_path, **arguments)
    checkpoint()
    return report


class CancellationBinding(Protocol):
    def bind(self, source: CancellationSource) -> AbstractContextManager[CancellationSource]: ...


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
    before_publish: Callable[[], None] | None = None
    accepted_output: AcceptedOutputPlan | None = None
    model_label: str | None = None
    cache_path: Path | None = None
    translation_context: ContextExecution | None = None
    prepare_offline: Callable[[], object] | None = None

    def __repr__(self) -> str:
        return f"ProjectTranslation(project_id={self.project_id!r})"


@dataclass(frozen=True, repr=False)
class ProjectTranslationRun:
    """``report`` is None when the run ended with nothing to report on, as a refusal or a failure does."""

    result: TranslateProjectResult
    report: TranslationReport | None
    preflight_error: Exception | None

    def __repr__(self) -> str:
        return f"ProjectTranslationRun(result={self.result!r})"


def translate_in_project(translation: ProjectTranslation, observer: TranslateObserver, *,
                         cancellation: CancellationSource | CancellationBinding | None = None) -> ProjectTranslationRun:
    if translation.accepted_output is not None and translation.output != translation.accepted_output.output:
        raise TranslateCommandError("output_plan_mismatch")
    runtime = LegacyTranslationRuntime(translation.source_path, translation.options, translation.prepare, translation.cache_factory)
    execution = {}
    destination = translation.output.path
    overwrite = translation.accepted_output.overwrite if translation.accepted_output is not None else translation.replace_existing
    try:
        if translation.translation_context is not None and (translation.model_label is None or not translation.options.cache_only):
            if translation.model_label is None:
                raise ContextError('context_capabilities')
            execution_context = translation.translation_context
            now = datetime.now(UTC)
            execution_context.validate(now)
            if any(r.snapshot.profile.project.value != translation.project_id
                   or r.operation_id != translation.operation_id
                   or r.snapshot.profile.target_language != translation.options.target
                   for r in execution_context.requests):
                raise ContextError('context_authorization')
            context_binding = (cancellation.bind(CancellationSource(('pipeline_between_stages',)))
                if cancellation is not None and not isinstance(cancellation, CancellationSource)
                else nullcontext(cancellation))
            with context_binding as source:
                cancel_context = (None if source is None else
                    lambda: source.token.checkpoint('pipeline_between_stages') is not None)
                execution_context.prepare(now, cancel=cancel_context)
            execution_context.authorize_prepared(datetime.now(UTC))
        if translation.model_label is not None:
            projects = ProjectJsonRepository(translation.processing_dir)
            paths = projects.resolve_database_paths(translation.project_id)
            runtime = RichTranslationRuntime(translation.source_path, translation.options, translation.prepare,
                translation.cache_path, translation.model_label, paths.database, paths.backups,
                projects.resolve_artifact_root(translation.project_id), translation.project_id,
                translation_context=translation.translation_context, prepare_offline=translation.prepare_offline)
            execution = {"execution_revision": 2, "producer": runtime.producer,
                         "additional_output_roles": () if translation.options.cache_only else (RAW_RESULTS_ROLE,)}
            if translation.translation_context is not None:
                execution['context_revision'] = translation.translation_context.job_revision
        guard = translation.accepted_output.publication_guard if translation.accepted_output is not None else translation.before_publish
        publication_options = {**({} if guard is None else {"before_publish": guard}), **project_store_options(translation, runtime)}
        use_case = open_translate_project(translation.processing_dir, translation.project_id, translation.source_path, destination,
                                          overwrite=overwrite, runtime=runtime, **publication_options)
    except (ProjectError, JobError, ArtifactError, ProvenanceError, ContextError, OSError, sqlite3.Error) as error:
        code = str(error) if isinstance(error, (ProjectError, JobError, ArtifactError, ProvenanceError, ContextError)) else "project_storage_unavailable"
        raise ProjectTranslationUnavailable(code) from None
    # The plan holds the canonical path, the form a resume file records. The destination stays as given, so a link stays a link.
    planned = (translation.accepted_output.execution_output if translation.accepted_output is not None
               else replace(translation.output, path=destination.expanduser().resolve()))
    binding = (cancellation.bind(use_case.cancellation()) if cancellation is not None and not isinstance(cancellation, CancellationSource)
               else nullcontext(cancellation))
    with binding as source:
        result = use_case.execute(TranslateProjectCommand(translation.project_id, translation.source_sha256, translation.options, planned,
                                                      overwrite=overwrite, operation_id=translation.operation_id,
                                                      accepted_output=translation.accepted_output, **execution),
                              observer=observer,
                              continuation=translation.continuation, cancellation=source)
    engine = result.engine
    ended_with_a_report = (engine.failure is None and result.raised is None and result.job_id is not None
                           and runtime.preflight_error is None and result.state not in (JobState.FAILED, JobState.CANCELLED))
    report = runtime.report(result, destination) if ended_with_a_report else None
    return ProjectTranslationRun(result, report, runtime.preflight_error)


def v2_cache_path(legacy: Path) -> Path:
    """Append, rather than replace a suffix: cache.sqlite -> cache.sqlite.v2.sqlite."""
    return legacy.with_name(legacy.name + ".v2.sqlite")


def memory_policy(options) -> RetrievalPolicy:
    try:
        return RetrievalPolicy(fuzzy_auto_apply=options.conservative_auto_apply,
            suggest_threshold=options.suggest_threshold,
            apply_threshold=options.apply_threshold if options.conservative_auto_apply else max(0.98, options.apply_threshold),
            legacy_cache_enabled=options.legacy_cache_compatibility)
    except ValueError:
        raise PreflightError("Política de memória inválida no runtime rich/v2.",
            "Use --tm-apply-threshold 0.98 ou maior com --tm-conservative-auto-apply; fuzzy é sugestão por padrão.") from None


def check_rich_options(options: TranslationOptions) -> None:
    if options.workers > 1 and options.translation_memory is not None:
        raise PreflightError("--workers é incompatível com --translation-memory nesta versão.",
                             "Use --workers 1 com --translation-memory, ou remova --translation-memory para executar em paralelo.")
    if options.translation_memory is not None:
        memory_policy(options.translation_memory)
    if options.require_full_cache and not options.cache_only:
        raise PreflightError("--require-full-cache exige --cache-only.", "Adicione --cache-only.")


class RichTranslationRuntime:
    """Only the composition knows the concrete repositories; its binding knows the provider."""
    def __init__(self, source_path, options, prepare, cache_path, label, database, backups, artifact_root, project_id,
                 *, clock=lambda: datetime.now(UTC), translation_context: ContextExecution | None = None, prepare_offline=None) -> None:
        self._source_path, self._options, self._prepare = source_path, options, prepare
        self._cache_path = v2_cache_path(cache_path)
        self._database, self._backups, self._artifact_root = database, backups, artifact_root
        self._project_id, self._clock = project_id, clock
        self.binding = LibreTranslateBinding(label, clock)
        self.producer = self.binding.producer
        self.preflight_error = None
        self._prepared = None
        self._cache = None
        self._translation_context = translation_context
        self._prepare_offline = prepare_offline

    def __repr__(self) -> str:
        return "RichTranslationRuntime()"

    def preflight(self) -> JobFailure | None:
        try:
            check_rich_options(self._options)
            if self._options.cache_only:
                prepare_offline(self)
                return None
            if self._translation_context is not None:
                self._translation_context.validate(self._clock())
                self._translation_context.authorize_prepared(self._clock())
            # Do not promote even a v1 database accidentally placed at the sibling name.
            if classify_cache(self._cache_path) not in (SchemaState.ABSENT, SchemaState.EMPTY, SchemaState.V2):
                raise CacheSchemaError("cache_foreign")
            # Constructor calls prepare_cache, synchronously, before preparation can reach /languages.
            self._cache = SQLiteExactCache(self._cache_path)
            self._prepared = self._prepare()
        except TranslationExecutionError as error:
            self.preflight_error = PreflightError("Offline preparation refused.", "Check local execution evidence.", detail=error.failure.code)
            return error.failure
        except BookRefused as error:
            self.preflight_error = error
            return JobFailure(JobFailureFamily.INPUT, error.code)
        except CacheSchemaError as error:
            self.preflight_error = PreflightError("Não foi possível preparar o cache v2.",
                "Verifique o arquivo irmão de --cache e as permissões do sistema de arquivos.", detail=error.code)
            return JobFailure(JobFailureFamily.RESOURCE, error.code)
        except Exception as error:
            # Existing presentation exceptions have their established wording; cache errors above are codes only.
            self.preflight_error = error
            return JobFailure(JobFailureFamily.RESOURCE, "preflight_failed")
        return None

    def facts(self):
        return read_package(self._source_path).facts

    @contextmanager
    def resources(self, context: RuntimeContext):
        prepared, options = self._prepared, self._options
        if options.cache_only:
            yield offline_work(self, context)
            return
        plan = self.binding.plan(prepared, options)
        artifacts = open_artifact_service(self._database, self._backups, self._artifact_root, self._project_id, clock=self._clock)
        provenance = open_provenance_repository(self._database, self._backups, self._project_id, clock=self._clock)
        provider = self.binding.provider(prepared.translator, self.producer)
        try:
            yield online_work(self, context, plan, provider, artifacts, provenance, memory_policy)
        finally:
            provider.close()

    def classify(self, error: Exception) -> JobFailure:
        if isinstance(error, TranslationExecutionError):
            return error.failure
        if isinstance(error, (CacheSchemaError, CacheIdentityError)):
            return JobFailure(JobFailureFamily.RESOURCE, error.code)
        return JobFailure(JobFailureFamily.INTERNAL, "rich_translation_failed")

    def report(self, result, destination):
        return to_report(result.engine, self._source_path, destination, read_package(self._source_path),
                         self._options, None if self._prepared is None else self._prepared.glossary)
