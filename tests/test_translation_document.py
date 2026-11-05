"""The rich path over the production artifact store and provenance, faked only at the provider port."""
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

from ayvu.artifact import ArtifactState
from ayvu.artifact_service import ARTIFACT_SAFE_POINTS
from ayvu.content import (
    AdapterIdentity, Constraint, Locator, ProtectedPart, SemanticKind, SourceLocale, UnitContent, UNKNOWN_LOCALE, LocaleState,
    build_document,
)
from ayvu.domain import LanguagePair
from ayvu.job import JobRequest, JobState
from ayvu.job_events import JobEventKind, JobFailureFamily, JobWarning, encode_event
from ayvu.job_execution import CancellationSource, InMemoryJobEventLog, JobCancellationError
from ayvu.project_schema import open_artifact_service, open_job_repository, open_provenance_repository
from ayvu.provenance import SourceKind, UNKNOWN, ProducerIdentity
from ayvu.provider import DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderResult
from ayvu.provider_capabilities import Capability, ProviderRequirements
from ayvu.provider_libretranslate import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ayvu.provider_service import failed_result
from ayvu.job_events import JobFailure
from ayvu.translation_document import (
    PIPELINE_SAFE_POINTS, RAW_RESULTS_ROLE, SAFE_POINTS, DocumentTranslator, ExecutionContext, TranslationCancelled,
    TranslationExecutionError, UnitSource,
)
from ayvu.translation_memory import TranslationMemoryMatch
from ayvu.cache_identity import (
    CacheCompatibilityIdentity,
    CacheAuthorizationPolicy,
    CacheOnlyRequest,
    CacheLookupState,
    CacheMissReason,
    CacheProvenanceMetadata,
    CacheValueKind,
    CompatibleMemoryCandidate,
    ExactCacheEntry,
    ExactCacheLookup,
    PutIfAbsentResult,
    PutIfAbsentState,
    TRANSLATION_STAGE_AGGREGATE,
)
from ayvu.translation_plan import ResolvedTranslationPlan, resolve_plan
from ayvu.translator import LibreTranslateTranslator, RoutedTranslator, TranslationRoute
from provider_fakes import FakeSession, http_response

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


def test_non_html_units_run_end_to_end_and_results_are_per_unit(env):
    provider = Provider()
    result = env.translate(provider=provider)
    assert [unit.source for unit in result.units] == [UnitSource.PROVIDER, UnitSource.PROVIDER]
    assert [unit.text for unit in result.units] == [' [HELLO THERE] ', '[GOOD NIGHT]']
    assert [unit.unit_id for unit in result.units] == [unit.unit_id for unit in document().units]
    assert result.complete and result.artifact_id is not None


def test_the_provider_request_carries_real_unit_identity_and_the_planned_route(env):
    provider = Provider()
    subject = document()
    env.translate(subject, provider)
    first = provider.requests[0]
    assert first.unit.unit_id == subject.units[0].unit_id and first.unit.revision_id == subject.revision_id
    assert first.route == ROUTE and first.text == 'Hello there' and first.request_id != provider.requests[1].request_id


def test_one_artifact_holds_the_document_and_its_raw_text_is_pre_glossary(env):
    result = env.translate()
    assert env.artifact_state(result.artifact_id) == ('ready', None)
    payload = json.loads(env.final(result.artifact_id).read_bytes())
    assert [(unit['source'], unit['raw']) for unit in payload['units']] == [('provider', '[Hello there]'), ('provider', '[Good night]')]
    assert [unit['unit_id'] for unit in payload['units']] == [unit.unit_id for unit in document().units]
    assert sum(1 for _ in (env.root / 'ready').iterdir()) == 1


def test_a_glossary_only_change_replays_after_restart_with_no_provider_call(env):
    env.translate()
    restarted = Env.__new__(Env)
    restarted.__dict__.update(env.__dict__)
    restarted.artifacts = open_artifact_service(env.database, env.backups, env.root, PROJECT, clock=env.clock, new_id=env.new_id)
    restarted.provenance = open_provenance_repository(env.database, env.backups, PROJECT, clock=env.clock, new_id=env.new_id)
    provider = Provider()
    result = restarted.translate(provider=provider, subject_plan=plan(glossary_ref=GLOSSARY_B), glossary=lambda text: Glossed(text.lower()))
    assert provider.requests == []
    assert [unit.source for unit in result.units] == [UnitSource.ARTIFACT_REPLAY] * 2
    assert [unit.text for unit in result.units] == [' [hello there] ', '[good night]']


def test_replay_keeps_outer_whitespace_and_expands_protected_parts(env):
    subject = document(cue('a', ' Hi <b> there ', 1, protected_parts=(ProtectedPart(4, 7, 'markup'),)))
    env.translate(subject)
    result = env.translate(subject, Provider(), subject_plan=plan(glossary_ref=GLOSSARY_B), glossary=Glossed)
    assert result.units[0].source is UnitSource.ARTIFACT_REPLAY and result.units[0].text == ' [Hi __AYVU_TAG_0__ there] '.replace('__AYVU_TAG_0__', '<b>')


