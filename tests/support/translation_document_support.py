from __future__ import annotations
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import inspect
import json
import logging
import sqlite3
from uuid import UUID
import pytest
import requests
from ayvu.artifacts.model import ArtifactState
from ayvu.artifacts.service import ARTIFACT_SAFE_POINTS
from ayvu.content.model import (
    AdapterIdentity, Constraint, Locator, ProtectedPart, SemanticKind, SourceLocale, UnitContent, UNKNOWN_LOCALE, LocaleState,
    build_document,
)
from ayvu.jobs.model import JobRequest, JobState
from ayvu.jobs.events import JobEventKind, encode_event
from ayvu.jobs.execution import CancellationSource, InMemoryJobEventLog
from ayvu.projects.schema import open_artifact_service, open_job_repository, open_provenance_repository
from ayvu.provenance.model import SourceKind, UNKNOWN, ProducerIdentity
from ayvu.providers.ports import DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderResult
from ayvu.providers.capabilities import Capability, ProviderRequirements
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ayvu.providers.service import failed_result
from ayvu.translation.document_contracts import (
    PIPELINE_SAFE_POINTS, SAFE_POINTS, ExecutionContext, TranslationCancelled, TranslationExecutionError,
    UnitSource,
)
from ayvu.translation.raw_results import RAW_RESULTS_ROLE
from ayvu.translation.document import DocumentTranslator
from ayvu.knowledge.memory.fuzzy import TranslationMemoryMatch
from ayvu.translation.stages.memory_retrieval import RetrievalPolicy
from ayvu.knowledge.cache.compatible_memory import CacheCompatibilityIdentity, CompatibleMemoryCandidate
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy
from ayvu.knowledge.cache.cache_only import CacheOnlyRequest
from ayvu.knowledge.cache.entries import (
    CacheLookupState, CacheMissReason, CacheProvenanceMetadata, ExactCacheEntry, ExactCacheLookup,
    PutIfAbsentResult, PutIfAbsentState,
)
from ayvu.knowledge.cache.identity_values import CacheValueKind, TRANSLATION_STAGE_AGGREGATE
from ayvu.translation.plan import ResolvedTranslationPlan, resolve_plan
from ayvu.providers.libretranslate.client import LibreTranslateTranslator, RoutedTranslator, TranslationRoute
from tests.support.provider_fakes import FakeSession, http_response
from ayvu.kernel import LanguagePair, JobFailureFamily, JobWarning, JobCancellationError, JobFailure

PROJECT = str(UUID(int=0x1234))


NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


PRODUCER = ProducerIdentity('engine@1', 'model@1', 'adapter@1', 'not_applicable')


ROUTE = ProviderRoute('en', 'pt')


GLOSSARY_A = 'glossary:' + 'a' * 64


GLOSSARY_B = 'glossary:' + 'b' * 64


ENGLISH = SourceLocale(LocaleState.EXPLICIT, 'en')


ADAPTER = AdapterIdentity('subtitle', 1, 1)


CANARIES = ('Call me Ishmael.', 'SECRET-GLOSSARY-VALUE')


def plan(**changes):
    values = {'producer': PRODUCER, 'route': ROUTE, 'capabilities': 'capfp:v1:' + '1' * 64, 'chunk_limit': 3000, 'glossary_ref': GLOSSARY_A, **changes}
    return ResolvedTranslationPlan(**values)


def locator(value):
    return Locator('webvtt_cue', 1, value)


def cue(value, text, order, **changes):
    return UnitContent(locator(value), text, SemanticKind('subtitle_cue'), (order,), **changes)


def document(*units, key='episode-1.vtt'):
    return build_document(PROJECT, key, ADAPTER, ENGLISH, units or (cue('a', ' Hello there ', 1), cue('b', 'Good night', 2)))


@dataclass
class Glossed:
    text: str
    usage: object = 'usage'


def upper_glossary(text):
    return Glossed(text.upper())


class Provider:
    def __init__(self, respond=None, producer=PRODUCER, route=ROUTE, warnings=()):
        self.respond = respond or (lambda text: f'[{text}]')
        self.producer, self.route, self.warnings = producer, route, warnings
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return TranslationProviderResult(request.request_id, ProviderStatus.SUCCEEDED, self.producer, DeliveryState.COMPLETED,
                                         text=self.respond(request.text), route=self.route, warnings=self.warnings, usage=ProviderUsage())

    def close(self):
        pass


