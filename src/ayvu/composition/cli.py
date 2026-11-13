"""Explicit, lazy production composition for the installed CLI.

Each named constructor creates only its selected workflow. Importing this module
opens no stores and builds no provider. There is no registry, cached graph or singleton.
The builders in ``composition/builders`` remain reusable implementation adapters.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from . import editions, surface_rules
from .builders import cache_admin, content, glossary, providers, review, translate_project, translate_transient
from ..review import local_files as review_files
from .builders.translate_batch import LocalBatchItem, LocalTranslateBatch, project_item_outcome
from .builders.translate_project import LegacyResources, ProjectTranslation, selected_model
from ..translation.batch_output import AcceptedItem, OutputIntent
from ..v1.cache import TranslationCache
from ..v1.cache_check import verify_cache_writable
from ..preferences.config_facade import ConfigStore, default_glossaries_dir as _default_glossaries_dir
from ..library.adapters import LocalLibraryFilesystem, ReaderLauncher
from ..library import LibraryService
from ..projects import resolve_project
from ..projects.store import ProjectJsonRepository, build_project_request, translation_selections
from ..translation.preflight import run_translation_preflight as _preflight, run_local_translation_preflight as _local_preflight
from ..recovery.checkpoint import ResumeStateStore, TranslationResumeState
from ..translation import ContinuationTicket, TranslateObserver, TranslateProjectResult
from ..translation import EngineObserver, TranslationReport
from ..kernel import JobWarning, OutputPlan, TranslationOptions, automatic_output_candidate


@dataclass(frozen=True, repr=False)
class _BookHooks:
    """What one book reports to the terminal and to the resume file while it runs."""

    on_chapter_start: Callable[[int, int, str], None]
    on_chapter_done: Callable[[int, int, str, object], None]
    on_text_processed: Callable[[str], None]
    on_validation: Callable[[int, int, str], None]


@dataclass(frozen=True, repr=False)
class _BookRequest:
    epub_path: Path
    output_plan: OutputPlan
    replace_existing: bool
    options: TranslationOptions
    cache_path: Path
    prepare: Callable[[], object]
    project_id: str | None = None
    source_sha256: str | None = None
    processing_dir: Path | None = None
    continuation: ContinuationTicket | None = None
    operation_id: str | None = None
    batch_plan: AcceptedItem | None = None
    cancellation: object | None = None
    on_project_result: Callable[[TranslateProjectResult], None] | None = None
    translator_name: str = "libretranslate"
    translator_models: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, repr=False)
class _BookOutcome:
    """``failure`` is a bounded code. With it there is no report, because the run never reached one."""

    report: TranslationReport | None = None
    validation: object | None = None
    failure: str | None = None
    raised: Exception | None = None
    warning: str | None = None
    job_result: TranslateProjectResult | None = None
    notices: tuple[str, ...] = ()


def configuration_store() -> ConfigStore:
    return ConfigStore.default()


def library_service(config) -> LibraryService:
    filesystem = LocalLibraryFilesystem(config.resolved_books_dir, config.folders.original, config.folders.translated)
    return LibraryService(filesystem, ReaderLauncher(config.reader_app), filesystem)


def default_glossaries_dir() -> Path:
    return _default_glossaries_dir()


def open_content_use_cases():
    return content.open_content_use_cases()


def cache_admin_services(*args, **kwargs):
    return cache_admin.cache_admin_services(*args, **kwargs)


def read_scope_map_payload(*args, **kwargs):
    return cache_admin.read_scope_map_payload(*args, **kwargs)


def managed_glossary_repository(directory):
    return glossary.managed_glossary_repository(directory)


def cli_provider_session(**configuration):
    return providers.cli_provider_session(**configuration)


def open_review_use_cases(processing_dir, *, source_path=None):
    return review.open_review_use_cases(processing_dir, source_path=source_path)


def destination_fingerprint(path):
    return review_files.destination_fingerprint(path)


def read_review_input(path):
    return review_files.read_review_input(path)


def run_translation_preflight(**configuration):
    return _preflight(**{"cache_check": verify_cache_writable, **configuration})


def resolve_translation_project(processing_dir, epub_path, *, source, target, glossary_path, profile_name, **selections):
    request = build_project_request(epub_path, source=source, target=target,
        glossary_path=glossary_path, profile_name=profile_name, selections=translation_selections(**selections))
    return resolve_project(ProjectJsonRepository(processing_dir), request)


def batch_output_intent(*args):
    return OutputIntent(*args)


def translate_batch(items, *, continue_on_error, on_progress):
    service = LocalTranslateBatch(tuple(LocalBatchItem(intent, execute) for intent, execute in items),
        continue_on_error=continue_on_error, on_progress=on_progress)
    return service.use_case.execute(service.command)


def batch_item_outcome(*args, **kwargs):
    return project_item_outcome(*args, **kwargs)


def book_refusal(*args, **kwargs):
    return translate_project.book_refusal(*args, **kwargs)


def check_book(*args, **kwargs):
    return translate_project.check_book(*args, **kwargs)


def default_processing_dir(*args, **kwargs):
    return translate_project.default_processing_dir(*args, **kwargs)


def detect_epub_language(*args, **kwargs):
    return translate_project.detect_epub_language(*args, **kwargs)


def interrupted_translations(*args, **kwargs):
    return translate_project.interrupted_translations(*args, **kwargs)


def open_job_recovery(*args, **kwargs):
    return translate_project.open_job_recovery(*args, **kwargs)


def resolve_chapter_selection(*args, **kwargs):
    return translate_project.resolve_chapter_selection(*args, **kwargs)


def translate_without_project(*args, **kwargs):
    return translate_project.translate_without_project(*args, **kwargs)


def validate_output(*args, **kwargs):
    return translate_project.validate_output(*args, **kwargs)


def _translate_without_project(book: _BookRequest, hooks: _BookHooks, *, validate: bool = True) -> _BookOutcome:
    """Dry run, and a resume file that recomputes to no job. See ``translate_without_project``."""
    if translation_modality(book.options.dry_run, book.translator_models, book.translator_name) == "transient_dry_run":
        return _BookOutcome(report=translate_transient.translate(book, hooks,
            selected_model(book.translator_models, book.translator_name),
            formats=translate_project.epub_format_service(), cache_path=translate_project.v2_cache_path(book.cache_path)))
    preflight = book.prepare()
    with TranslationCache(book.cache_path) as cache:
        report = translate_without_project(
            book.epub_path,
            book.output_plan.path,
            translator=preflight.translator,
            cache=cache,
            options=book.options,
            glossary=preflight.glossary,
            on_chapter_start=hooks.on_chapter_start,
            on_chapter_done=hooks.on_chapter_done,
            on_text_processed=hooks.on_text_processed,
            translator_factory=getattr(preflight, "translator_factory", None),
            cache_factory=lambda: TranslationCache(book.cache_path),
            **({"cancellation": book.cancellation} if book.cancellation is not None else {}),
        )
    validation = None
    if validate and report.output_written and not book.options.dry_run:
        validation = validate_output(book.output_plan.path, on_progress=hooks.on_validation)
    return _BookOutcome(report=report, validation=validation)


def _translate_in_project(book: _BookRequest, hooks: _BookHooks) -> _BookOutcome:
    def prepare() -> LegacyResources:
        preflight = book.prepare()
        return LegacyResources(preflight.translator, preflight.glossary, getattr(preflight, "translator_factory", None))

    run = translate_project.translate_in_project(
        ProjectTranslation(
            book.processing_dir,
            book.project_id,
            book.source_sha256,
            book.epub_path,
            book.options,
            book.output_plan,
            book.replace_existing,
            prepare,
            lambda: TranslationCache(book.cache_path),
            book.continuation,
            book.operation_id,
            book.batch_plan.publication_guard if book.batch_plan is not None else None,
            book.batch_plan,
            selected_model(book.translator_models, book.translator_name),
            book.cache_path,
            prepare_offline=book.prepare,
        ),
        TranslateObserver(
            EngineObserver(hooks.on_chapter_start, hooks.on_chapter_done, hooks.on_text_processed),
            on_validation_progress=hooks.on_validation,
        ),
        **({"cancellation": book.cancellation} if book.cancellation is not None else {}),
    )
    if book.on_project_result is not None:
        book.on_project_result(run.result)
    if run.preflight_error is not None:
        raise run.preflight_error
    result = run.result
    notices = tuple(dict.fromkeys(event.payload.code for event in result.events
        if isinstance(event.payload, JobWarning) and event.payload.code not in ("validation_warnings", "validation_unavailable")))
    reason = None if result.outcome is None else result.outcome.summary.reason_code
    if run.report is None:
        code = reason if reason == "publication_uncommitted" else (result.failure.code if result.failure else "job_record_failed")
        return _BookOutcome(failure=code, raised=result.raised, job_result=result, notices=notices)
    warning = "job_record_failed" if not result.recorded else (reason if reason == "provenance_failed" else None)
    return _BookOutcome(
        report=run.report,
        validation=result.validation,
        warning=warning,
        job_result=result,
        notices=notices,
    )


class TranslationCheckpoint:
    """Compatibility checkpoint writes stay with the storage adapter, never the presenter."""
    def __init__(self):
        self.store = None
        self.state = None

    def __repr__(self):
        return 'TranslationCheckpoint()'

    def start(self, *, processing_dir, epub_path, **values):
        self.store = ResumeStateStore(processing_dir)
        self.state = TranslationResumeState.create(input_path=epub_path, **values)
        self._save()

    def chapter_started(self, index, total, name):
        if self.state is not None:
            self.state = self.state.start_chapter(name, total)
            self._save()

    def chapter_done(self, index, total, name, stats):
        if self.state is not None:
            self.state = self.state.record_chapter(name=name, total=total,
                ok=not stats.errors and stats.missing == 0, failed_segment_count=len(stats.errors) + stats.missing)
            self._save()

    def completed(self):
        if self.state is not None:
            self.state = self.state.mark_completed()
            self._save()

    def _save(self):
        self.store.save(self.state)


def translation_checkpoint():
    return TranslationCheckpoint()


def translation_modality(dry_run, models, translator_name):
    """Select without opening resources; unlabeled runs retain the predecessor path."""
    return ("transient_dry_run" if dry_run and selected_model(models, translator_name) is not None else "standard")


def prepare_translation(*, epub_path, options, cache_path, glossary_path, translator_name,
                        url, timeout, retries, requests_per_second, retry_backoff, retry_backoff_max,
                        output_path, overwrite, processing_dir, profile_name,
                        preflight, save_checkpoint, on_route, on_ready, network_policy=None,
                        translator_models=None, project_backed=False, modality="standard"):
    """Wire predecessor preparation and the compatibility checkpoint at its original safe point."""
    check_book(epub_path)
    if modality == "transient_dry_run":
        local = _local_preflight(epub_path=epub_path, glossary_path=glossary_path, language_pair=options.language_pair,
            translator_name=translator_name, url=url, timeout=timeout, retries=retries,
            requests_per_second=requests_per_second, retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max)
        prepared = translate_transient.prepare(epub_path=epub_path, options=options, glossary=local.glossary,
            glossary_path=glossary_path, translator_name=translator_name, url=url, processing_dir=processing_dir, profile_name=profile_name)
        on_ready()
        return prepared
    rich = project_backed and selected_model(translator_models or {}, translator_name) is not None
    if rich and options.cache_only:
        prepared = _local_preflight(epub_path=epub_path, glossary_path=glossary_path, language_pair=options.language_pair,
            translator_name=translator_name, url=url, timeout=timeout, retries=retries,
            requests_per_second=requests_per_second, retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max)
        on_ready()
        return prepared
    # The rich runtime already prepared the sibling cache before entering this callback.
    cache_options = {"cache_check": lambda _path: None} if rich else {}
    prepared = preflight(epub_path=epub_path, cache_path=cache_path, glossary_path=glossary_path,
        translator_name=translator_name, url=url, timeout=timeout, retries=retries,
        requests_per_second=requests_per_second, retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
        language_pair=options.language_pair, dry_run=options.dry_run, cache_only=options.cache_only,
        network_policy=network_policy, **cache_options)
    on_route(prepared.route)
    if not options.dry_run and not options.cache_only:
        save_checkpoint(epub_path=epub_path, output_path=output_path, cache_path=cache_path,
            translator_name=translator_name, url=url, glossary_path=glossary_path, options=options,
            overwrite=overwrite, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            processing_dir=processing_dir, profile_name=profile_name, **({"execution_revision": 2} if rich else {}))
    on_ready()
    return prepared


def publish_cli_report(path: Path, text: str) -> Path:
    """Preserve the current report publication semantics, including mode and overwrite behavior."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def report_destination(directory: Path, stem: str, suffix: str = '.md') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = automatic_output_candidate(directory, stem, suffix, 1)
    index = 2
    while path.exists():
        path = automatic_output_candidate(directory, stem, suffix, index)
        index += 1
    return path


