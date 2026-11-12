"""The rich path over the production artifact store and provenance, faked only at the provider port."""
from __future__ import annotations
from contextlib import closing
import json
import sqlite3
import pytest
from ayvu.content.model import ProtectedPart
from ayvu.jobs.execution import InMemoryJobEventLog
from ayvu.projects.schema import open_artifact_service, open_provenance_repository
from ayvu.provenance.model import ProducerIdentity
from ayvu.providers.ports import ProviderRoute
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from ayvu.translation.document import DocumentTranslator
from ayvu.kernel import JobFailureFamily
from tests.support.translation_document_support import Env
from tests.support.translation_document_support import GLOSSARY_B, Glossed
from tests.support.translation_document_support import PROJECT, Provider, ROUTE
from tests.support.translation_document_support import cue, document, env, plan, rewrite_payload
from tests.support.translation_document_support import upper_glossary


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
    from ayvu.jobs.events import JobEventOrderError
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


@pytest.mark.parametrize('mutate', [
    lambda data: data.update(schema_version=3), lambda data: data.update(raw_fingerprint='sha256:' + '0' * 64),
    lambda data: data.update(revision_id='rev:v1:' + '0' * 64), lambda data: data.update(document_id='doc:v1:' + '0' * 64),
    lambda data: data.update(extra=1), lambda data: data['units'].pop(), lambda data: data['units'].reverse(),
    lambda data: data['units'][0].update(unit_id='unit:v1:' + '0' * 64), lambda data: data['units'][0].update(source='unknown'),
    lambda data: data['units'][0].update(source='dry_run'), lambda data: data['units'][0].update(raw=7),
    lambda data: data['units'][0].update(raw=None), lambda data: data['units'][0].update(extra=1),
    lambda data: data['units'][0].update(raw='AYVU PROTECTED 3'),
    lambda data: data.pop('dimensions'), lambda data: data.update(schema_version=1), lambda data: data.update(schema_version=True),
], ids=range(17))
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
