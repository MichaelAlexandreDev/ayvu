from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ayvu.providers.ports import ProviderRoute
from ayvu.providers.capabilities import ProviderRequirements
from ayvu.providers.discovery import DiscoveryOutcome, ProviderDiscoveryError, ProviderDiscoveryService
from ayvu.providers.network import NetworkClass, NetworkDecisionError, ProviderOperation, network_binding

from tests.providers.provider_discovery_support import Clock, FakeAdapter, INSTANCE
from tests.providers.provider_discovery_support import NOW, REFERENCE
from tests.providers.provider_discovery_support import decision
from tests.providers.provider_discovery_support import service


def test_a_second_provider_works_through_the_port_alone():
    subject, adapter, _ = service()
    assert [item.instance_id for item in subject.list_providers()] == ['fake_local']
    assert subject.availability('fake_local', decision()).outcome is DiscoveryOutcome.OK
    assert subject.languages('fake_local', decision(ProviderOperation.LANGUAGES)).languages[0].name == 'Inglês'
    assert subject.resolve_route('fake_local', decision(ProviderOperation.ROUTE), 'en', 'pt').route == ProviderRoute('en', 'pt')
    assert subject.test_provider('fake_local', decision(ProviderOperation.TEST), 'en', 'pt').text == 'Ola'


def test_providers_are_listed_in_a_fixed_order():
    subject = ProviderDiscoveryService(trusted_issuers=frozenset(), policy_revision=1, clock=Clock())
    for name in ('zeta', 'alpha'):
        subject.register(replace(INSTANCE, instance_id=name), FakeAdapter(Clock()))
    assert [item.instance_id for item in subject.list_providers()] == ['alpha', 'zeta']


@pytest.mark.parametrize('instance,code', [
    (INSTANCE, 'duplicate_instance'), (replace(INSTANCE, instance_id='b', adapter_id='other'), 'incompatible_adapter'),
    (replace(INSTANCE, instance_id='b', adapter_version=9), 'incompatible_adapter'),
    (replace(INSTANCE, instance_id='b', config_version=9), 'incompatible_configuration'), ('x', 'invalid_instance'),
], ids=['duplicate', 'adapter', 'version', 'config', 'type'])
def test_registration_conflicts_are_distinct(instance, code):
    subject, _, clock = service()
    with pytest.raises(ProviderDiscoveryError, match=f'^{code}$'):
        subject.register(instance, FakeAdapter(clock))
    assert len(subject.list_providers()) == 1


@pytest.mark.parametrize('call', [
    lambda s, d: s.availability('fake_local', d), lambda s, d: s.languages('fake_local', d),
    lambda s, d: s.resolve_route('fake_local', d, 'en', 'pt'), lambda s, d: s.test_provider('fake_local', d, 'en', 'pt'),
    lambda s, d: s.preflight('fake_local', d, ProviderRequirements(()), 'en', 'pt'),
], ids=['availability', 'languages', 'route', 'test', 'preflight'])
@pytest.mark.parametrize('bad,code', [
    (None, 'network_decision_missing'), ('allow', 'network_decision_malformed'), ({'allowed': True}, 'network_decision_malformed'),
    (decision(issuer='someone_else'), 'network_decision_untrusted'),
    (decision(issued_at=NOW + timedelta(seconds=1), expires_at=NOW + timedelta(minutes=5)), 'network_decision_not_yet_valid'),
    (decision(issued_at=NOW - timedelta(minutes=5), expires_at=NOW), 'network_decision_expired'),
    (decision(binding='sha256:' + '0' * 64), 'network_decision_binding_mismatch'),
], ids=['missing', 'wrong-type', 'dict', 'issuer', 'future', 'expired', 'binding'])
def test_an_invalid_decision_stops_every_operation_with_zero_provider_io(call, bad, code):
    subject, adapter, _ = service()
    result = call(subject, bad)
    assert result.outcome is DiscoveryOutcome.POLICY_DENIED and result.code == code
    assert adapter.observed == 0 and adapter.executed == []


@pytest.mark.parametrize('operation,call', [
    (ProviderOperation.AVAILABILITY, lambda s, d: s.availability('fake_local', d)), (ProviderOperation.LANGUAGES, lambda s, d: s.languages('fake_local', d)),
    (ProviderOperation.ROUTE, lambda s, d: s.resolve_route('fake_local', d, 'en', 'pt')), (ProviderOperation.TEST, lambda s, d: s.test_provider('fake_local', d, 'en', 'pt')),
    (ProviderOperation.PREFLIGHT, lambda s, d: s.preflight('fake_local', d, ProviderRequirements(()), 'en', 'pt')),
], ids=lambda value: value.value if isinstance(value, ProviderOperation) else None)
def test_strict_offline_and_denied_decisions_perform_no_io(operation, call):
    subject, adapter, _ = service()
    denied = decision(operation, allowed=False, network_class=NetworkClass.STRICT_OFFLINE, reason_code='strict_offline')
    result = call(subject, denied)
    assert result.outcome is DiscoveryOutcome.POLICY_DENIED and result.code == 'strict_offline'
    assert adapter.observed == 0 and adapter.executed == []
    assert call(subject, decision(operation)).outcome is DiscoveryOutcome.OK


