from datetime import timedelta
import logging

import pytest

from ayvu.providers.ports import DeliveryState
from ayvu.providers.capabilities import NOT_PROBED, AvailabilityState, Capability, SupportState
from ayvu.providers.discovery import SYNTHETIC_TEST_TEXT, DiscoveryOutcome, Freshness, PolicyEligibility
from ayvu.providers.network import ProviderOperation
from ayvu.kernel import JobFailure, JobFailureFamily

from tests.providers.provider_discovery_support import BrokenAdapter, Clock, DESCRIPTOR, FakeAdapter
from tests.providers.provider_discovery_support import NOW, OddAdapter, PRODUCER
from tests.providers.provider_discovery_support import TTL, decision
from tests.providers.provider_discovery_support import service


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


def test_a_provider_that_cancels_is_reported_as_cancelled():
    from ayvu.providers.service import failed_result

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
