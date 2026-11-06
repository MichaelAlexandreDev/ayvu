"""Explicit, lazy production composition for the installed CLI.

Each named constructor creates only its selected workflow. Importing this module
opens no stores and builds no provider. There is no registry, cached graph or singleton.
Predecessor composition modules remain reusable implementation adapters.
"""
from __future__ import annotations
from pathlib import Path
from . import cache_admin_composition, content_composition, glossary_catalog_composition
from . import provider_composition, review_composition, translate_project_composition
from .batch_output_policy import OutputIntent
from .translate_batch_composition import LocalBatchItem, LocalTranslateBatch, project_item_outcome
from .cache import TranslationCache
from .cli_contracts import _BookHooks, _BookRequest, _BookOutcome
from .config import ConfigStore, default_glossaries_dir as _default_glossaries_dir
from .library_adapters import LocalLibraryFilesystem, ReaderLauncher
from .library_service import LibraryService
from .project_service import resolve_project
from .project_store import ProjectJsonRepository, build_project_request, translation_selections
from .preflight import run_translation_preflight as _preflight
from .resume import ResumeStateStore, TranslationResumeState
from .translate_project import TranslateObserver
from .translation_engine import EngineObserver
from .translate_project_composition import LegacyResources, ProjectTranslation


def configuration_store() -> ConfigStore:
    return ConfigStore.default()


def library_service(config) -> LibraryService:
    filesystem = LocalLibraryFilesystem(config.resolved_books_dir, config.folders.original, config.folders.translated)
    return LibraryService(filesystem, ReaderLauncher(config.reader_app), filesystem)


def default_glossaries_dir() -> Path:
    return _default_glossaries_dir()


def open_content_use_cases():
    return content_composition.open_content_use_cases()


def cache_admin_services(*args, **kwargs):
    return cache_admin_composition.cache_admin_services(*args, **kwargs)


def read_scope_map_payload(*args, **kwargs):
    return cache_admin_composition.read_scope_map_payload(*args, **kwargs)


def managed_glossary_repository(directory):
    return glossary_catalog_composition.managed_glossary_repository(directory)


def cli_provider_session(**configuration):
    return provider_composition.cli_provider_session(**configuration)


def open_review_use_cases(processing_dir, *, source_path=None):
    return review_composition.open_review_use_cases(processing_dir, source_path=source_path)


def destination_fingerprint(path):
    return review_composition.destination_fingerprint(path)


def read_review_input(path):
    return review_composition.read_review_input(path)


def run_translation_preflight(**configuration):
    return _preflight(**configuration)


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
    return translate_project_composition.book_refusal(*args, **kwargs)


def check_book(*args, **kwargs):
    return translate_project_composition.check_book(*args, **kwargs)


def default_processing_dir(*args, **kwargs):
    return translate_project_composition.default_processing_dir(*args, **kwargs)


def detect_epub_language(*args, **kwargs):
    return translate_project_composition.detect_epub_language(*args, **kwargs)


def interrupted_translations(*args, **kwargs):
    return translate_project_composition.interrupted_translations(*args, **kwargs)


def open_job_recovery(*args, **kwargs):
    return translate_project_composition.open_job_recovery(*args, **kwargs)


def resolve_chapter_selection(*args, **kwargs):
    return translate_project_composition.resolve_chapter_selection(*args, **kwargs)


def translate_without_project(*args, **kwargs):
    return translate_project_composition.translate_without_project(*args, **kwargs)


def validate_output(*args, **kwargs):
    return translate_project_composition.validate_output(*args, **kwargs)


def _translate_without_project(book: _BookRequest, hooks: _BookHooks, *, validate: bool = True) -> _BookOutcome:
    """Dry run, and a resume file that recomputes to no job. See ``translate_without_project``."""
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

    run = translate_project_composition.translate_in_project(
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
    reason = None if result.outcome is None else result.outcome.summary.reason_code
    if run.report is None:
        code = reason if reason == "publication_uncommitted" else (result.failure.code if result.failure else "job_record_failed")
        return _BookOutcome(failure=code, raised=result.raised, job_result=result)
    warning = "job_record_failed" if not result.recorded else (reason if reason == "provenance_failed" else None)
    return _BookOutcome(
        report=run.report,
        validation=result.validation,
        warning=warning,
        job_result=result,
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


def prepare_translation(*, epub_path, options, cache_path, glossary_path, translator_name,
                        url, timeout, retries, requests_per_second, retry_backoff, retry_backoff_max,
                        output_path, overwrite, processing_dir, profile_name,
                        preflight, save_checkpoint, on_route, on_ready, network_policy=None):
    """Wire predecessor preparation and the compatibility checkpoint at its original safe point."""
    check_book(epub_path)
    prepared = preflight(epub_path=epub_path, cache_path=cache_path, glossary_path=glossary_path,
        translator_name=translator_name, url=url, timeout=timeout, retries=retries,
        requests_per_second=requests_per_second, retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
        language_pair=options.language_pair, dry_run=options.dry_run, cache_only=options.cache_only,
        network_policy=network_policy)
    on_route(prepared.route)
    if not options.dry_run and not options.cache_only:
        save_checkpoint(epub_path=epub_path, output_path=output_path, cache_path=cache_path,
            translator_name=translator_name, url=url, glossary_path=glossary_path, options=options,
            overwrite=overwrite, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            processing_dir=processing_dir, profile_name=profile_name)
    on_ready()
    return prepared


def publish_cli_report(path: Path, text: str) -> Path:
    """Preserve the current report publication semantics, including mode and overwrite behavior."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def report_destination(directory: Path, stem: str, suffix: str = '.md') -> Path:
    from .domain import automatic_output_candidate
    directory.mkdir(parents=True, exist_ok=True)
    path = automatic_output_candidate(directory, stem, suffix, 1)
    index = 2
    while path.exists():
        path = automatic_output_candidate(directory, stem, suffix, index)
        index += 1
    return path
