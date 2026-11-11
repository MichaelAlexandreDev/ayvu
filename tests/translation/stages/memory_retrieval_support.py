from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json
import logging
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
import pytest
from ayvu.knowledge.memory.approved.model import (
    Candidate, MAX_VALUE_BYTES, MemoryQuery, MemoryScope, MemoryScopeError, MemorySource, MemoryState,
    MemoryValidationError, PipelineStage, RetrievalLimits, RetrievalMode, ScopeKind, TargetValue, ValueKind,
)
from ayvu.artifacts.service import ArtifactService
from ayvu.artifacts.store import LocalArtifactPayloads, SqliteArtifactRepository
from ayvu.knowledge.cache import (CacheAuthorizationPolicy, CacheProvenanceMetadata, ExactCacheEntry,
                                  TRANSLATION_STAGE_AGGREGATE, exact_entry_key)
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.content.model import ProtectedPart
from ayvu.jobs.model import JobState
from ayvu.jobs.events import encode_event
from ayvu.jobs.execution import InMemoryJobEventLog
from ayvu.translation.stages.memory_retrieval import (
    LegacyCacheCompatibility, MemoryRetrieval, RetrievalPolicy, RetrievalSource,
    plan_memory_pin, similarity, snippet, structural_compatibility,
)
from ayvu.projects.schema import project_migration_runner
from ayvu.providers.ports import ProviderRoute
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from tests.support.approved_memory_fixtures import MemoryEnvironment, NOW, PAIR, PIN, PROJECT_SCOPE, SCOPES, WORK_SCOPE, final, source
from tests.support.content_fixtures import PROJECT, epub_document, unit
from tests.support.translation_cache_v2_support import Provider, make_plan
from ayvu.kernel import LanguagePair, TranslationMemoryOptions, TranslationOptions

@pytest.fixture
def env(tmp_path):
    return MemoryEnvironment(tmp_path)


def query(**changes):
    return MemoryQuery(**dict(text='Café  Hello', languages=PAIR, scopes=(PROJECT_SCOPE,), pin=PIN) | changes)


def approved(env, text='Café  Hello', target='Olá', **kwargs):
    return env.approve(env.create(src=source(text), target=final(target), **kwargs))


class Pipeline:
    """All production local adapters, including artifacts/provenance/jobs/events."""
    def __init__(self, env, tmp_path, *, policy=RetrievalPolicy(), glossary=None):
        self.env, self.plan = env, make_plan(route=ProviderRoute('en', 'pt-br'))
        self.cache = SQLiteExactCache(tmp_path / 'cache-v2.sqlite', clock=lambda: NOW)
        self.artifacts = ArtifactService(SqliteArtifactRepository(project_migration_runner(), env.project_database,
            PROJECT, clock=lambda: NOW), LocalArtifactPayloads(tmp_path / 'artifacts'))
        job = env.job
        for state in (JobState.PREFLIGHT, JobState.RUNNING):
            job = env.jobs.transition(job.job_id, state, expected_revision=job.revision)
        self.job = env.jobs.acquire_lease(job.job_id, timedelta(minutes=30))
        self.events = InMemoryJobEventLog(job.job_id)
        self.policy, self.glossary = policy, glossary or (lambda text: SimpleNamespace(text=text + '!', usage=None))
        self.provider = Provider(self.plan, respond=lambda text: 'máquina')
        self.sequence, self.runs = 1, 0

    def approve(self, document, index=0, target='revista!', **changes):
        value = Candidate(MemorySource.from_document(document, document.units[index].unit_id), PAIR,
            final(target, pin=plan_memory_pin(self.plan)), PROJECT_SCOPE, str(uuid4()))
        return self.env.approve(self.env.create(value=replace(value, **changes)))

    def run(self, document, *, memory=True, legacy=None):
        self.runs += 1
        translator = DocumentTranslator(self.plan, self.provider, self.glossary, self.artifacts,
            self.env.provenance, self.events, exact_cache=self.cache,
            approved_memory=MemoryRetrieval(self.env.repo, self.policy) if memory else None,
            memory=legacy, retrieval_policy=self.policy, clock=lambda: NOW)
        context = ExecutionContext(self.job, self.job.lease.owner_token, first_sequence=self.sequence,
            progress_phase=f'run_{self.runs}')
        result = translator.translate(document, context, replay_raw_results=False)
        self.sequence = result.next_sequence
        return result


def doc(*texts):
    return epub_document(units=[unit(f'body/p[{i}]', text, order=(i,)) for i, text in enumerate(texts)])
