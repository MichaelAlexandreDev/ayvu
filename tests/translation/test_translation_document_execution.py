from __future__ import annotations
from datetime import timedelta
import pytest
from ayvu.artifacts.service import ARTIFACT_SAFE_POINTS
from ayvu.jobs.model import JobRequest
from ayvu.jobs.events import JobEventKind
from ayvu.jobs.execution import CancellationSource
from ayvu.provenance.model import ProducerIdentity
from ayvu.providers.ports import ProviderRoute
from ayvu.providers.service import failed_result
from ayvu.translation.document_contracts import PIPELINE_SAFE_POINTS, SAFE_POINTS, ExecutionContext, TranslationCancelled, TranslationExecutionError
from ayvu.translation.document import DocumentTranslator
from ayvu.kernel import JobFailureFamily, JobWarning, JobCancellationError, JobFailure
from tests.support.translation_document_support import Glossed, NOW
from tests.support.translation_document_support import PROJECT, PlanCache, Provider
from tests.support.translation_document_support import cue, document, env, plan
from tests.support.translation_document_support import upper_glossary


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
