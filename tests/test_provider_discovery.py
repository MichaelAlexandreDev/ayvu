from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
from uuid import UUID

import pytest
import requests

from ayvu.job_execution import CancellationSource
from ayvu.provenance import ProducerIdentity
from ayvu.provider import DeliveryState, ProviderRoute, ProviderStatus, TranslationProviderResult
from ayvu.provider_capabilities import (
    NOT_PROBED, AvailabilityState, Capability, CapabilityFact, EvidenceSource, LanguageEvidence, NegotiationDecision,
    ProviderAvailability, ProviderCapabilitySnapshot, ProviderDescriptor, ProviderRequirements, SupportState, observation,
)
from ayvu.provider_discovery import (
    DISCOVERY_SAFE_POINT, SYNTHETIC_TEST_TEXT, DiscoveredLanguage, DiscoveryObservation, DiscoveryOutcome, Freshness,
    PolicyEligibility, ProviderDiscoveryError, ProviderDiscoveryService,
)
from ayvu.provider_libretranslate import ADAPTER_ID, ADAPTER_VERSION, LibreTranslateDiscoveryAdapter
from ayvu.provider_network import (
    NetworkClass, NetworkDecisionError, ProviderOperation, ResolvedProviderNetworkDecision, network_binding,
)
from ayvu.provider_service import ProviderInstance
from ayvu.translator import LibreTranslateTranslator
from provider_fakes import FakeSession, http_response

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
REFERENCE = 'config:' + 'a' * 64
ISSUER = 'test_composition'
PRODUCER = ProducerIdentity('fake_engine@1', 'fake_model@1', 'fake_adapter@1', 'not_applicable')
DESCRIPTOR = ProviderDescriptor('fake_engine', 3, 'fake_protocol', 1, (1,), (1, 2))
EVIDENCE = {SupportState.SUPPORTED: EvidenceSource.ADAPTER_CODE, SupportState.UNSUPPORTED: EvidenceSource.ADAPTER_CODE,
            SupportState.UNKNOWN: EvidenceSource.NONE, SupportState.CONFIGURATION_DEPENDENT: EvidenceSource.CONFIGURATION}
TTL = timedelta(seconds=60)
LANGUAGES = '[{"code": "en", "name": "English", "targets": ["pt", "fr"]}, {"code": "fr", "name": "French", "targets": ["en"]}, {"code": "pt", "name": "Portuguese", "targets": ["en"]}]'


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeProvider:
    def __init__(self, adapter):
        self.adapter = adapter

    def execute(self, request):
        self.adapter.executed.append(request)
        return TranslationProviderResult(request.request_id, ProviderStatus.SUCCEEDED, PRODUCER, DeliveryState.COMPLETED, text='Ola')

    def close(self):
        pass


class FakeAdapter:
    """A second provider, whose evidence is nothing like /languages, using only the port."""

    descriptor = DESCRIPTOR

    def __init__(self, clock, *, state=AvailabilityState.AVAILABLE, reason=None, states=None):
        self.clock, self.state, self.reason = clock, state, reason
        self.states = states or {}
        self.observed = 0
        self.executed = []

    def snapshot(self):
        facts = tuple(CapabilityFact(item, state, EVIDENCE[state]) for item in Capability
                      for state in [self.states.get(item, SupportState.SUPPORTED)])
        return ProviderCapabilitySnapshot(DESCRIPTOR, facts, (), self.clock())

    def observe(self):
        self.observed += 1
        if self.state is AvailabilityState.AVAILABLE:
            evidence = (LanguageEvidence('en', ('pt',)),)
            return DiscoveryObservation(observation(self.state, None, self.clock(), TTL, evidence), (DiscoveredLanguage('en', 'Inglês', ('pt',)),))
        return DiscoveryObservation(observation(self.state, self.reason, self.clock(), TTL))

    def route(self, availability, source, target, now, candidates):
        return ProviderRoute(source, target) if (source, target) == ('en', 'pt') else None

    def provider(self):
        return FakeProvider(self)