def test_reusing_a_phase_in_one_event_stream_is_refused_by_the_ordering_rules(env):
    from ayvu.job_events import JobEventOrderError
    env.translate(context=env.context(progress_phase='same'))
    with pytest.raises(JobEventOrderError, match='^progress_regression$'):
        env.translate(subject_plan=plan(glossary_ref=GLOSSARY_B), context=env.context(progress_phase='same'))


def test_repeating_the_same_replay_is_idempotent_in_provenance(env):
    env.translate()
    env.translate(subject_plan=plan(glossary_ref=GLOSSARY_B))
    before = len(env.records())
    env.translate(subject_plan=plan(glossary_ref=GLOSSARY_B))
    assert len(env.records()) == before


@pytest.mark.parametrize('changes', [
    {'producer': ProducerIdentity('other@1', 'model@1', 'adapter@1', 'not_applicable')},
    {'capabilities': 'capfp:v1:' + '2' * 64}, {'chunk_limit': 1000}, {'policy_ref': 'policy:' + 'c' * 64},
    {'context_ref': 'context:' + 'd' * 64}, {'glossary_native': True},
], ids=['producer', 'capabilities', 'chunking', 'policy', 'context', 'native-glossary'])
def test_an_upstream_change_prevents_replay_and_calls_the_provider(env, changes):
    env.translate()
    subject_plan = plan(**changes)
    provider = Provider(producer=subject_plan.producer)
    result = env.translate(provider=provider, subject_plan=subject_plan)
    assert len(provider.requests) == 2 and result.units[0].source is UnitSource.PROVIDER


def test_a_different_route_prevents_replay(env):
    env.translate()
    route = ProviderRoute('en', 'pt', 'es')
    provider = Provider(route=route)
    env.translate(provider=provider, subject_plan=plan(route=route))
    assert len(provider.requests) == 2


def test_another_document_revision_prevents_replay(env):
    env.translate()
    provider = Provider()
    edited = document(cue('a', ' Hello there! ', 1), cue('b', 'Good night', 2))
    assert env.translate(edited, provider).units[0].source is UnitSource.PROVIDER and provider.requests


def test_another_job_cannot_replay_this_jobs_artifact(env):
    env.translate()
    other = env.running_job()
    provider = Provider()
    log = InMemoryJobEventLog(other.job_id)
    translator = DocumentTranslator(plan(), provider, upper_glossary, env.artifacts, env.provenance, log, clock=env.clock, new_id=env.new_id)
    assert translator.translate(document(), ExecutionContext(other, other.lease.owner_token)).units[0].source is UnitSource.PROVIDER


@pytest.mark.parametrize('damage', [
    lambda path: path.write_bytes(path.read_bytes()[:-5]), lambda path: path.write_bytes(b'Z' + path.read_bytes()[1:]), lambda path: path.unlink(),
], ids=['partial', 'corrupt', 'missing'])
def test_a_damaged_artifact_prevents_replay_and_the_work_is_redone(env, damage):
    first = env.translate()
    damage(env.final(first.artifact_id))
    provider = Provider()
    second = env.translate(provider=provider, subject_plan=plan(glossary_ref=GLOSSARY_B))
    assert len(provider.requests) == 2 and second.units[0].source is UnitSource.PROVIDER
    assert second.artifact_id != first.artifact_id
    assert env.artifact_state(first.artifact_id) == ('abandoned', 'integrity_failed')
    assert env.artifact_state(second.artifact_id) == ('ready', None)
    stored = [node.record for node in env.records() if node.record.request.operation == 'store_raw_results']
    assert [record.request.artifacts for record in stored] == [(first.artifact_id,), (second.artifact_id,)]


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


@pytest.mark.parametrize('mutate', [
    lambda data: data.update(schema_version=2), lambda data: data.update(raw_fingerprint='sha256:' + '0' * 64),
    lambda data: data.update(revision_id='rev:v1:' + '0' * 64), lambda data: data.update(document_id='doc:v1:' + '0' * 64),
    lambda data: data.update(extra=1), lambda data: data['units'].pop(), lambda data: data['units'].reverse(),
    lambda data: data['units'][0].update(unit_id='unit:v1:' + '0' * 64), lambda data: data['units'][0].update(source='unknown'),
    lambda data: data['units'][0].update(source='dry_run'), lambda data: data['units'][0].update(raw=7),
    lambda data: data['units'][0].update(raw=None), lambda data: data['units'][0].update(extra=1),
    lambda data: data['units'][0].update(raw='AYVU PROTECTED 3'),
], ids=range(14))
def test_a_mismatched_payload_with_a_valid_digest_fails_closed(env, mutate):
    first = env.translate()
    rewrite_payload(env, first.artifact_id, mutate)
    provider = Provider()
    with pytest.raises(TranslationExecutionError, match='^invalid_raw_results$') as caught:
        env.translate(provider=provider, subject_plan=plan(glossary_ref=GLOSSARY_B))
    assert provider.requests == [] and caught.value.failure.family is JobFailureFamily.INTEGRITY