class PlanCache:
    def __init__(self):
        self.stored = {}
        self.calls = []

    def lookup(self, key):
        self.calls.append(('lookup', key))
        entry = self.stored.get(key)
        if entry is None:
            return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)
        return ExactCacheLookup(CacheLookupState.HIT, value=entry.value)

    def resolve_compatible(self, scope, value_kind, source_digest, *, limit):
        self.calls.append(('resolve', scope, value_kind, source_digest, limit))
        return tuple(key for key in self.stored if key.scope == scope and key.value_kind is value_kind
                     and key.source_content_digest == source_digest)

    def put_if_absent(self, entry):
        self.calls.append(('put_if_absent', entry.key))
        winner = self.stored.get(entry.key)
        if winner is None:
            self.stored[entry.key] = entry
            return PutIfAbsentResult(PutIfAbsentState.INSERTED)
        if winner.value == entry.value and winner.provenance == entry.provenance:
            return PutIfAbsentResult(PutIfAbsentState.IDEMPOTENT)
        return PutIfAbsentResult(PutIfAbsentState.CONFLICT)


class Env:
    def __init__(self, tmp_path):
        self.database, self.backups, self.root = tmp_path / 'project.sqlite', tmp_path / 'backups', tmp_path / 'artifacts'
        self.now = NOW
        self.count = 5000
        self.jobs = open_job_repository(self.database, self.backups, PROJECT, clock=self.clock)
        self.artifacts = open_artifact_service(self.database, self.backups, self.root, PROJECT, clock=self.clock, new_id=self.new_id)
        self.provenance = open_provenance_repository(self.database, self.backups, PROJECT, clock=self.clock, new_id=self.new_id)
        self.job = self.running_job()
        self.log = InMemoryJobEventLog(self.job.job_id)
        self.sequence = 1
        self.runs = 0

    def clock(self):
        return self.now

    def new_id(self):
        self.count += 1
        return str(UUID(int=self.count))

    def running_job(self):
        job = self.jobs.create(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'e' * 64))
        for state in (JobState.PREFLIGHT, JobState.RUNNING):
            job = self.jobs.transition(job.job_id, state, expected_revision=job.revision)
        return self.jobs.acquire_lease(job.job_id, timedelta(minutes=30))

    def context(self, **changes):
        # A phase cannot return in one event stream, so each run in these tests gets its own.
        self.runs += 1
        return ExecutionContext(**{'job': self.job, 'owner_token': self.job.lease.owner_token, 'first_sequence': self.sequence,
                                   'progress_phase': f'run_{self.runs}', **changes})

    def translator(self, provider=None, subject_plan=None, glossary=upper_glossary, **options):
        return DocumentTranslator(subject_plan or plan(), provider or Provider(), glossary, self.artifacts, self.provenance, self.log,
                                  clock=self.clock, new_id=self.new_id, **options)

    def translate(self, subject=None, provider=None, subject_plan=None, glossary=upper_glossary, context=None, translator_options=None, **options):
        result = self.translator(provider, subject_plan, glossary, **(translator_options or {})).translate(
            subject or document(), context or self.context(), **options)
        self.sequence = result.next_sequence
        return result

    def records(self):
        return self.provenance.list_by_job(self.job.job_id, limit=100).nodes

    def artifact_state(self, artifact_id):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute('SELECT state, reason_code FROM job_artifacts WHERE artifact_id = ?', (artifact_id,)).fetchone()

    def final(self, artifact_id):
        return self.root / 'ready' / f'{artifact_id}.bin'


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def rewrite_payload(env, artifact_id, mutate):
    """A payload changed and re-recorded, so it passes the digest and only its content can give it away."""
    path = env.final(artifact_id)
    data = json.loads(path.read_bytes())
    mutate(data)
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    path.write_bytes(payload)
    import hashlib
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute('UPDATE job_artifacts SET size = ?, digest = ? WHERE artifact_id = ?',
                           (len(payload), 'sha256:' + hashlib.sha256(payload).hexdigest(), artifact_id))
        connection.commit()


class PlanMemory:
    def __init__(self, match):
        self.match = match
        self.calls = []

    def lookup(self, query):
        self.calls.append(query)
        if self.match is None:
            return None
        return CompatibleMemoryCandidate(
            self.match.original, self.match.translated, self.match.score, self.match.applied,
            CacheCompatibilityIdentity(query.scope, query.value_kind, query.semantic_fingerprint),
        )


def tags(index):
    # Spans become tag tokens, and the protection stage turns those into these before the provider.
    return f'__AYVU_PROTECTED_{index}__'


CORRUPTIONS = {
    'missing': lambda text: text.replace(tags(0), ''),
    'duplicated': lambda text: text.replace(tags(0), tags(0) + tags(0)),
    'invented': lambda text: text + tags(5),
    'reordered': lambda text: text.replace(tags(0), '@').replace(tags(1), tags(0)).replace('@', tags(1)),
    'corrupted': lambda text: text.replace(tags(0), 'AYVU TAG 0'),
}


def source_of(env):
    return CancellationSource(SAFE_POINTS)


LANGUAGES = '[{"code": "en", "name": "English", "targets": ["pt"]}]'
