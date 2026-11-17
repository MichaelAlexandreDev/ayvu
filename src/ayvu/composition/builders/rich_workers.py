"""Build task-owned offline resources without migrating or sharing SQLite connections.

The coordinator has already opened the project database. Repository constructors
below only bind that prepared database; every operation opens its own connection.
Online workers each own a translator from the prepared factory, which shares the
book's single rate limiter, plus their own provider, artifact and provenance
adapters. They share the prepared exact cache through ArbitratedExactCache, so a
repeated key is produced once per invocation.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from threading import Lock
from weakref import WeakValueDictionary
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
from ...knowledge.glossary import apply_glossary_with_usage, effective_glossary_reference
from ...projects.schema import ProjectDatabaseTurns, project_migration_runner
from ...provenance.store import SqliteProvenanceRepository
from ...translation import (
    ArbitratedExactCache, CacheOnlyDocumentExecutor, DocumentTranslator, DocumentWork, LocalCachePlanIntent,
    TranslationResources, TranslationExecutionError, TranslationCancelled,
)
from .rich_document_executor import RichDocumentExecutor, map_offline_execution

# Workers and the coordinator share one project database. Their connections take turns
# in-process (``projects.turns``), one turn per run, so SQLite only sees other processes;
# for those, every party waits up to the runner's maximum instead of half a second.
WORKER_BUSY_TIMEOUT = 2.0

# The turns of each project database, held weakly: alive while any store of a run uses them,
# so the coordinator's stores and the workers' of one run share them.
_DATABASE_TURNS: WeakValueDictionary = WeakValueDictionary()
_DATABASE_TURNS_GUARD = Lock()


def _run_turns(runtime) -> ProjectDatabaseTurns:
    key = os.path.abspath(runtime._database)
    with _DATABASE_TURNS_GUARD:
        turns = _DATABASE_TURNS.get(key)
        if turns is None:
            turns = _DATABASE_TURNS[key] = ProjectDatabaseTurns()
        return turns


def _worker_runner(runtime):
    return project_migration_runner(busy_timeout=WORKER_BUSY_TIMEOUT, turns=_run_turns(runtime))


def project_store_options(translation, runtime) -> dict:
    """Coordinator stores take the run's turns whenever several labeled workers share the database."""
    if translation.model_label is not None and translation.options.workers > 1:
        return {"busy_timeout": WORKER_BUSY_TIMEOUT, "turns": _run_turns(runtime)}
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
    authority, runner = WorkerAuthority(context, runtime._clock), _worker_runner(runtime)

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
    runner = _worker_runner(runtime)

    @contextmanager
    def factory(events, check):
        check()
        provider = runtime.binding.provider(factory_of(), plan.producer, runtime._prepared.glossary)
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
