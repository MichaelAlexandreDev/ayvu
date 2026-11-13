"""Wire the labeled offline dry run, before any durable store or provider.

The catalog grants only an existing matching project's exact cache scope. Missing
catalogs grant nothing; unreadable, incomplete or ambiguous catalogs fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ...formats.local import LocalFormatSource
from ...formats.epub.reader import read_package
from ...formats.epub import review_text_from_template
from ...knowledge.cache import CacheAuthorizationPolicy, CacheIdentityError
from ...knowledge.cache.read_only import ReadOnlyExactCache
from ...knowledge.cache.schema import CacheSchemaError
from ...knowledge.glossary import apply_glossary_with_usage, effective_glossary_reference
from ...projects import ProjectError, ProjectSourceError, select_project
from ...projects.store import ProjectJsonRepository, build_project_request, translation_selections, source_digest
from ...provenance import declared_model
from ...providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
from ...translation import (
    EngineObserver, LocalCachePlanIntent, LocalCachePlanResolver, PreflightError,
    TransientExecution, TransientTranslation, TransientUnits, TranslationExecutionError, to_report,
)


@dataclass(frozen=True, repr=False)
class TransientPreparation:
    glossary: object
    request: object
    processing_dir: object
    route: None = None
    network_decision: None = None


def prepare(*, epub_path, options, glossary, glossary_path, translator_name, url, processing_dir, profile_name):
    """Bind the locally validated preparation to the same project intention."""
    selections = translation_selections(translator=translator_name, url=url, chunk_limit=options.chunk_limit,
        translate_metadata=options.translate_metadata, translate_alt_text=options.translate_alt_text,
        chapters=options.chapter_selection.source if options.chapter_selection else None,
        translation_memory=options.translation_memory)
    try:
        request = build_project_request(epub_path, source=options.source, target=options.target,
            glossary_path=glossary_path, profile_name=profile_name, selections=selections)
    except ProjectError as error:
        raise _refusal(str(error)) from None
    return TransientPreparation(glossary, request, processing_dir)


def _refusal(code):
    return PreflightError("The transient dry run could not read local evidence.",
                          "Check the project catalog and the v2 cache; no execution state was written.", detail=code)


def _authorized_project(prepared):
    repository = ProjectJsonRepository(prepared.processing_dir)
    catalog = repository.read_catalog()
    if catalog.failures:
        raise _refusal(catalog.failures[0])
    project = select_project(catalog.projects, prepared.request)
    if project is not None:
        current = repository.load_read_only(project.project_id)
        if current != project or not current.matches(prepared.request):
            raise ProjectSourceError("project_changed_during_read")
    return project


def translate(book, hooks, label, *, formats, cache_path):
    prepared = book.prepare()
    try:
        project = _authorized_project(prepared)
        reader, resolver = None, None
        if project is not None:
            reader = ReadOnlyExactCache(cache_path)
            intent = LocalCachePlanIntent(book.options.source, book.options.target,
                replace(LIBRETRANSLATE_PRODUCER, model=declared_model(label)), book.options.chunk_limit,
                glossary_ref=effective_glossary_reference(prepared.glossary))
            resolver = LocalCachePlanResolver(reader, CacheAuthorizationPolicy(project.project_id).resolve(), intent)
        units = TransientUnits(book.options.language_pair,
            lambda text: apply_glossary_with_usage(text, prepared.glossary), review_text_from_template, reader=reader, resolver=resolver,
            cache_only=book.options.cache_only, chunk_limit=book.options.chunk_limit)
        service = TransientTranslation(formats, units,
            observer=EngineObserver(hooks.on_chapter_start, hooks.on_chapter_done, hooks.on_text_processed),
            cancellation=book.cancellation)
        package = read_package(book.epub_path)
        result = service.execute(TransientExecution(LocalFormatSource(book.epub_path), book.options,
            package.facts, None if project is None else project.project_id))
        size, digest = source_digest(book.epub_path)
        if (size, digest) != (prepared.request.source_size, prepared.request.source_sha256):
            raise ProjectSourceError("source_changed_during_read")
        report = to_report(result, book.epub_path, book.output_plan.path, package, book.options, prepared.glossary)
        report.execution_note = (f"{book.options.workers} workers configured; sequential local analysis. "
                                 "Only existing matching project evidence can supply exact cache hits; no network or cache warming.")
        return report
    except CacheSchemaError as error:
        if error.code == "cache_busy":
            raise PreflightError("The v2 cache is busy.", "Wait for the cache writer to finish and retry.",
                                 detail="cache_busy") from None
        raise _refusal(error.code) from None
    except (CacheIdentityError, ProjectError) as error:
        raise _refusal(str(error)) from None
    except TranslationExecutionError as error:
        raise _refusal(error.failure.code) from None