def test_a_non_json_payload_with_a_valid_digest_fails_closed(env):
    first = env.translate()
    path = env.final(first.artifact_id)
    path.write_bytes(b'not json')
    import hashlib
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute('UPDATE job_artifacts SET size = 8, digest = ? WHERE artifact_id = ?',
                           ('sha256:' + hashlib.sha256(b'not json').hexdigest(), first.artifact_id))
        connection.commit()
    with pytest.raises(TranslationExecutionError, match='^invalid_raw_results$'):
        env.translate(provider=Provider())


def test_exact_cache_is_keyed_by_the_plan_and_reuse_skips_the_provider(env):
    cache = PlanCache()
    env.translate(document(key='one.vtt'), translator_options={'exact_cache': cache})
    assert {key.semantic_fingerprint for key in cache.stored} == {plan().semantic_fingerprint} and len(cache.stored) == 2
    assert len({key.source_content_digest for key in cache.stored}) == 2
    provider = Provider()
    result = env.translate(document(key='two.vtt'), provider, context=env.context(progress_phase='second'), translator_options={'exact_cache': cache})
    assert provider.requests == [] and result.units[0].source is UnitSource.EXACT_CACHE and result.units[0].text == ' [HELLO THERE] '


def test_another_plan_does_not_see_the_cache_entries(env):
    cache = PlanCache()
    env.translate(document(key='one.vtt'), translator_options={'exact_cache': cache})
    provider = Provider()
    env.translate(document(key='two.vtt'), provider, subject_plan=plan(chunk_limit=999), context=env.context(progress_phase='second'),
                  translator_options={'exact_cache': cache})
    assert len(provider.requests) == 2


def test_the_legacy_v1_ports_do_not_fit_the_generic_path():
    from ayvu.html_translate import _LegacyV1Cache
    from ayvu.translation_document import ExactCachePort, MemoryPort
    from ayvu.translation_memory import TranslationMemory
    assert 'key' in inspect.signature(ExactCachePort.lookup).parameters
    assert 'plan_fingerprint' not in inspect.signature(_LegacyV1Cache.get).parameters
    assert 'query' in inspect.signature(MemoryPort.lookup).parameters
    assert 'plan_fingerprint' not in inspect.signature(TranslationMemory.lookup).parameters


def test_the_generic_path_cannot_reach_the_legacy_cache():
    from pathlib import Path
    source = (Path(__file__).parents[1] / 'src' / 'ayvu' / 'translation_document.py').read_text()
    for name in ('from .cache import', 'TranslationCache', 'from .html_translate', '_LegacyV1Cache', 'import requests'):
        assert name not in source


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


def test_applied_memory_skips_the_provider_and_is_recorded_as_unknown_producer(env):
    memory = PlanMemory(TranslationMemoryMatch('Good night', 'Boa noite', 0.99, True))
    provider = Provider()
    result = env.translate(document(cue('a', 'Good night', 1)), provider, translator_options={'memory': memory})
    assert provider.requests == [] and result.units[0].source is UnitSource.APPLIED_MEMORY and result.units[0].text == 'BOA NOITE'
    assert [query.semantic_fingerprint for query in memory.calls] == [plan().semantic_fingerprint]
    record = next(node.record for node in env.records() if node.record.request.source_kind is SourceKind.TRANSLATION_MEMORY)
    assert record.request.producer.provider == UNKNOWN and not record.request.producer.fully_identified


def test_a_memory_suggestion_is_distinct_from_applied_memory(env):
    memory = PlanMemory(TranslationMemoryMatch('Good night', 'Boa noite', 0.6, False))
    result = env.translate(document(cue('a', 'Good night', 1)), translator_options={'memory': memory})
    assert result.units[0].source is UnitSource.PROVIDER and result.units[0].memory_suggested


@pytest.mark.parametrize('options,source', [({'cache_only': CacheOnlyRequest.resolve_compatible()}, UnitSource.CACHE_ONLY_MISSING),
                                            ({'dry_run': True}, UnitSource.DRY_RUN)], ids=['cache-only', 'dry-run'])
def test_gates_call_no_provider_and_publish_nothing(env, options, source):
    provider = Provider()
    result = env.translate(provider=provider, **options)
    assert provider.requests == [] and result.artifact_id is None and not result.complete
    assert [unit.source for unit in result.units] == [source] * 2 and result.units[0].text == ' Hello there '
    assert not (env.root / 'ready').exists() and env.records() == ()