@dataclass(frozen=True)
class CliSurface:
    """Presentation data only; command paths are binding keys, menu pairs are key/action."""

    commands: tuple[tuple[str, ...], ...]
    menu_actions: tuple[tuple[str, str], ...]
    providers: tuple[str, ...]
    input_formats: tuple[str, ...]
    extraction_writers: tuple[str, ...]
    report_writers: tuple[str, ...]
    assistance_roles: tuple[str, ...]
    preview: bool
    execution_preset: str | None
    report_style: str


def project_cli_surface(features: frozenset[str], *, execution_preset=None, report_style=None) -> CliSurface:
    """Pure capability projection, also usable without product applicability in tests."""
    selected = surface_rules.project_surface(features, execution_preset=execution_preset, report_style=report_style)
    return CliSurface(selected.commands, selected.menu_actions, selected.providers,
        selected.input_formats, selected.extraction_writers, selected.report_writers,
        selected.assistance_roles, selected.preview, selected.execution_preset, selected.report_style)


def cli_surface(edition_id: str) -> CliSurface:
    """Resolve a product for the terminal without acquiring workflow resources."""
    edition = editions.get_edition(edition_id)
    if "cli" not in edition.interfaces:
        raise ValueError("Product does not support the CLI interface")
    return project_cli_surface(edition.features, execution_preset=edition.execution_preset,
        report_style=edition.report_style)


def installed_cli_surface() -> CliSurface:
    """Declare the installed presentation surface without acquiring resources."""
    return cli_surface("cli_free")