INSTANCE = ProviderInstance('fake_local', 'fake_engine', 3, 1, REFERENCE)


def decision(operation=ProviderOperation.AVAILABILITY, **changes):
    values = {'allowed': True, 'network_class': NetworkClass.UNCLASSIFIED, 'reason_code': 'test_allowed',
              'binding': network_binding('fake_local', REFERENCE, operation), 'policy_revision': 1, 'issuer': ISSUER,
              'issued_at': NOW, 'expires_at': NOW + timedelta(minutes=5), **changes}
    return ResolvedProviderNetworkDecision(**values)


def service(adapter=None, clock=None, instance=INSTANCE):
    clock = clock or Clock()
    subject = ProviderDiscoveryService(trusted_issuers=frozenset({ISSUER}), policy_revision=1, clock=clock,
                                       new_id=lambda: str(UUID(int=42)))
    adapter = adapter or FakeAdapter(clock)
    subject.register(instance, adapter)
    return subject, adapter, clock


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
    assert result.outcome is DiscoveryOutcome.POLICY_DENIED and result.code == 'network_decision_denied'
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


def test_describe_keeps_the_four_kinds_of_fact_apart_and_uses_no_network():
    subject, adapter, clock = service()
    description = subject.describe('fake_local')
    assert description.descriptor is DESCRIPTOR and description.capabilities.state_of(Capability.BATCH) is SupportState.SUPPORTED
    assert description.availability is NOT_PROBED and description.eligibility is PolicyEligibility.NOT_EVALUATED
    assert adapter.observed == 0 and adapter.executed == []
    item = decision(ProviderOperation.PREFLIGHT)
    assert subject.describe('fake_local', item).eligibility is PolicyEligibility.ELIGIBLE
    denied = subject.describe('fake_local', decision(ProviderOperation.PREFLIGHT, allowed=False))
    assert (denied.eligibility, denied.eligibility_reason) == (PolicyEligibility.DENIED, 'network_decision_denied')
    assert adapter.observed == 0


def test_describe_reports_last_evidence_until_it_expires():
    subject, adapter, clock = service()
    subject.availability('fake_local', decision())
    assert subject.describe('fake_local').availability.state is AvailabilityState.AVAILABLE
    clock.now = NOW + TTL
    stale = subject.describe('fake_local').availability
    assert (stale.state, stale.reason_code) == (AvailabilityState.UNKNOWN, 'evidence_expired')


@pytest.mark.parametrize('state,reason,outcome', [
    (AvailabilityState.UNAVAILABLE, 'provider_timeout', DiscoveryOutcome.UNAVAILABLE), (AvailabilityState.DEGRADED, 'no_languages', DiscoveryOutcome.DEGRADED),
    (AvailabilityState.UNKNOWN, 'evidence_gone', DiscoveryOutcome.UNKNOWN),
])
def test_runtime_states_stay_distinct_and_never_change_capabilities(state, reason, outcome):
    clock = Clock()
    adapter = FakeAdapter(clock, state=state, reason=reason)
    subject, _, _ = service(adapter, clock)
    before = subject.describe('fake_local').capabilities
    assert subject.availability('fake_local', decision()).outcome is outcome
    languages = subject.languages('fake_local', decision(ProviderOperation.LANGUAGES))
    assert (languages.outcome, languages.code, languages.languages) == (outcome, reason, ())
    route = subject.resolve_route('fake_local', decision(ProviderOperation.ROUTE), 'en', 'pt')
    assert (route.outcome, route.route, route.code) == (outcome, None, reason)
    assert subject.describe('fake_local').capabilities == before


def test_availability_is_fresh_unless_reuse_is_asked_for_and_still_valid():
    subject, adapter, clock = service()
    assert subject.availability('fake_local', decision()).freshness is Freshness.FRESH
    assert subject.availability('fake_local', decision()).freshness is Freshness.FRESH and adapter.observed == 2
    assert subject.availability('fake_local', decision(), reuse_fresh=True).freshness is Freshness.CACHED and adapter.observed == 2
    clock.now = NOW + TTL
    item = decision(issued_at=NOW, expires_at=NOW + timedelta(days=1))
    assert subject.availability('fake_local', item, reuse_fresh=True).freshness is Freshness.FRESH and adapter.observed == 3


