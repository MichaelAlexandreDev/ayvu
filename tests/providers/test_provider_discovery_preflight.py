from datetime import datetime, timedelta

import pytest

from ayvu.jobs.execution import CancellationSource
from ayvu.providers.ports import ProviderRoute
from ayvu.providers.capabilities import AvailabilityState, Capability, NegotiationDecision, ProviderRequirements, SupportState
from ayvu.providers.discovery import DISCOVERY_SAFE_POINT, DiscoveredLanguage, DiscoveryOutcome, ProviderDiscoveryError
from ayvu.providers.network import ProviderOperation

from tests.providers.provider_discovery_support import Clock, FakeAdapter
from tests.providers.provider_discovery_support import NOW
from tests.providers.provider_discovery_support import decision, preflight
from tests.providers.provider_discovery_support import service


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
