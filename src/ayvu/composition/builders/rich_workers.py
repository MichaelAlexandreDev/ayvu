"""Build task-owned offline resources without migrating or sharing SQLite connections.

The coordinator has already opened the project database. Repository constructors
below only bind that prepared database; every operation opens its own connection.
Online execution remains a single coordinator worker until REF-18 batch 5.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from ...formats.epub import review_text_from_template
from ...jobs import JobState
from ...kernel import JobFailure, JobFailureFamily
from ...knowledge.cache.read_only import ReadOnlyExactCache
from ...knowledge.memory.approved import MemoryScope, ScopeKind
from ...knowledge.memory.approved.store import open_approved_memory
from ...translation.stages import MemoryRetrieval, RetrievalPolicy, LegacyCacheCompatibility
from ...knowledge.glossary import apply_glossary_with_usage, effective_glossary_reference
from ...projects.schema import project_migration_runner
from ...provenance.store import SqliteProvenanceRepository
from ...translation import (
    CacheOnlyDocumentExecutor, DocumentTranslator, DocumentWork, LocalCachePlanIntent,
    TranslationResources, TranslationExecutionError, TranslationCancelled,
)
from .rich_document_executor import RichDocumentExecutor, map_offline_execution


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
    def __init__(self, repository, check):
        self.repository, self.check = repository, check

    def append(self, request):
        self.check()
        return self.repository.append(request)


def offline_work(runtime, context):
    options, prepared = runtime._options, runtime._prepared
    intent = LocalCachePlanIntent(options.source, options.target, runtime.producer, options.chunk_limit,
        glossary_ref=effective_glossary_reference(prepared.glossary))
    authority = WorkerAuthority(context, runtime._clock)

    @contextmanager
    def factory(events, check):
        check()
        reader = ReadOnlyExactCache(runtime._cache_path)
        repository = SqliteProvenanceRepository(project_migration_runner(), runtime._database,
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

    @contextmanager
    def factory(events, check):
        translator = DocumentTranslator(plan, provider,
            lambda text: apply_glossary_with_usage(text, runtime._prepared.glossary), artifacts, provenance, events,
            exact_cache=runtime._cache, approved_memory=retrieval, memory=legacy, retrieval_policy=policy,
            clock=runtime._clock, translation_context=runtime._translation_context)
        yield RichDocumentExecutor(translator, context)

    return TranslationResources(None, documents=DocumentWork(factory, context))