def test_reuse_still_checks_the_decision():
    subject, adapter, _ = service()
    subject.availability('fake_local', decision())
    assert subject.availability('fake_local', decision(allowed=False), reuse_fresh=True).outcome is DiscoveryOutcome.POLICY_DENIED


class BrokenAdapter(FakeAdapter):
    def observe(self):
        raise RuntimeError('https://user:hunter2@example.test Call me Ishmael.')


class OddAdapter(FakeAdapter):
    def observe(self):
        return 'available'


@pytest.mark.parametrize('kind', [BrokenAdapter, OddAdapter])
def test_a_broken_adapter_proves_nothing_and_leaks_nothing(kind, caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    subject, _, _ = service(kind(clock), clock)
    result = subject.availability('fake_local', decision())
    assert (result.outcome, result.code) == (DiscoveryOutcome.UNKNOWN, 'probe_failed')
    assert 'hunter2' not in repr(result) and 'hunter2' not in caplog.text


def test_route_outcomes_are_distinct():
    subject, _, _ = service()
    assert subject.resolve_route('fake_local', decision(ProviderOperation.ROUTE), 'fr', 'de').outcome is DiscoveryOutcome.NO_ROUTE


def test_a_broken_route_is_not_a_missing_route():
    class Broken(FakeAdapter):
        def route(self, *args):
            raise RuntimeError('secret')

    clock = Clock()
    subject, _, _ = service(Broken(clock), clock)
    result = subject.resolve_route('fake_local', decision(ProviderOperation.ROUTE), 'en', 'pt')
    assert (result.outcome, result.code) == (DiscoveryOutcome.UNKNOWN, 'route_failed')


def test_the_test_sends_only_the_synthetic_text():
    subject, adapter, _ = service()
    result = subject.test_provider('fake_local', decision(ProviderOperation.TEST), 'en', 'pt')
    assert [request.text for request in adapter.executed] == [SYNTHETIC_TEST_TEXT] == ['Hello world']
    assert adapter.executed[0].source_locale == 'en' and adapter.executed[0].target_locale == 'pt' and adapter.observed == 0
    assert (result.outcome, result.text, result.producer) == (DiscoveryOutcome.OK, 'Ola', PRODUCER)
    assert 'Ola' not in repr(result)


def test_a_malformed_provider_result_is_a_failure():
    class Odd(FakeAdapter):
        def provider(self):
            class Provider:
                def execute(self, request):
                    return 'Ola'

                def close(self):
                    pass
            return Provider()

    clock = Clock()
    subject, _, _ = service(Odd(clock), clock)
    result = subject.test_provider('fake_local', decision(ProviderOperation.TEST), 'en', 'pt')
    assert (result.outcome, result.text, result.code) == (DiscoveryOutcome.FAILED, None, 'malformed_provider_result')


def test_a_provider_that_cannot_be_created_is_a_failure_without_its_message():
    class Failing(FakeAdapter):
        def provider(self):
            raise RuntimeError('hunter2')

    clock = Clock()
    subject, _, _ = service(Failing(clock), clock)
    result = subject.test_provider('fake_local', decision(ProviderOperation.TEST), 'en', 'pt')
    assert result.code == 'provider_unavailable_to_create' and 'hunter2' not in repr(result.failure)


def preflight(subject, requirements=(), source='en', target='pt', **options):
    return subject.preflight('fake_local', decision(ProviderOperation.PREFLIGHT), ProviderRequirements(tuple(requirements)), source, target, **options)


def test_preflight_accepts_and_returns_the_route_without_sending_text():
    subject, adapter, _ = service()
    result = preflight(subject, [Capability.BATCH])
    assert (result.outcome, result.route, result.negotiation) == (DiscoveryOutcome.OK, ProviderRoute('en', 'pt'), NegotiationDecision.ACCEPT)
    assert adapter.executed == [] and adapter.observed == 1


def test_preflight_rejects_an_unsupported_requirement_before_any_io_or_decision():
    clock = Clock()
    adapter = FakeAdapter(clock, states={Capability.BATCH: SupportState.UNSUPPORTED})
    subject, _, _ = service(adapter, clock)
    result = subject.preflight('fake_local', None, ProviderRequirements((Capability.BATCH,)), 'en', 'pt')
    assert (result.outcome, result.code, result.negotiation) == (DiscoveryOutcome.UNSUPPORTED, 'unsupported_requirement', NegotiationDecision.REJECT)
    assert adapter.observed == 0


@pytest.mark.parametrize('state', [SupportState.UNKNOWN, SupportState.CONFIGURATION_DEPENDENT])
def test_preflight_never_reads_an_unknown_as_support(state):
    clock = Clock()
    adapter = FakeAdapter(clock, states={Capability.BATCH: state})
    subject, _, _ = service(adapter, clock)
    result = preflight(subject, [Capability.BATCH])
    assert (result.outcome, result.code) == (DiscoveryOutcome.INDETERMINATE, 'unresolved_requirement')
    assert result.negotiation is NegotiationDecision.PREFLIGHT_REQUIRED and result.route == ProviderRoute('en', 'pt')


def test_preflight_reports_unavailable_and_no_route_distinctly():
    clock = Clock()
    subject, _, _ = service(FakeAdapter(clock, state=AvailabilityState.UNAVAILABLE, reason='provider_timeout'), clock)
    assert preflight(subject).outcome is DiscoveryOutcome.UNAVAILABLE
    subject, _, _ = service()
    assert preflight(subject, source='fr', target='de').outcome is DiscoveryOutcome.NO_ROUTE


@pytest.mark.parametrize('call', [
    lambda s, **o: s.availability('fake_local', decision(), **o), lambda s, **o: s.languages('fake_local', decision(ProviderOperation.LANGUAGES), **o),
    lambda s, **o: s.resolve_route('fake_local', decision(ProviderOperation.ROUTE), 'en', 'pt', **o),
    lambda s, **o: s.test_provider('fake_local', decision(ProviderOperation.TEST), 'en', 'pt', **o),
    lambda s, **o: preflight(s, **o),
], ids=['availability', 'languages', 'route', 'test', 'preflight'])
def test_cancellation_and_deadlines_stop_every_operation_before_a_request(call):
    subject, adapter, _ = service()
    source = CancellationSource((DISCOVERY_SAFE_POINT,))
    source.request('user_requested', NOW)
    result = call(subject, cancellation=source.token)
    assert (result.outcome, result.code) == (DiscoveryOutcome.CANCELLED, 'cancelled_before_request')
    assert source.acknowledgement.safe_point == DISCOVERY_SAFE_POINT
    expired = call(subject, deadline=NOW)
    assert (expired.outcome, expired.code) == (DiscoveryOutcome.DEADLINE_EXPIRED, 'deadline_expired')
    assert adapter.observed == 0 and adapter.executed == []
    assert call(subject, deadline=NOW + timedelta(seconds=1)).outcome is DiscoveryOutcome.OK


def test_a_deadline_must_be_utc():
    subject, adapter, _ = service()
    with pytest.raises(ProviderDiscoveryError, match='^invalid_deadline$'):
        subject.availability('fake_local', decision(), deadline=datetime(2026, 1, 1))
    assert adapter.observed == 0


@pytest.mark.parametrize('name', ['', None, 7, 'x' * 257, 'line\nbreak', '\x7f'], ids=repr)
def test_language_names_are_bounded_display_text(name):
    with pytest.raises(ProviderDiscoveryError, match='^invalid_language_name$'):
        DiscoveredLanguage('en', name)


def test_language_names_stay_out_of_repr_and_evidence():
    language = DiscoveredLanguage('en', 'Secret server label')
    assert 'Secret' not in repr(language)


def libre(get_responses=(), posts=(), url='http://localhost:5000'):
    base = LibreTranslateTranslator(url=url, timeout=3.0, retries=0)
    base.session = FakeSession(posts, get_responses)
    clock = Clock()
    subject = ProviderDiscoveryService(trusted_issuers=frozenset({ISSUER}), policy_revision=1, clock=clock, new_id=lambda: str(UUID(int=42)))
    subject.register(ProviderInstance('libre_local', ADAPTER_ID, ADAPTER_VERSION, 1), LibreTranslateDiscoveryAdapter(base, clock=clock))
    return subject, base


def libre_decision(operation):
    return decision(operation, binding=network_binding('libre_local', None, operation))


def test_libretranslate_languages_keep_the_servers_order_and_names():
    subject, base = libre([http_response(200, LANGUAGES)])
    result = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    assert [(item.code, item.name, item.targets, item.state) for item in result.languages] == [
        ('en', 'English', ('pt', 'fr'), 'installed'), ('fr', 'French', ('en',), 'installed'), ('pt', 'Portuguese', ('en',), 'installed')]
    assert base.session.gets == [('http://localhost:5000/languages', 3.0)] and base.session.posts == []


@pytest.mark.parametrize('source,target,route', [
    ('en', 'pt', ProviderRoute('en', 'pt')), ('fr', 'pt', ProviderRoute('fr', 'pt', 'en')), ('de', 'pt', None),
])
def test_libretranslate_routes_come_from_live_language_data(source, target, route):
    subject, base = libre([http_response(200, LANGUAGES)])
    result = subject.resolve_route('libre_local', libre_decision(ProviderOperation.ROUTE), source, target)
    assert result.route == route and base.session.posts == []
    assert result.outcome is (DiscoveryOutcome.OK if route else DiscoveryOutcome.NO_ROUTE)


def test_libretranslate_test_sends_exactly_the_known_text():
    subject, base = libre(posts=[http_response(200, '{"translatedText": "Ola mundo"}')])
    result = subject.test_provider('libre_local', libre_decision(ProviderOperation.TEST), 'en', 'pt')
    assert result.text == 'Ola mundo' and base.session.gets == []
    assert base.session.posts == [('http://localhost:5000/translate', {'q': 'Hello world', 'source': 'en', 'target': 'pt', 'format': 'text'}, 3.0)]


@pytest.mark.parametrize('response,outcome,code', [
    (requests.exceptions.Timeout(), DiscoveryOutcome.UNAVAILABLE, 'provider_timeout'), (requests.exceptions.ConnectionError(), DiscoveryOutcome.UNAVAILABLE, 'provider_unavailable'),
    (http_response(401, 'no'), DiscoveryOutcome.UNAVAILABLE, 'provider_unauthorized'), (http_response(429, 'slow'), DiscoveryOutcome.UNAVAILABLE, 'provider_rejected'),
    (http_response(500, 'oops'), DiscoveryOutcome.UNAVAILABLE, 'provider_server_error'), (http_response(200, 'not json'), DiscoveryOutcome.DEGRADED, 'malformed_languages'),
    (http_response(200, '[]'), DiscoveryOutcome.DEGRADED, 'no_languages'), (http_response(200, '[{"code": "en", "name": "line\\nbreak"}]'), DiscoveryOutcome.DEGRADED, 'malformed_languages'),
], ids=lambda value: value if isinstance(value, str) else None)
def test_libretranslate_discovery_failures_are_typed(response, outcome, code):
    subject, base = libre([response])
    result = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    assert (result.outcome, result.code, result.languages) == (outcome, code, ())
    assert base.session.posts == [] and not any('health' in url for url, _ in base.session.gets)


@pytest.mark.parametrize('response,code', [
    (requests.exceptions.Timeout(), 'provider_timeout'), (http_response(401, 'no'), 'provider_unauthorized'), (http_response(429, 'slow'), 'provider_rate_limited'),
    (http_response(500, 'oops'), 'provider_server_error'), (http_response(400, 'bad'), 'provider_rejected'), (http_response(200, 'not json'), 'malformed_response'),
    (http_response(200, '{"translatedText": ""}'), 'empty_result'),
], ids=lambda value: value if isinstance(value, str) else None)
def test_libretranslate_test_failures_are_typed(response, code):
    subject, _ = libre(posts=[response])
    result = subject.test_provider('libre_local', libre_decision(ProviderOperation.TEST), 'en', 'pt')
    assert (result.outcome, result.code, result.text) == (DiscoveryOutcome.FAILED, code, None)


def test_libretranslate_preflight_makes_no_translation_request():
    subject, base = libre([http_response(200, LANGUAGES)])
    result = subject.preflight('libre_local', libre_decision(ProviderOperation.PREFLIGHT),
                               ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.ROUTE_DISCOVERY)), 'fr', 'pt')
    assert result.outcome is DiscoveryOutcome.OK and result.route == ProviderRoute('fr', 'pt', 'en')
    assert base.session.posts == [] and [url for url, _ in base.session.gets] == ['http://localhost:5000/languages']


