from __future__ import annotations
from contextlib import closing
from datetime import timedelta
import json
import logging
import sqlite3
from uuid import UUID
import pytest
from ayvu.content.model import SourceLocale, UNKNOWN_LOCALE, LocaleState, build_document
from ayvu.jobs.events import encode_event
from ayvu.provenance.model import SourceKind
from ayvu.translation.document_contracts import TranslationExecutionError
from ayvu.translation.document import DocumentTranslator
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy
from ayvu.knowledge.cache.entries import CacheProvenanceMetadata, ExactCacheEntry
from ayvu.knowledge.cache.identity_values import TRANSLATION_STAGE_AGGREGATE
from tests.support.translation_document_support import ADAPTER, CANARIES, ENGLISH
from tests.support.translation_document_support import GLOSSARY_B, Glossed, NOW, PRODUCER
from tests.support.translation_document_support import PROJECT, PlanCache, Provider
from tests.support.translation_document_support import cue, document, env, plan
from tests.support.translation_document_support import upper_glossary


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
            from ayvu.artifacts.service import ReuseResult
            return ReuseResult(None, 'artifact_not_found')

    translator = DocumentTranslator(plan(), Provider(), upper_glossary, Damaged(env.artifacts), env.provenance, env.log, clock=env.clock, new_id=env.new_id)
    with pytest.raises(TranslationExecutionError, match='^artifact_exists$'):
        translator.translate(document(), env.context(first_sequence=env.sequence))


def test_provenance_traces_the_artifact_to_the_job_input_and_distinguishes_origins(env):
    cache = PlanCache()
    from ayvu.knowledge.cache.entries import exact_entry_key
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