def test_a_blank_unit_touches_nothing_and_survives_replay(env):
    subject = document(cue('a', '   ', 1), cue('b', 'Hi', 2))
    provider = Provider()
    result = env.translate(subject, provider)
    assert result.units[0].source is UnitSource.BLANK and result.units[0].text == '   ' and len(provider.requests) == 1
    replayed = env.translate(subject, subject_plan=plan(glossary_ref=GLOSSARY_B))
    assert replayed.units[0].source is UnitSource.BLANK and replayed.units[1].source is UnitSource.ARTIFACT_REPLAY


def test_chunks_are_sent_in_order_with_cancellation_points_between_them(env):
    text = 'First sentence here. Second sentence here. Third sentence here.'
    provider = Provider()
    result = env.translate(document(cue('a', text, 1)), provider, subject_plan=plan(chunk_limit=25), glossary=Glossed)
    assert ''.join(request.text for request in provider.requests) == text and len(provider.requests) > 1
    assert result.units[0].text == ''.join(f'[{request.text}]' for request in provider.requests)


def tags(index):
    # Spans become tag tokens, and the protection stage turns those into these before the provider.
    return f'__AYVU_PROTECTED_{index}__'


def test_protected_parts_never_reach_the_provider_and_are_restored(env):
    provider = Provider()
    subject = document(cue('a', 'Say <b>hi</b> now', 1, protected_parts=(ProtectedPart(4, 7, 'markup'), ProtectedPart(9, 13, 'markup'))))
    result = env.translate(subject, provider, glossary=Glossed)
    assert '<b>' not in provider.requests[0].text and '__AYVU_TAG' not in provider.requests[0].text
    assert tags(0) in provider.requests[0].text and tags(1) in provider.requests[0].text
    assert result.units[0].text == '[Say <b>hi</b> now]'


CORRUPTIONS = {
    'missing': lambda text: text.replace(tags(0), ''),
    'duplicated': lambda text: text.replace(tags(0), tags(0) + tags(0)),
    'invented': lambda text: text + tags(5),
    'reordered': lambda text: text.replace(tags(0), '@').replace(tags(1), tags(0)).replace('@', tags(1)),
    'corrupted': lambda text: text.replace(tags(0), 'AYVU TAG 0'),
}


@pytest.mark.parametrize('kind', CORRUPTIONS)
def test_any_protected_part_corruption_fails_before_cache_artifact_and_output(env, kind):
    # The recovery keeps protected spans verbatim and translates around them, so damage is repaired or refused,
    # and a provider that also damages plain fragments cannot pass.
    def respond(text):
        return CORRUPTIONS[kind](text) if '__AYVU' in text else 'AYVU TAG 9'

    cache = PlanCache()
    subject = document(cue('a', 'Say <b>hi</b> now', 1, protected_parts=(ProtectedPart(4, 7, 'markup'), ProtectedPart(9, 13, 'markup'))))
    with pytest.raises(TranslationExecutionError, match='^protected_content_not_preserved$') as caught:
        env.translate(subject, Provider(respond), translator_options={'exact_cache': cache})
    assert caught.value.failure.family is JobFailureFamily.INTEGRITY and 'put_if_absent' not in [call[0] for call in cache.calls]
    assert not (env.root / 'ready').exists() and env.records() == ()


def test_recovery_from_a_corrupted_part_is_a_valid_translation(env):
    def respond(text):
        return text.replace(tags(0), '') if '__AYVU' in text else f'<{text}>'

    subject = document(cue('a', 'Say <b> now', 1, protected_parts=(ProtectedPart(4, 7, 'markup'),)))
    assert env.translate(subject, Provider(respond), glossary=Glossed).units[0].text == '<Say> <b> <now>'


def test_a_token_inside_a_protected_part_is_safe_and_comes_back_unchanged(env):
    literal = '__AYVU_TAG_0__'
    subject = document(cue('a', f'{literal} world', 1, protected_parts=(ProtectedPart(0, len(literal), 'placeholder'),)))
    result = env.translate(subject, Provider(lambda text: text.replace('world', 'mundo')), glossary=lambda text: Glossed(text))
    assert result.units[0].text == f'{literal} mundo'
    assert result.units[0].tokenized_text == '__AYVU_TAG_0__ mundo'


def test_replay_gives_the_same_tokenized_text(env):
    literal = '<b>'
    subject = document(cue('a', f'{literal}world', 1, protected_parts=(ProtectedPart(0, len(literal), 'markup'),)))
    first = env.translate(subject, Provider(lambda text: text.replace('world', 'mundo')), glossary=lambda text: Glossed(text))
    second = env.translate(subject, Provider(), glossary=lambda text: Glossed(text), context=env.context())
    assert second.units[0].source is UnitSource.ARTIFACT_REPLAY
    assert second.units[0].tokenized_text == first.units[0].tokenized_text == '__AYVU_TAG_0__mundo'


