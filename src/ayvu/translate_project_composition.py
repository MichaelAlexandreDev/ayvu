"""Puts a real ``TranslateProject`` together for one book of a stored project.

Only this module knows both sides: the use case and its ports on one, and the project files, the EPUB
adapter, the v1 cache and the current validator on the other. A failure to open the project's storage
raises here, so a caller learns of it before any translator is asked anything.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .artifact_destination import DestinationFileOps, DestinationPublications, LocalDestinationPayloads
from .domain import TranslationOptions
from .epub_format import EPUB_REGISTRATION, epub_format_adapter
from .epub_io import TranslationReport, _validate_worker_options
from .format_local import LocalFormatSource
from .format_service import FormatRegistry, FormatService
from .job_events import JobFailure, JobFailureFamily
from .legacy_translation import LegacyExecutorPool, LegacyUnitExecutor, read_package, to_report, to_review_segment
from .project_schema import open_artifact_service, open_job_repository, open_provenance_repository
from .project_store import ProjectJsonRepository
from .review_export import ReviewSegment
from .translate_project import OutputValidation, TranslateProject, TranslateProjectPorts, TranslateProjectResult
from .translation_engine import BookFacts, TranslationResources
from .translation_memory import TranslationMemory
from .translator import TranslatorError
from .validation import validate_output_epub


def epub_format_service() -> FormatService:
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


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
                                  LegacyOutputValidator(destination), LocalFormatSource(source_path), runtime)
    return TranslateProject(ports, clock=clock, **tuning)
