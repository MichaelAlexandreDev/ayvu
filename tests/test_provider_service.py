import ast
import logging
from pathlib import Path
from uuid import UUID

import pytest

from ayvu.job_events import JobFailureFamily, JobOperationError, JobFailure
from ayvu.job_execution import CancellationSource
from ayvu.provider import (
    ConstraintsFeature, ContextFeature, ContextItem, DeadlineFeature, DeliveryState, ProviderRoute, ProviderStatus,
    Requirement,
)
from ayvu.content import Constraint
from ayvu.provider_service import (
    ProviderInstance, ProviderRegistry, ProviderRegistryError, UNKNOWN_PRODUCER, decide_features, execute,
)
from provider_fakes import FAKE_PRODUCER, FakeFactory, FakeProvider, failure, fake_instance, request, success

REQUIRED = ContextFeature((ContextItem('previous', 'x'),), Requirement.REQUIRED)
ADVISORY = ConstraintsFeature((Constraint('max_chars', 5),), Requirement.ADVISORY)


def registry(**changes):
    target = ProviderRegistry()
    factory = FakeFactory(**changes)
    target.register(fake_instance(), factory)
    return target, factory


def test_a_fake_provider_registers_resolves_and_translates():
    target, factory = registry()
    with target.open('fake_local') as provider:
        result = execute(provider, request())
    assert result.status is ProviderStatus.SUCCEEDED and result.text == 'Ola' and result.producer == FAKE_PRODUCER
    assert factory.created[0][0] == fake_instance() and factory.created[0][1].closed == 1


def test_each_open_is_a_new_provider_and_implies_no_singleton():
    target, factory = registry()
    with target.open('fake_local') as first, target.open('fake_local') as second:
        assert first is not second
    assert [provider.closed for _, provider in factory.created] == [1, 1]


def test_instance_ids_are_listed_in_a_fixed_order():
    target = ProviderRegistry()
    for name in ('zeta', 'alpha', 'mid'):
        target.register(fake_instance(instance_id=name), FakeFactory())
    assert target.instance_ids() == ('alpha', 'mid', 'zeta')


@pytest.mark.parametrize('instance_changes,code', [
    ({'adapter_id': 'other'}, 'incompatible_adapter'), ({'adapter_version': 4}, 'incompatible_adapter'),
    ({'config_version': 9}, 'incompatible_configuration'),
])
def test_incompatible_registrations_are_refused_and_not_stored(instance_changes, code):
    target = ProviderRegistry()
    with pytest.raises(ProviderRegistryError, match=f'^{code}$'):
        target.register(fake_instance(**instance_changes), FakeFactory())
    assert target.instance_ids() == ()


def test_duplicate_is_distinct_and_keeps_the_first_factory():
    target, factory = registry()
    other = FakeFactory()
    with pytest.raises(ProviderRegistryError, match='^duplicate_instance$'):
        target.register(fake_instance(), other)
    with target.open('fake_local'):
        pass
    assert len(factory.created) == 1 and other.created == []


def test_supported_configuration_versions_are_accepted():
    target = ProviderRegistry()
    target.register(fake_instance(config_version=2), FakeFactory())
    assert target.instance_ids() == ('fake_local',)


@pytest.mark.parametrize('instance_id', ['unknown', 'libretranslate', None, 7, ''], ids=repr)
def test_unknown_instance_is_distinct(instance_id):
    target, factory = registry()
    with pytest.raises(ProviderRegistryError, match='^unknown_instance$'):
        with target.open(instance_id):
            pass
    assert factory.created == []


