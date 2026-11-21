"""Wire labeled transient dry runs and previews before any durable execution.

The catalog grants only an existing matching project's exact cache scope. Missing
catalogs grant nothing; unreadable, incomplete or ambiguous catalogs fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ...formats.local import LocalFormatSource
from ...formats.epub.reader import read_package
from ...formats.epub import review_text_from_template
from ...formats.epub.io import preview_documents
from ...knowledge.cache import CacheAuthorizationPolicy, CacheIdentityError
from ...knowledge.cache.read_only import ReadOnlyExactCache
from ...knowledge.cache.schema import CacheSchemaError
from ...knowledge.glossary import apply_glossary_with_usage, effective_glossary_reference
from ...projects import ProjectError, ProjectSourceError, select_project
from ...projects.store import ProjectJsonRepository, build_project_request, translation_selections, source_digest
from ...provenance import declared_model
from ...providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ...providers.libretranslate.client import RoutedTranslator
from ...providers import Capability, ProviderRequirements, ProviderRoute
from ...translation import (
    EngineObserver, LocalCachePlanIntent, LocalCachePlanResolver, PreflightError,
    TransientExecution, TransientTranslation, TransientUnits, TranslationExecutionError, to_report,
    TransientCache, TransientProvider, resolve_plan,
)
from .rich_document_executor import map_offline_execution
from .transient_publication import LocalTransientPublication


@dataclass(frozen=True, repr=False)
class TransientPreparation:
    glossary: object
    request: object
    processing_dir: object
    route: object | None = None
    network_decision: object | None = None
    online: object | None = None


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


def translate(book, hooks, label, *, formats, cache_path, estimate=lambda glossary: None):
    """A dry run, or a preview. ``estimate`` gives a model translator's usage estimate (#380): that dry run reads
    no cache, since its plan needs the model's identity, so it estimates as if nothing were cached."""
    if getattr(book, "preview", False):
        return _preview(book, hooks, label, formats=formats, cache_path=cache_path)
    prepared = book.prepare()
    estimator = estimate(prepared.glossary)
    try:
        project = _authorized_project(prepared)
        reader, resolver = None, None
        if project is not None and estimator is None:
            reader = ReadOnlyExactCache(cache_path)
            intent = LocalCachePlanIntent(book.options.source, book.options.target,
                replace(LIBRETRANSLATE_PRODUCER, model=declared_model(label)), book.options.chunk_limit,
                glossary_ref=effective_glossary_reference(prepared.glossary))
            resolver = LocalCachePlanResolver(reader, CacheAuthorizationPolicy(project.project_id).resolve(), intent)
        units = TransientUnits(book.options.language_pair,
            lambda text: apply_glossary_with_usage(text, prepared.glossary), review_text_from_template, reader=reader, resolver=resolver,
            cache_only=book.options.cache_only, chunk_limit=book.options.chunk_limit,
            on_estimate=None if estimator is None else estimator.add)
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
        if estimator is not None:
            report.usage_estimate = estimator.summary()
            report.execution_note = (f"{book.options.workers} workers configured; sequential local analysis. "
                                     "Nothing was sent; the estimate assumes no text is cached.")
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


def prepare_preview(*, label, options, processing_dir, preflight, rich_preflight, on_route, defer=False, **configuration):
    """Select before any resource; the unlabeled callback receives its original arguments."""
    if defer:
        def prepare_selected():
            return prepare_preview(label=label, options=options, processing_dir=processing_dir,
                                   preflight=preflight, rich_preflight=rich_preflight, on_route=on_route, **configuration)
        if label is None:
            prepared = prepare_selected()
            return lambda: prepared
        return prepare_selected
    if label is None:
        prepared = preflight(**configuration)
    else:
        prepared = rich_preflight(**configuration, cache_check=lambda _: None, close_on_error=True)
    on_route(prepared)
    if label is None:
        return prepared
    try:
        local = prepare(epub_path=configuration["epub_path"], options=options, glossary=prepared.glossary,
            glossary_path=configuration["glossary_path"], translator_name=configuration["translator_name"],
            url=configuration["url"], processing_dir=processing_dir, profile_name=None)
        return replace(local, online=prepared, route=prepared.route, network_decision=prepared.network_decision)
    except BaseException:
        _base_translator(prepared).close()
        raise


def _base_translator(prepared):
    translator = prepared.translator
    return translator._base if isinstance(translator, RoutedTranslator) else translator


def _preview(book, hooks, label, *, formats, cache_path):
    prepared = book.prepare()
    base = _base_translator(prepared.online)
    overlay = None
    try:
        project = _authorized_project(prepared)
        options = book.options
        producer = replace(LIBRETRANSLATE_PRODUCER, model=declared_model(label))
        route = prepared.online.route
        plan = resolve_plan(libretranslate_snapshot(base, datetime.now(UTC)),
            ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT)), producer,
            ProviderRoute(options.source, options.target, None if route is None else route.intermediate),
            chunk_limit=options.chunk_limit, glossary_ref=effective_glossary_reference(prepared.glossary))
        overlay = TransientCache(plan, reader=None if project is None else ReadOnlyExactCache(cache_path),
            scope=None if project is None else CacheAuthorizationPolicy(project.project_id).resolve())
        provider = TransientProvider(LibreTranslateProvider(prepared.online.translator, producer=producer), plan,
                                     cancellation=book.cancellation)
        units = TransientUnits(options.language_pair, lambda text: apply_glossary_with_usage(text, prepared.glossary),
            review_text_from_template, provider=provider, exact_cache=overlay, chunk_limit=options.chunk_limit,
            map_execution=map_offline_execution)
        publication = LocalTransientPublication(book.output_plan.path, overwrite=book.replace_existing,
            before_publish=lambda: _check_source(book, prepared))
        service = TransientTranslation(formats, units, publication=publication, cancellation=book.cancellation,
            observer=EngineObserver(hooks.on_chapter_start, hooks.on_chapter_done, hooks.on_text_processed))
        package = read_package(book.epub_path)
        selection = preview_documents(book.epub_path, translate_metadata=options.translate_metadata,
            max_documents=options.max_documents, selection=options.chapter_selection)
        result = service.execute(TransientExecution(LocalFormatSource(book.epub_path), options, package.facts,
            None if project is None else project.project_id, tuple(item.archive_path for item in selection)))
        return to_report(result, book.epub_path, book.output_plan.path, package, options, prepared.glossary)
    except PreflightError as error:
        raise _preview_refusal(error.detail) from None
    except (CacheIdentityError, ProjectError) as error:
        raise _preview_refusal(str(error)) from None
    except CacheSchemaError as error:
        raise _preview_refusal(error.code) from None
    except TranslationExecutionError as error:
        raise _preview_refusal(error.failure.code) from None
    finally:
        if overlay is not None:
            overlay.clear()
        base.close()


def _check_source(book, prepared):
    size, digest = source_digest(book.epub_path)
    if (size, digest) != (prepared.request.source_size, prepared.request.source_sha256):
        raise ProjectSourceError("source_changed_during_read")


def _preview_refusal(code):
    return PreflightError("The rich preview could not be completed.",
                          "Check the provider and local evidence; the destination was not changed.", detail=code)