def test_tokenized_text_of_a_blank_unit_is_its_source(env):
    result = env.translate(document(cue('a', '   ', 1), cue('b', 'x', 2)), glossary=lambda text: Glossed(text))
    assert result.units[0].tokenized_text == '   '
    replayed = env.translate(document(cue('a', '   ', 1), cue('b', 'x', 2)), glossary=lambda text: Glossed(text), context=env.context())
    assert replayed.units[0].source is UnitSource.BLANK and replayed.units[0].tokenized_text == '   '


def test_tokenized_text_stays_out_of_the_representation(env):
    result = env.translate(document(cue('a', 'Call me Ishmael.', 1)), glossary=lambda text: Glossed(text))
    assert 'Ishmael' not in repr(result.units[0])


def test_an_html_document_survives_the_rich_path_and_reconstruction(env):
    from ayvu.format_adapter import UnitTranslation
    from ayvu.html_structure import _parse, extract_html_document, reconstruct_html

    html = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="urn:epub"><body>'
            '<p>The <a epub:type="noteref" href="#n">world<!-- keep --></a> and <code>world</code>.</p><p>__AYVU_TAG_0__ world</p>'
            '<img src="a.png" alt="world"/></body></html>')
    subject = extract_html_document(html, PROJECT, 'chapter.xhtml', source_locale=ENGLISH, include_alt_text=True)
    hostile = Provider(lambda text: text.replace('world', 'mundo <script>x()</script>'))
    result = env.translate(subject, hostile, glossary=lambda text: Glossed(text))
    assert all('<code>' not in request.text and '<a ' not in request.text for request in hostile.requests)
    output = reconstruct_html(html, subject, [UnitTranslation(unit.unit_id, unit.tokenized_text) for unit in result.units])
    parsed = _parse(output)
    assert parsed.find('script') is None and 'mundo &lt;script&gt;' in output.decode()
    link = parsed.find('a')
    assert link['epub:type'] == 'noteref' and link['href'] == '#n' and '<!-- keep -->' in str(link)
    assert parsed.find('code').string == 'world' and '__AYVU_TAG_0__ mundo' in output.decode()
    assert parsed.find('img')['alt'].startswith('mundo') and parsed.find('img')['src'] == 'a.png'


def test_source_holding_a_reserved_placeholder_is_refused(env):
    with pytest.raises(TranslationExecutionError, match='^reserved_placeholder_in_source$'):
        env.translate(document(cue('a', 'Odd __AYVU_TAG_0__ text', 1)))


@pytest.mark.parametrize('failure,code', [
    (JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'provider_timeout', True), 'provider_timeout'),
    (JobFailure(JobFailureFamily.CAPABILITY, 'unsupported'), 'unsupported'),
], ids=['transient', 'capability'])
def test_a_provider_failure_stops_the_document_and_publishes_nothing(env, failure, code):
    class Failing(Provider):
        def execute(self, request):
            self.requests.append(request)
            return failed_result(request, failure)

    provider = Failing()
    cache = PlanCache()
    with pytest.raises(TranslationExecutionError, match=f'^{code}$') as caught:
        env.translate(provider=provider, translator_options={'exact_cache': cache})
    assert caught.value.failure is failure and len(provider.requests) == 1
    assert not (env.root / 'ready').exists() and cache.stored == {} and env.records() == ()