def test_factory_failure_is_distinct_and_does_not_quote_the_cause():
    secret = 'https://user:hunter2@example.test'

    def explode():
        raise RuntimeError(secret)

    target, _ = registry(make=explode)
    with pytest.raises(ProviderRegistryError, match='^factory_failed$') as caught:
        with target.open('fake_local'):
            pass
    assert secret not in repr(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize('made', [object(), None, 'text'], ids=repr)
def test_factory_returning_a_non_provider_fails(made):
    target, _ = registry(make=lambda: made)
    with pytest.raises(ProviderRegistryError, match='^factory_failed$'):
        with target.open('fake_local'):
            pass


def test_cleanup_runs_when_the_block_raises_and_the_original_error_survives():
    target, factory = registry()
    with pytest.raises(KeyError):
        with target.open('fake_local'):
            raise KeyError('body')
    assert factory.created[0][1].closed == 1


def test_cleanup_failure_is_typed_but_never_hides_the_block_error():
    def explode():
        raise RuntimeError('https://user:hunter2@example.test')

    target, _ = registry(make=lambda: FakeProvider(on_close=explode))
    with pytest.raises(ProviderRegistryError, match='^cleanup_failed$') as caught:
        with target.open('fake_local'):
            pass
    assert 'hunter2' not in repr(caught.value) and caught.value.__cause__ is None
    with pytest.raises(KeyError):
        with target.open('fake_local'):
            raise KeyError('body')


@pytest.mark.parametrize('changes', [
    {'instance_id': 'Bad Name'}, {'adapter_id': '../x'}, {'adapter_version': 0}, {'adapter_version': True},
    {'config_version': 0}, {'configuration_ref': 'plain secret'},
])
def test_invalid_instances_are_refused(changes):
    with pytest.raises(ProviderRegistryError, match='^invalid_instance$'):
        fake_instance(**changes)
    with pytest.raises(ProviderRegistryError, match='^invalid_instance$'):
        ProviderRegistry().register('fake_local', FakeFactory())


def test_instance_repr_hides_its_configuration():
    reference = 'config:' + 'a' * 64
    assert reference not in repr(fake_instance(configuration_ref=reference))
    assert 'instances=1' in repr(registry()[0])


def test_required_and_advisory_features_are_separated():
    decision = decide_features(request(features=(REQUIRED, ADVISORY)), frozenset())
    assert decision.unsupported_required == ('context',)
    assert [item.code for item in decision.ignored_advisory] == ['ignored_constraints']


def test_supported_features_are_neither_refused_nor_warned_about():
    decision = decide_features(request(features=(REQUIRED, ADVISORY)), frozenset({'context', 'constraints'}))
    assert decision.unsupported_required == () and decision.ignored_advisory == ()


@pytest.mark.parametrize('behavior,code,family', [
    (lambda item: None, 'malformed_provider_result', JobFailureFamily.PROVIDER_PERMANENT),
    (lambda item: 'Ola', 'malformed_provider_result', JobFailureFamily.PROVIDER_PERMANENT),
    (lambda item: {'text': 'Ola'}, 'malformed_provider_result', JobFailureFamily.PROVIDER_PERMANENT),
    (lambda item: success(request(request_id=str(UUID(int=8)))), 'result_identity_conflict', JobFailureFamily.PROVIDER_PERMANENT),
])
def test_malformed_and_conflicting_results_are_never_a_translation(behavior, code, family):
    result = execute(FakeProvider(behavior), request())
    assert (result.status, result.text, result.failure.code, result.failure.family) == (ProviderStatus.FAILED, None, code, family)
    assert result.delivery is DeliveryState.UNKNOWN


def test_route_conflict_is_a_failure():
    item = request(route=ProviderRoute('en', 'pt'))
    result = execute(FakeProvider(lambda item: success(item, route=ProviderRoute('en', 'pt', 'es'))), item)
    assert result.status is ProviderStatus.FAILED and result.failure.code == 'result_route_conflict'
    assert result.producer == FAKE_PRODUCER


def test_matching_or_unstated_routes_pass():
    item = request(route=ProviderRoute('en', 'pt'))
    assert execute(FakeProvider(lambda item: success(item, route=ProviderRoute('en', 'pt'))), item).text == 'Ola'
    assert execute(FakeProvider(), item).text == 'Ola'
    other = request()
    assert execute(FakeProvider(lambda item: success(item, route=ProviderRoute('en', 'pt', 'es'))), other).text == 'Ola'


def test_a_provider_exception_is_normalized_without_its_message(caplog):
    caplog.set_level(logging.DEBUG)
    canary = 'Call me Ishmael. https://user:hunter2@example.test'

    def explode(item):
        raise RuntimeError(canary)

    result = execute(FakeProvider(explode), request())
    assert result.failure.code == 'unexpected_error' and result.failure.family is JobFailureFamily.INTERNAL
    assert result.producer == UNKNOWN_PRODUCER and result.delivery is DeliveryState.UNKNOWN
    assert canary not in repr(result) and canary not in repr(result.failure) and canary not in caplog.text


def test_a_sanitized_envelope_passes_through():
    envelope = JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout', True)

    def explode(item):
        raise JobOperationError(envelope)

    assert execute(FakeProvider(explode), request()).failure is envelope


def test_a_provider_failure_result_passes_unchanged():
    assert execute(FakeProvider(failure), request()).failure.code == 'fake_failure'


def test_keyboard_interrupt_is_not_swallowed():
    def stop(item):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute(FakeProvider(stop), request())


def test_execute_rejects_a_value_that_is_not_a_request():
    with pytest.raises(ProviderRegistryError, match='^invalid_request$'):
        execute(FakeProvider(), 'Hello')


def test_a_provider_can_use_the_shared_cancellation_token():
    source = CancellationSource(('fake_safe_point',))

    def cooperative(item):
        if item.cancellation.checkpoint('fake_safe_point') is not None:
            from ayvu.provider_service import failed_result
            return failed_result(item, JobFailure(JobFailureFamily.CANCELLED, 'stopped'), delivery=DeliveryState.NOT_SENT)
        return success(item)

    provider = FakeProvider(cooperative)
    assert execute(provider, request(cancellation=source.token)).status is ProviderStatus.SUCCEEDED
    source.request('user_requested', __import__('datetime').datetime(2026, 11, 3, tzinfo=__import__('datetime').UTC))
    result = execute(provider, request(cancellation=source.token))
    assert result.status is ProviderStatus.CANCELLED and result.text is None


def test_contract_modules_have_no_provider_name_or_framework():
    root = Path(__file__).parents[1] / 'src' / 'ayvu'
    forbidden = {'requests', 'typer', 'rich', 'sqlite3', 'bs4', 'ebooklib', 'lxml', 'os', 'pathlib', 'importlib'}
    for name in ('provider.py', 'provider_service.py'):
        source = (root / name).read_text()
        assert 'libretranslate' not in source.lower()
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots.add(node.module.split('.')[0])
        assert not roots & forbidden, name
    assert ProviderInstance.__doc__ and DeadlineFeature.name == 'deadline'


def test_a_refusal_keeps_its_own_code_and_delivery_state_when_routes_differ():
    from ayvu.provider_service import failed_result
    item = request(route=ProviderRoute('en', 'pt'))
    refusal = lambda subject: failed_result(subject, JobFailure(JobFailureFamily.CAPABILITY, 'route_conflict'),
                                            delivery=DeliveryState.NOT_SENT, route=ProviderRoute('en', 'pt', 'es'))
    result = execute(FakeProvider(refusal), item)
    assert result.failure.code == 'route_conflict' and result.delivery is DeliveryState.NOT_SENT