def test_libretranslate_description_makes_no_request():
    subject, base = libre()
    description = subject.describe('libre_local')
    assert description.capabilities.state_of(Capability.CONTEXT) is SupportState.UNSUPPORTED
    assert base.session.gets == [] and base.session.posts == []


@pytest.mark.parametrize('body', ['Call me Ishmael.', '{"error": "Translate this prompt"}', '/home/reader/private/book.epub'])
def test_canaries_stay_out_of_results_and_logs(body, caplog):
    caplog.set_level(logging.DEBUG)
    url = 'http://user:hunter2@private.example.test'
    subject, _ = libre([http_response(500, body), http_response(200, '[{"code": "en", "name": "Secret label", "targets": ["pt"]}]')], posts=[http_response(500, body)], url=url)
    languages = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    test = subject.test_provider('libre_local', libre_decision(ProviderOperation.TEST), 'en', 'pt')
    listed = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    texts = [repr(languages), repr(test), repr(test.failure), repr(subject), caplog.text, str(languages.code), str(test.code),
             repr(subject.describe('libre_local')), repr(libre_decision(ProviderOperation.TEST)), repr(listed.languages)]
    for secret in (body, 'hunter2', 'private.example.test', 'Secret label'):
        assert not any(secret in text for text in texts)


def test_contract_modules_name_no_provider_and_read_no_url():
    from pathlib import Path
    root = Path(__file__).parents[1] / 'src' / 'ayvu'
    for name in ('provider_discovery.py', 'provider_network.py'):
        source = (root / name).read_text()
        assert 'libretranslate' not in source.lower() and 'urlparse' not in source and 'urllib' not in source and 'localhost' not in source
        assert 'import requests' not in source and '/health' not in source


def test_a_provider_that_cancels_is_reported_as_cancelled():
    from ayvu.job_events import JobFailure, JobFailureFamily
    from ayvu.provider_service import failed_result

    class Cancelling(FakeAdapter):
        def provider(self):
            class Provider:
                def execute(self, request):
                    return failed_result(request, JobFailure(JobFailureFamily.CANCELLED, 'stopped'), delivery=DeliveryState.NOT_SENT)

                def close(self):
                    pass
            return Provider()

    clock = Clock()
    subject, _, _ = service(Cancelling(clock), clock)
    result = subject.test_provider('fake_local', decision(ProviderOperation.TEST), 'en', 'pt')
    assert (result.outcome, result.code, result.text) == (DiscoveryOutcome.CANCELLED, 'stopped', None)


def test_libretranslate_routes_use_only_the_given_candidates():
    subject, _ = libre([http_response(200, LANGUAGES)])
    result = subject.resolve_route('libre_local', libre_decision(ProviderOperation.ROUTE), 'fr', 'pt', candidates=('es',))
    assert result.outcome is DiscoveryOutcome.NO_ROUTE and result.route is None