def test_a_provider_exception_is_normalized_without_its_message(env):
    class Exploding(Provider):
        def execute(self, request):
            raise RuntimeError('Call me Ishmael.')

    with pytest.raises(TranslationExecutionError, match='^unexpected_error$') as caught:
        env.translate(provider=Exploding())
    assert 'Ishmael' not in repr(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize('changes,code', [
    ({'producer': ProducerIdentity('other@1', 'model@1', 'adapter@1', 'not_applicable')}, 'plan_invalidated'),
    ({'route': None}, 'plan_invalidated'), ({'route': ProviderRoute('en', 'pt', 'es')}, 'result_route_conflict'),
], ids=['producer', 'unknown-route', 'route'])
def test_a_provider_that_is_not_what_was_planned_invalidates_the_plan(env, changes, code):
    cache = PlanCache()
    with pytest.raises(TranslationExecutionError, match=f'^{code}$'):
        env.translate(provider=Provider(**changes), translator_options={'exact_cache': cache})
    assert cache.stored == {} and not (env.root / 'ready').exists()


def test_provider_warnings_become_events(env):
    env.translate(provider=Provider(warnings=(JobWarning('ignored_context'),)))
    assert [event.payload.code for event in env.log.replay() if event.kind is JobEventKind.WARNING] == ['ignored_context'] * 2


def test_progress_events_are_ordered_and_bounded(env):
    result = env.translate(context=env.context(progress_phase='translate'))
    events = env.log.replay()
    assert [event.sequence for event in events] == list(range(1, len(events) + 1)) and result.next_sequence == len(events) + 1
    assert [(event.payload.completed, event.payload.total) for event in events] == [(1, 2), (2, 2)]
    assert {event.payload.phase for event in events} == {'translate'}


def test_two_documents_in_one_job_use_their_own_phases(env):
    env.translate(document(key='one.vtt'), context=env.context(progress_phase='document_1'))
    env.translate(document(key='two.vtt'), context=env.context(progress_phase='document_2'))
    assert {event.payload.phase for event in env.log.replay()} == {'document_1', 'document_2'}


def source_of(env):
    return CancellationSource(SAFE_POINTS)


@pytest.mark.parametrize('moment', ['before_first_unit', 'between_units', 'between_stages', 'between_chunks', 'before_publication'])
def test_cancellation_at_each_safe_point_stops_work_and_publishes_nothing(env, moment):
    source = CancellationSource(SAFE_POINTS)
    provider_calls = []

    def respond(text):
        provider_calls.append(text)
        if moment == 'between_chunks' and len(provider_calls) == 1:
            source.request('user_requested', NOW)
        return f'[{text}]'

    def glossary(text):
        if moment == 'between_units':
            source.request('user_requested', NOW)
        if moment == 'before_publication' and len(provider_calls) == 2:
            source.request('user_requested', NOW)
        return Glossed(text)

    class Cache(PlanCache):
        def lookup(self, key):
            if moment == 'between_stages':
                source.request('user_requested', NOW)
            return super().lookup(key)

    if moment == 'before_first_unit':
        source.request('user_requested', NOW)
    long_text = 'First sentence here. Second sentence here.'
    subject = document(cue('a', long_text, 1), cue('b', 'Good night', 2)) if moment == 'between_chunks' else document()
    with pytest.raises(TranslationCancelled, match='^translation_cancelled$') as caught:
        env.translate(subject, Provider(respond), subject_plan=plan(chunk_limit=25) if moment == 'between_chunks' else plan(), glossary=glossary,
                      context=env.context(cancellation=source), translator_options={'exact_cache': Cache()})
    assert caught.value.failure.family is JobFailureFamily.CANCELLED
    assert not (env.root / 'ready').exists() or not list((env.root / 'ready').iterdir())
    assert env.records() == ()
    kinds = [event.kind for event in env.log.replay()]
    assert kinds[-2:] == [JobEventKind.CANCELLATION_REQUESTED, JobEventKind.CANCELLATION_ACKNOWLEDGED]
    assert JobEventKind.OUTCOME not in kinds
    assert source.acknowledgement.safe_point in SAFE_POINTS
    if moment == 'before_first_unit':
        assert provider_calls == [] and source.acknowledgement.safe_point == 'pipeline_before_unit'
    if moment == 'between_stages':
        assert provider_calls == [] and source.acknowledgement.safe_point == 'pipeline_between_stages'
    if moment == 'between_units':
        assert len(provider_calls) == 1
    if moment == 'between_chunks':
        assert len(provider_calls) == 1 and source.acknowledgement.safe_point == 'pipeline_between_chunks'


def test_cancellation_during_publication_is_translated(env):
    source = CancellationSource(SAFE_POINTS)

    class Requesting:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def publish(self, *args, **kwargs):
            source.request('user_requested', NOW)
            return self.inner.publish(*args, **kwargs)

    translator = DocumentTranslator(plan(), Provider(), upper_glossary, Requesting(env.artifacts), env.provenance, env.log, clock=env.clock, new_id=env.new_id)
    with pytest.raises(TranslationCancelled):
        translator.translate(document(), env.context(cancellation=source))
    assert source.acknowledgement.safe_point == 'artifact_before_write' and env.records() == ()


def test_a_blocking_call_is_not_reported_as_cancelled(env):
    source = CancellationSource(SAFE_POINTS)

    def respond(text):
        source.request('user_requested', NOW)
        return f'[{text}]'

    subject = document(cue('a', 'Only one', 1))
    with pytest.raises(TranslationCancelled):
        env.translate(subject, Provider(respond), context=env.context(cancellation=source))
    # The call that was already running finished; the stop comes at the next safe point.
    assert source.acknowledgement.safe_point == 'pipeline_between_stages'


def test_a_source_must_declare_the_safe_points(env):
    with pytest.raises(JobCancellationError, match='^undeclared_safe_point$'):
        env.translate(context=env.context(cancellation=CancellationSource(('other',))))
    assert not (env.root / 'ready').exists()
    assert set(PIPELINE_SAFE_POINTS) | set(ARTIFACT_SAFE_POINTS) == set(SAFE_POINTS)


def test_a_late_cancellation_does_not_retract_a_finished_document(env):
    source = CancellationSource(SAFE_POINTS)
    result = env.translate(context=env.context(cancellation=source))
    source.request('user_requested', NOW)
    assert env.artifact_state(result.artifact_id) == ('ready', None)


def test_a_job_that_is_not_running_is_refused(env):
    queued = env.jobs.create(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'e' * 64))
    leased = env.jobs.acquire_lease(queued.job_id, timedelta(minutes=5))
    provider = Provider()
    with pytest.raises(TranslationExecutionError, match='^job_not_running$'):
        env.translator(provider).translate(document(), ExecutionContext(leased, leased.lease.owner_token))
    assert provider.requests == []


