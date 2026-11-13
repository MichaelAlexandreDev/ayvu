"""Real cache stores, instrumented transport and offline guards for G7."""
from contextlib import contextmanager
from dataclasses import replace

import requests

from ayvu.kernel import LanguagePair, TranslationOptions

from ayvu.composition.builders.translate_project import epub_format_service
from ayvu.formats import ContentScope, ExtractionRequest
from ayvu.formats.epub import review_text_from_template
from ayvu.formats.local import LocalFormatSource
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.knowledge.glossary import apply_glossary_with_usage
from ayvu.providers import Capability, ProviderRequirements
from ayvu.providers.libretranslate.adapter import LibreTranslateProvider, libretranslate_snapshot, LIBRETRANSLATE_PRODUCER
from ayvu.providers.libretranslate.client import LibreTranslateTranslator
from ayvu.translation import CacheOnlyDocumentExecutor, DocumentTranslator, ExecutionContext, LocalCachePlanIntent, RuntimeContext, resolve_plan
from tests.support.cli_rich_runtime_support import Transport, text, config, isolate, records
from tests.support.cli_translate_project_support import _args, _book, _jobs, runner, app
from tests.support.transient_translation_support import prepared, exact_entry, forbidden
from tests.support.translation_cache_v2_support import Env, NOW, document, make_plan, Glossed


@contextmanager
def offline_guard(monkeypatch):
    import socket
    with monkeypatch.context() as patch:
        for name in ("getaddrinfo", "gethostbyname", "create_connection"):
            patch.setattr(socket, name, forbidden)
        for name in ("connect", "connect_ex", "sendto", "send", "sendall"):
            patch.setattr(socket.socket, name, forbidden)
        patch.setattr(requests, "Session", forbidden)
        for name in (
            "ayvu.providers.libretranslate.client.LibreTranslateTranslator.__init__",
            "ayvu.providers.libretranslate.client.create_translator",
            "ayvu.providers.libretranslate.adapter.LibreTranslateProvider.__init__",
            "ayvu.providers.libretranslate.adapter.LibreTranslateProviderFactory.__init__",
            "ayvu.providers.libretranslate.adapter.LibreTranslateDiscoveryAdapter.__init__",
            "ayvu.providers.discovery.ProviderDiscoveryService.__init__",
            "ayvu.translation.preflight.create_translator",
            "ayvu.translation.preflight.CliProviderSession",
            "ayvu.translation.DocumentTranslator.__init__",
            "ayvu.translation.document_replay._replay",
            "ayvu.translation.document.DocumentTranslator._replay",
            "ayvu.translation.stages.MemoryRetrieval.__init__",
            "ayvu.translation.stages.LegacyCacheCompatibility.__init__",
            "ayvu.composition.builders.translate_project.open_approved_memory",
            "ayvu.knowledge.cache.repository.SQLiteExactCache.__init__",
            "ayvu.knowledge.cache.repository.SQLiteExactCache.put_if_absent",
            "ayvu.composition.cli.TranslationCache", "ayvu.composition.cli.verify_cache_writable",
        ):
            patch.setattr(name, forbidden)
        yield


def warm_units(path, documents, *, plan=None, glossary=None):
    """Warm through the real provider adapter and linguistic pipeline, without sockets."""
    transport = Transport()
    session_factory = requests.Session
    requests.Session = lambda: transport
    try:
        translator = LibreTranslateTranslator(retries=0)
        translator.session = transport
    finally:
        requests.Session = session_factory
    producer = replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026")
    plan = plan or resolve_plan(libretranslate_snapshot(translator, NOW),
        ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT)), producer,
        make_plan().route, chunk_limit=3000)
    env = Env(plan)
    project_id = documents[0].project_id
    env.job = replace(env.job, request=replace(env.job.request, project_id=project_id))
    cache = SQLiteExactCache(path)
    provider = LibreTranslateProvider(translator, producer=plan.producer, clock=lambda: NOW)
    service = DocumentTranslator(plan, provider, lambda value: apply_glossary_with_usage(value, glossary),
        env.artifacts, env.provenance, env.events, exact_cache=cache, clock=lambda: NOW)
    try:
        for doc in documents:
            service.translate(doc, ExecutionContext(env.job, env.job.lease.owner_token))
    finally:
        translator.close()
    assert transport.posts
    return plan, env, transport


def seed(root, source, options, *, warm=True, glossary=None):
    from ayvu.projects.store import ProjectJsonRepository
    from ayvu.composition.builders.translate_project import v2_cache_path
    prep = prepared(root, source, options, glossary=glossary)
    project = ProjectJsonRepository(root / "processing").create(prep.request)
    scopes = {ContentScope.BODY}
    if options.translate_metadata:
        scopes.add(ContentScope.METADATA)
    if options.translate_alt_text:
        scopes.add(ContentScope.IMAGE_ALT_TEXT)
    extracted = epub_format_service().extract("epub", LocalFormatSource(source),
        ExtractionRequest(project.project_id, frozenset(scopes)))
    assert extracted.failure is None
    cache = v2_cache_path(root / "cache.sqlite")
    if warm:
        warm_units(cache, extracted.documents, glossary=prep.glossary)
    return project, cache, extracted.documents


def executor(path, plan, env, *, glossary=None, readable=review_text_from_template, **kwargs):
    intent = LocalCachePlanIntent(plan.route.source, plan.route.target, plan.producer, plan.chunk_limit)
    return CacheOnlyDocumentExecutor(ReadOnlyExactCache(path), intent, RuntimeContext(env.job, None, env.events),
        env.provenance, readable, glossary or (lambda value: Glossed(value)), clock=lambda: NOW, **kwargs)


def invoke(root, source, *args):
    return runner.invoke(app, _args(root, source, "--cache-only", *args))


def ready_publisher(artifacts):
    """Capture actual format output with a ready artifact, without a durable database."""
    import hashlib
    from ayvu.artifacts import ArtifactState
    original = artifacts.publish
    def publish(*args, **kwargs):
        artifact = original(*args, **kwargs)
        payload = artifacts.calls[-1][1]
        return replace(artifact, state=ArtifactState.READY, size=len(payload),
            digest="sha256:" + hashlib.sha256(payload).hexdigest())
    artifacts.publish = publish
    return artifacts


def options(**changes):
    return TranslationOptions(LanguagePair("en", "pt"), cache_only=True, **changes)