@pytest.mark.parametrize('operation,call', [
    (ProviderOperation.AVAILABILITY, lambda s, d: s.availability('fake_local', d)), (ProviderOperation.LANGUAGES, lambda s, d: s.languages('fake_local', d)),
    (ProviderOperation.ROUTE, lambda s, d: s.resolve_route('fake_local', d, 'en', 'pt')), (ProviderOperation.TEST, lambda s, d: s.test_provider('fake_local', d, 'en', 'pt')),
    (ProviderOperation.PREFLIGHT, lambda s, d: s.preflight('fake_local', d, ProviderRequirements(()), 'en', 'pt')),
], ids=lambda value: value.value if isinstance(value, ProviderOperation) else None)
def test_a_decision_from_another_policy_revision_is_refused_with_no_io(operation, call):
    subject, adapter, _ = service()
    result = call(subject, decision(operation, policy_revision=2))
    assert result.code == 'network_decision_revision_mismatch' and adapter.observed == 0 and adapter.executed == []


def test_a_decision_for_one_operation_cannot_be_replayed_for_another():
    subject, adapter, _ = service()
    for call in (lambda d: subject.languages('fake_local', d), lambda d: subject.test_provider('fake_local', d, 'en', 'pt')):
        assert call(decision(ProviderOperation.AVAILABILITY)).code == 'network_decision_binding_mismatch'
    assert adapter.observed == 0 and adapter.executed == []


def test_a_decision_for_another_provider_or_configuration_cannot_be_replayed():
    subject, adapter, _ = service()
    other_provider = decision(binding=network_binding('other', REFERENCE, ProviderOperation.AVAILABILITY))
    other_configuration = decision(binding=network_binding('fake_local', 'config:' + 'b' * 64, ProviderOperation.AVAILABILITY))
    for bad in (other_provider, other_configuration):
        assert subject.availability('fake_local', bad).code == 'network_decision_binding_mismatch'
    assert adapter.observed == 0


def test_a_decision_expires_while_the_service_lives():
    clock = Clock()
    subject, adapter, _ = service(clock=clock)
    item = decision()
    assert subject.availability('fake_local', item).outcome is DiscoveryOutcome.OK
    clock.now = NOW + timedelta(minutes=5)
    assert subject.availability('fake_local', item).code == 'network_decision_expired'
    assert adapter.observed == 1


@pytest.mark.parametrize('changes,code', [
    ({'version': 2}, 'unsupported_network_decision'), ({'allowed': 1}, 'malformed_network_decision'), ({'network_class': 'public'}, 'malformed_network_decision'),
    ({'reason_code': 'Has Space'}, 'malformed_network_decision'), ({'issuer': '../x'}, 'malformed_network_decision'), ({'binding': 'plain'}, 'malformed_network_decision'),
    ({'policy_revision': -1}, 'malformed_network_decision'), ({'policy_revision': True}, 'malformed_network_decision'), ({'policy_ref': 'plain'}, 'malformed_network_decision'),
    ({'issued_at': datetime(2026, 1, 1)}, 'malformed_network_decision'), ({'expires_at': NOW}, 'malformed_network_decision'),
], ids=range(11))
def test_malformed_decisions_cannot_be_built(changes, code):
    with pytest.raises(NetworkDecisionError, match=f'^{code}$'):
        decision(**changes)


def test_the_taxonomy_matches_the_threat_model_and_admits_no_guess():
    assert {item.value for item in NetworkClass} == {
        'strict_offline', 'loopback_only', 'local_network', 'private_remote', 'public_remote', 'unclassified'}
    assert 'https://' not in repr(decision()) and decision().policy_ref is None


def test_the_binding_is_deterministic_and_separates_every_field():
    base = network_binding('a', REFERENCE, ProviderOperation.TEST)
    assert base == network_binding('a', REFERENCE, ProviderOperation.TEST)
    assert len({base, network_binding('b', REFERENCE, ProviderOperation.TEST), network_binding('a', None, ProviderOperation.TEST),
                network_binding('a', REFERENCE, ProviderOperation.ROUTE)}) == 4


@pytest.mark.parametrize('name', ['absent', None, 7])
def test_an_unknown_instance_is_distinct(name):
    subject, adapter, _ = service()
    assert subject.availability(name, decision()).outcome is DiscoveryOutcome.UNKNOWN_INSTANCE
    assert subject.test_provider(name, decision(), 'en', 'pt').code == 'unknown_instance'
    assert subject.preflight(name, decision(), ProviderRequirements(()), 'en', 'pt').outcome is DiscoveryOutcome.UNKNOWN_INSTANCE
    with pytest.raises(ProviderDiscoveryError, match='^unknown_instance$'):
        subject.describe(name)
    assert adapter.observed == 0