@pytest.mark.parametrize('context,code', [
    (lambda env: None, 'identity_required'), (lambda env: 'job', 'identity_required'),
    (lambda env: env.context(owner_token='0' * 64), 'lease_not_held'),
    (lambda env: env.context(job='job'), 'identity_mismatch'),
], ids=['absent', 'wrong-type', 'wrong-owner', 'no-job'])
def test_the_rich_entry_rejects_missing_or_inconsistent_identity_before_any_work(env, context, code):
    provider = Provider()
    with pytest.raises(TranslationExecutionError, match=f'^{code}$'):
        env.translator(provider).translate(document(), context(env))
    assert provider.requests == []


def test_a_plain_string_is_not_a_document(env):
    with pytest.raises(TranslationExecutionError, match='^identity_required$'):
        env.translator().translate('Hello', env.context())


def test_an_expired_lease_is_refused(env):
    env.now = NOW + timedelta(minutes=30)
    provider = Provider()
    with pytest.raises(TranslationExecutionError, match='^lease_not_held$'):
        env.translator(provider).translate(document(), env.context())
    assert provider.requests == []


def test_a_document_of_another_project_is_refused(env):
    foreign = build_document(str(UUID(int=0x9)), 'x.vtt', ADAPTER, ENGLISH, (cue('a', 'Hi', 1),))
    with pytest.raises(TranslationExecutionError, match='^identity_mismatch$'):
        env.translate(foreign)


@pytest.mark.parametrize('subject', [
    lambda: build_document(PROJECT, 'x.vtt', ADAPTER, SourceLocale(LocaleState.EXPLICIT, 'fr'), (cue('a', 'Hi', 1),)),
    lambda: build_document(PROJECT, 'x.vtt', ADAPTER, UNKNOWN_LOCALE, (cue('a', 'Hi', 1, source_locale=SourceLocale(LocaleState.EXPLICIT, 'fr')),)),
], ids=['document', 'unit'])
def test_a_locale_that_contradicts_the_plan_is_refused(env, subject):
    with pytest.raises(TranslationExecutionError, match='^locale_conflicts_with_plan$'):
        env.translate(subject())


def test_unknown_locale_is_accepted(env):
    subject = build_document(PROJECT, 'x.vtt', ADAPTER, UNKNOWN_LOCALE, (cue('a', 'Hi', 1, source_locale=UNKNOWN_LOCALE),))
    assert env.translate(subject).complete


def test_an_invalid_plan_is_refused():
    with pytest.raises(TranslationExecutionError, match='^invalid_plan$'):
        DocumentTranslator('plan', None, None, None, None, None, clock=lambda: NOW)


def test_an_artifact_failure_is_a_typed_failure_and_leaves_no_record(env):
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    with pytest.raises(TranslationExecutionError, match='^lease_not_held$'):
        env.translate()
    assert env.records() == ()


def test_a_publication_conflict_is_reported_not_overwritten(env):
    env.translate()

    class Damaged:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def find_reusable(self, request):
            from ayvu.artifact_service import ReuseResult
            return ReuseResult(None, 'artifact_not_found')

    translator = DocumentTranslator(plan(), Provider(), upper_glossary, Damaged(env.artifacts), env.provenance, env.log, clock=env.clock, new_id=env.new_id)
    with pytest.raises(TranslationExecutionError, match='^artifact_exists$'):
        translator.translate(document(), env.context(first_sequence=env.sequence))


def test_provenance_traces_the_artifact_to_the_job_input_and_distinguishes_origins(env):
    cache = PlanCache()
    from ayvu.cache_identity import exact_entry_key
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    key = exact_entry_key(scope, TRANSLATION_STAGE_AGGREGATE, plan().semantic_fingerprint, 'Good night')
    cache.stored[key] = ExactCacheEntry(key, 'Boa noite', CacheProvenanceMetadata('translate', PRODUCER, plan().pipeline_revision))
    subject = document()
    result = env.translate(subject, translator_options={'exact_cache': cache})
    trace = env.provenance.trace_artifact(result.artifact_id, max_depth=8, max_records=32)
    operations = {node.record.request.operation: node.record.request.source_kind for node in trace.graph.nodes}
    assert operations == {'store_raw_results': SourceKind.PIPELINE, 'translate': SourceKind.PROVIDER, 'reuse_exact_cache': SourceKind.CACHE}
    assert [item.input_fingerprint for item in trace.jobs] == ['sha256:' + 'e' * 64]
    assert all(node.record.request.subject.value == subject.revision_id for node in trace.graph.nodes)
    payload = json.loads(env.final(result.artifact_id).read_bytes())
    assert [unit['source'] for unit in payload['units']] == ['provider', 'exact_cache']


