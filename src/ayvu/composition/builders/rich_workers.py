"""Build task-owned offline resources without migrating or sharing SQLite connections.

The coordinator has already opened the project database. Repository constructors
below only bind that prepared database; every operation opens its own connection.
Online workers each own a translator from the prepared factory, which shares the
book's single rate limiter, plus their own provider, artifact and provenance
adapters. They share the prepared exact cache through ArbitratedExactCache, so a
repeated key is produced once per invocation.
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from dataclasses import replace

from ...formats.epub import review_text_from_template
from ...jobs import JobState
from ...kernel import JobFailure, JobFailureFamily
from ...knowledge.cache.read_only import ReadOnlyExactCache
from ...knowledge.memory.approved import MemoryScope, ScopeKind
from ...knowledge.memory.approved.store import open_approved_memory
from ...translation.stages import MemoryRetrieval, RetrievalPolicy, LegacyCacheCompatibility
from ...artifacts import ArtifactService
from ...artifacts.store import LocalArtifactPayloads, SqliteArtifactRepository
from ...providers.libretranslate.adapter import LibreTranslateProvider
from ...providers.libretranslate.client import LibreTranslateTranslator, RoutedTranslator
from ...knowledge.glossary import apply_glossary_with_usage, effective_glossary_reference
from ...projects.schema import project_migration_runner
from ...provenance.store import SqliteProvenanceRepository
from ...translation import (
    ArbitratedExactCache, CacheOnlyDocumentExecutor, DocumentTranslator, DocumentWork, LocalCachePlanIntent,
    TranslationResources, TranslationExecutionError, TranslationCancelled,
)
from .rich_document_executor import RichDocumentExecutor, map_offline_execution

# Workers and the coordinator write one project database at once. On a slow disk the default
# half second is shorter than the queue, so every party waits up to the runner's maximum.
WORKER_BUSY_TIMEOUT = 2.0



class _SerializedWrites:
    """Worker repositories take turns to write, waiting in-process without a deadline.

    SQLite then sees at most one worker at a time, next to the coordinator, so its
    busy timeout covers one write rather than the whole queue. Reads stay concurrent.
    """

    def __init__(self) -> None:
        self._runner, self._lock = project_migration_runner(busy_timeout=WORKER_BUSY_TIMEOUT), Lock()

    def connect(self, database, *, readonly: bool):
        if readonly:
            return self._runner.connect(database, readonly=True)
        self._lock.acquire()
        try:
            return _ReleasingConnection(self._runner.connect(database, readonly=False), self._lock)
        except BaseException:
            self._lock.release()
            raise


class _ReleasingConnection:
    """Delegates to the SQLite connection and hands the write turn on when it closes."""

    def __init__(self, connection, lock) -> None:
        self._connection, self._lock = connection, lock

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            lock, self._lock = self._lock, None
            if lock is not None:
                lock.release()

def project_store_options(translation) -> dict:
    """Coordinator stores wait like the workers whenever several labeled workers share the database."""
    if translation.model_label is not None and translation.options.workers > 1:
        return {"busy_timeout": WORKER_BUSY_TIMEOUT}
    return {}


class WorkerAuthority:
    def __init__(self, context, clock):
        self.context, self.clock = context, clock

    def check(self):
        context = self.context
        if context.cancellation is not None and context.cancellation.token.checkpoint("pipeline_before_unit") is not None:
            raise TranslationCancelled()
        job = context.current_job() if context.current_job is not None else context.job
        if (context.lease_lost() or job.state is not JobState.RUNNING or job.lease is None
                or context.job.lease is None or job.job_id != context.job.job_id
                or job.request.project_id != context.job.request.project_id
                or job.lease.owner_token != context.job.lease.owner_token or job.lease.expired(self.clock())):
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "lease_not_held"))


class WorkerProvenance:
    """Checks the job's authority before each write; reads, such as resume's replay, pass through."""

    def __init__(self, repository, check):
        self.repository, self.check = repository, check

    def append(self, request):
        self.check()
        return self.repository.append(request)

    def __getattr__(self, name):
        return getattr(self.repository, name)


def offline_work(runtime, context):
    options, prepared = runtime._options, runtime._prepared
    intent = LocalCachePlanIntent(options.source, options.target, runtime.producer, options.chunk_limit,
        glossary_ref=effective_glossary_reference(prepared.glossary))
    authority, runner = WorkerAuthority(context, runtime._clock), _SerializedWrites()

    @contextmanager
    def factory(events, check):
        check()
        reader = ReadOnlyExactCache(runtime._cache_path)
        repository = SqliteProvenanceRepository(runner, runtime._database,
            runtime._project_id, clock=runtime._clock)
        yield CacheOnlyDocumentExecutor(reader, intent, replace(context, events=events),
            WorkerProvenance(repository, check), review_text_from_template,
            lambda text: apply_glossary_with_usage(text, prepared.glossary), clock=runtime._clock,
            checkpoint=check, begin_unit=events.begin_unit, map_execution=map_offline_execution)

    return TranslationResources(None, documents=DocumentWork(factory, context, checkpoint=authority.check))


def online_work(runtime, context, plan, provider, artifacts, provenance, memory_policy):
    """Adapt the existing single online worker to the complete document port."""
    options = runtime._options
    retrieval, legacy, policy = None, None, RetrievalPolicy()
    if options.translation_memory is not None:
        policy = memory_policy(options.translation_memory)
        scope = MemoryScope(ScopeKind.PROJECT, runtime._project_id)
        repository = open_approved_memory(runtime._database.parent, allowed_scopes=frozenset({scope}),
            provenance={runtime._project_id: provenance})
        retrieval = MemoryRetrieval(repository, policy)
        if policy.legacy_cache_enabled:
            legacy = LegacyCacheCompatibility(runtime._cache, policy,
                apply_threshold=options.translation_memory.apply_threshold)

    if options.workers > 1:
        return _parallel_online(runtime, context, plan)

    @contextmanager
    def factory(events, check):
        translator = DocumentTranslator(plan, provider,
            lambda text: apply_glossary_with_usage(text, runtime._prepared.glossary), artifacts, provenance, events,
            exact_cache=runtime._cache, approved_memory=retrieval, memory=legacy, retrieval_policy=policy,
            clock=runtime._clock, translation_context=runtime._translation_context)
        yield RichDocumentExecutor(translator, context)

    return TranslationResources(None, documents=DocumentWork(factory, context))


def _parallel_online(runtime, context, plan):
    """Workers > 1 run without governed memory, which check_rich_options refuses earlier."""
    factory_of = getattr(runtime._prepared, "translator_factory", None)
    if factory_of is None:
        raise TranslationExecutionError(JobFailure(JobFailureFamily.RESOURCE, "worker_factory_unavailable"))
    cache, authority = ArbitratedExactCache(runtime._cache), WorkerAuthority(context, runtime._clock)
    runner = _SerializedWrites()

    @contextmanager
    def factory(events, check):
        check()
        translator = factory_of()
        base = translator._base if isinstance(translator, RoutedTranslator) else translator
        provider = LibreTranslateProvider(translator, producer=plan.producer, clock=runtime._clock,
            cleanup=base.close if isinstance(base, LibreTranslateTranslator) else None)
        try:
            artifacts = ArtifactService(SqliteArtifactRepository(runner, runtime._database, runtime._project_id,
                clock=runtime._clock), LocalArtifactPayloads(runtime._artifact_root))
            provenance = SqliteProvenanceRepository(runner, runtime._database, runtime._project_id, clock=runtime._clock)
            translator_of_document = DocumentTranslator(plan, provider,
                lambda text: apply_glossary_with_usage(text, runtime._prepared.glossary), artifacts,
                WorkerProvenance(provenance, check), events, exact_cache=cache, clock=runtime._clock,
                translation_context=runtime._translation_context)
            with cache.worker(check):
                yield RichDocumentExecutor(translator_of_document, replace(context, events=events))
        finally:
            provider.close()

    return TranslationResources(None, documents=DocumentWork(factory, context, checkpoint=authority.check))