def test_provenance_marks_a_replay_as_such_and_links_the_original(env):
    first = env.translate()
    env.translate(subject_plan=plan(glossary_ref=GLOSSARY_B))
    trace = env.provenance.trace_artifact(first.artifact_id, max_depth=8, max_records=32)
    by_operation = {node.record.request.operation: node.record.request for node in trace.graph.nodes}
    assert {'replay_raw_results', 'store_raw_results', 'translate'} <= set(by_operation)
    assert by_operation['replay_raw_results'].configuration_refs[0].reference == GLOSSARY_B


def test_storage_stays_small_one_artifact_and_few_records_for_many_units(env):
    subject = document(*(cue(f'c{index}', f'Line number {index}', index) for index in range(200)))
    result = env.translate(subject, glossary=Glossed)
    assert len(result.units) == 200
    assert sum(1 for _ in (env.root / 'ready').iterdir()) == 1 and len(env.records()) == 2


@pytest.mark.parametrize('canary', CANARIES)
def test_content_stays_out_of_events_provenance_repr_errors_and_logs(env, canary, caplog):
    caplog.set_level(logging.DEBUG)
    subject = document(cue('a', canary, 1))
    result = env.translate(subject, glossary=lambda text: Glossed(text + ' SECRET-GLOSSARY-VALUE'))
    texts = [encode_event(event) for event in env.log.replay()]
    texts += [repr(result), repr(result.units[0]), repr(env.context()), repr(env.translator()), repr(plan()), caplog.text]
    with closing(sqlite3.connect(env.database)) as connection:
        texts += [row[0] for row in connection.execute('SELECT payload FROM provenance_records')]
    assert not any(canary in text for text in texts)
    # The artifact is the one private place that holds it, by design.
    assert canary.encode() in env.final(result.artifact_id).read_bytes().replace(b'\\u', b'') or canary.upper().encode() in env.final(result.artifact_id).read_bytes()


LANGUAGES = '[{"code": "en", "name": "English", "targets": ["pt"]}]'


def test_the_production_libretranslate_adapter_runs_the_rich_path_over_fake_http(env):
    base = LibreTranslateTranslator(url='http://localhost:5000', retries=0)
    base.session = FakeSession([http_response(200, '{"translatedText": "Ola"}'), http_response(200, '{"translatedText": "Boa noite"}')])
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements((Capability.PLAIN_TEXT_INPUT,)),
                                LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000, glossary_ref=GLOSSARY_A)
    result = env.translate(provider=LibreTranslateProvider(base, clock=env.clock), subject_plan=subject_plan, glossary=Glossed)
    assert [unit.text for unit in result.units] == [' Ola ', 'Boa noite']
    assert [post[1] for post in base.session.posts] == [
        {'q': 'Hello there', 'source': 'en', 'target': 'pt', 'format': 'text'},
        {'q': 'Good night', 'source': 'en', 'target': 'pt', 'format': 'text'},
    ]
    assert base.session.gets == []


def test_the_production_adapter_failure_reaches_the_caller_without_its_message(env):
    base = LibreTranslateTranslator(url='http://user:hunter2@private.example.test', retries=0)
    base.session = FakeSession([http_response(500, 'Call me Ishmael.')])
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements(()), LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000)
    with pytest.raises(TranslationExecutionError, match='^provider_server_error$') as caught:
        env.translate(provider=LibreTranslateProvider(base, clock=env.clock), subject_plan=subject_plan)
    assert 'hunter2' not in repr(caught.value) and 'Ishmael' not in repr(caught.value.failure)


def test_a_bridge_route_must_match_the_plan_through_the_production_adapter(env):
    base = LibreTranslateTranslator(url='http://localhost:5000', retries=0)
    base.session = FakeSession([])
    routed = RoutedTranslator(base, TranslationRoute('en', 'pt', 'es'))
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements(()), LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000)
    with pytest.raises(TranslationExecutionError, match='^route_conflict$'):
        env.translate(provider=LibreTranslateProvider(routed, clock=env.clock), subject_plan=subject_plan)
    assert base.session.posts == [] and ArtifactState.READY


def test_the_facade_remains_free_of_durable_state():
    from pathlib import Path
    root = Path(__file__).parents[1] / 'src' / 'ayvu'
    for name in ('translation_pipeline.py',):
        source = (root / name).read_text()
        assert 'translation_document' not in source and 'from .artifact' not in source and 'from .provenance' not in source
    assert RAW_RESULTS_ROLE == 'raw_results' and LanguagePair(source='en', target='pt') and requests


@pytest.mark.parametrize('options', [{'dry_run': True}, {'cache_only': CacheOnlyRequest.resolve_compatible()}],
                         ids=['dry-run', 'cache-only'])
def test_an_untranslated_unit_returns_its_exact_source_with_protected_parts(env, options):
    subject = document(cue('a', ' Say <b> now ', 1, protected_parts=(ProtectedPart(5, 8, 'markup'),)))
    assert env.translate(subject, **options).units[0].text == ' Say <b> now '
