from datetime import datetime, timedelta

import pytest

from ayvu.providers.capabilities import MAX_LANGUAGE_TARGETS, MAX_LANGUAGES, NOT_PROBED, AvailabilityState, Capability, LanguageEvidence, NegotiationDecision, ProviderAvailability, ProviderCapabilityError, ProviderRequirements, SupportState, negotiate, observation, snapshot_projection
from ayvu.providers.service import ProviderAvailabilityProbe, observe_availability

from tests.providers.provider_capabilities_support import LATER, NOW
from tests.providers.provider_capabilities_support import TTL, available, facts, require
from tests.providers.provider_capabilities_support import snapshot


def test_supported_requirements_are_accepted():
    result = negotiate(snapshot(), require(Capability.BATCH, Capability.CONTEXT))
    assert result.decision is NegotiationDecision.ACCEPT and result.unsupported == () and result.unresolved == ()


def test_no_requirements_are_accepted():
    assert negotiate(snapshot(), require()).decision is NegotiationDecision.ACCEPT


def test_unsupported_requirement_rejects():
    subject = snapshot(facts=facts(batch=SupportState.UNSUPPORTED))
    result = negotiate(subject, require(Capability.BATCH, Capability.CONTEXT))
    assert result.decision is NegotiationDecision.REJECT and result.unsupported == (Capability.BATCH,)


@pytest.mark.parametrize('state', [SupportState.UNKNOWN, SupportState.CONFIGURATION_DEPENDENT])
def test_unresolved_requirement_needs_preflight_and_never_implies_support(state):
    subject = snapshot(facts=facts(batch=state))
    result = negotiate(subject, require(Capability.BATCH))
    assert result.decision is NegotiationDecision.PREFLIGHT_REQUIRED and result.unresolved == (Capability.BATCH,)


def test_rejection_takes_precedence_and_reports_both():
    subject = snapshot(facts=facts(batch=SupportState.UNSUPPORTED, context=SupportState.UNKNOWN))
    result = negotiate(subject, require(Capability.BATCH, Capability.CONTEXT), NOT_PROBED, now=NOW)
    assert result.decision is NegotiationDecision.REJECT
    assert (result.unsupported, result.unresolved, result.availability_reason) == ((Capability.BATCH,), (Capability.CONTEXT,), 'not_probed')


def test_a_capability_that_is_not_required_does_not_matter():
    subject = snapshot(facts=facts(batch=SupportState.UNSUPPORTED, context=SupportState.UNKNOWN))
    assert negotiate(subject, require(Capability.STREAMING)).decision is NegotiationDecision.ACCEPT


def test_fresh_availability_keeps_an_accept():
    result = negotiate(snapshot(), require(Capability.BATCH), available(), now=NOW)
    assert result.decision is NegotiationDecision.ACCEPT and result.availability_reason is None


@pytest.mark.parametrize('availability,now,reason', [
    (NOT_PROBED, NOW, 'not_probed'),
    (available(), NOW + TTL, 'evidence_expired'),
    (observation(AvailabilityState.UNAVAILABLE, 'provider_timeout', NOW, TTL), NOW, 'provider_timeout'),
    (observation(AvailabilityState.DEGRADED, 'no_languages', NOW, TTL), NOW, 'no_languages'),
], ids=['not-probed', 'expired', 'unavailable', 'degraded'])
def test_runtime_state_asks_for_preflight_and_never_rejects(availability, now, reason):
    result = negotiate(snapshot(), require(Capability.BATCH), availability, now=now)
    assert result.decision is NegotiationDecision.PREFLIGHT_REQUIRED and result.availability_reason == reason
    assert result.unsupported == ()


def test_negotiation_arguments_are_validated():
    for call in (lambda: negotiate('x', require()), lambda: negotiate(snapshot(), 'x'),
                 lambda: negotiate(snapshot(), require(), 'x', now=NOW), lambda: negotiate(snapshot(), require(), available())):
        with pytest.raises(ProviderCapabilityError, match='^invalid_negotiation$'):
            call()


@pytest.mark.parametrize('required', [['batch'], ('batch',), (Capability.BATCH, Capability.BATCH), None])
def test_invalid_requirements_are_refused(required):
    with pytest.raises(ProviderCapabilityError, match='^invalid_requirements$'):
        ProviderRequirements(required)


def test_requirements_have_a_canonical_order():
    assert require(Capability.CONTEXT, Capability.BATCH) == require(Capability.BATCH, Capability.CONTEXT)


def test_availability_expires_exactly_at_the_end_of_its_ttl():
    item = available()
    assert item.current(NOW + TTL - timedelta(microseconds=1)) is item
    expired = item.current(NOW + TTL)
    assert (expired.state, expired.reason_code, expired.languages) == (AvailabilityState.UNKNOWN, 'evidence_expired', ())
    assert expired.observed_at == NOW


def test_expiring_availability_does_not_change_the_original_or_the_snapshot():
    item, subject = available(), snapshot()
    before = snapshot_projection(subject)
    item.current(LATER)
    assert item.state is AvailabilityState.AVAILABLE and snapshot_projection(subject) == before


def test_not_probed_never_expires_and_stays_unknown():
    assert NOT_PROBED.current(LATER) is NOT_PROBED and NOT_PROBED.state is AvailabilityState.UNKNOWN


@pytest.mark.parametrize('arguments', [
    ('available', None), (AvailabilityState.AVAILABLE, 'reason', NOW, LATER, (LanguageEvidence('en'),)),
    (AvailabilityState.AVAILABLE, None, NOW, LATER, ()), (AvailabilityState.AVAILABLE, None, None, None, (LanguageEvidence('en'),)),
    (AvailabilityState.UNAVAILABLE, None, NOW, LATER), (AvailabilityState.UNAVAILABLE, 'Has Space', NOW, LATER),
    (AvailabilityState.UNAVAILABLE, 'reason'), (AvailabilityState.DEGRADED, 'reason'), (AvailabilityState.UNKNOWN, None),
    (AvailabilityState.UNKNOWN, 'reason', NOW, None), (AvailabilityState.UNKNOWN, 'reason', None, LATER),
    (AvailabilityState.UNKNOWN, 'reason', LATER, NOW), (AvailabilityState.UNKNOWN, 'reason', NOW, NOW),
    (AvailabilityState.UNKNOWN, 'reason', datetime(2026, 1, 1), datetime(2026, 1, 2)),
    (AvailabilityState.DEGRADED, 'reason', NOW, LATER, (LanguageEvidence('en'),)),
], ids=range(15))
def test_contradictory_availability_is_refused(arguments):
    with pytest.raises(ProviderCapabilityError, match='^invalid_availability$'):
        ProviderAvailability(*arguments)


@pytest.mark.parametrize('languages', [
    ['x'], ('x',), (LanguageEvidence('en'), LanguageEvidence('en')),
    tuple(LanguageEvidence(f'xx-{index:04d}') for index in range(MAX_LANGUAGES + 1)),
], ids=range(4))
def test_language_evidence_is_bounded_and_unique(languages):
    with pytest.raises(ProviderCapabilityError, match='^invalid_language_evidence$'):
        ProviderAvailability(AvailabilityState.AVAILABLE, None, NOW, LATER, languages)


@pytest.mark.parametrize('code,targets', [
    ('not a code', ()), (None, ()), ('en', ['pt']), ('en', ('not a code',)), ('en', (None,)),
    ('en', tuple('pt' for _ in range(MAX_LANGUAGE_TARGETS + 1))), ('/etc', ()), ('https://user:pw@host', ()),
])
def test_invalid_language_evidence_is_refused(code, targets):
    with pytest.raises(ProviderCapabilityError, match='^invalid_language_evidence$'):
        LanguageEvidence(code, targets)


@pytest.mark.parametrize('ttl', [timedelta(0), timedelta(seconds=-1), 60, None])
def test_observation_requires_a_positive_ttl(ttl):
    with pytest.raises(ProviderCapabilityError, match='^invalid_availability$'):
        observation(AvailabilityState.UNAVAILABLE, 'reason', NOW, ttl)


def test_observe_availability_passes_a_valid_answer_and_distrusts_a_broken_probe():
    class Good:
        def probe(self):
            return NOT_PROBED

    class Exploding:
        def probe(self):
            raise RuntimeError('https://user:hunter2@example.test')

    class Odd:
        def probe(self):
            return 'available'

    assert observe_availability(Good()) is NOT_PROBED
    for broken in (Exploding(), Odd()):
        result = observe_availability(broken)
        assert (result.state, result.reason_code) == (AvailabilityState.UNKNOWN, 'probe_failed')
        assert 'hunter2' not in repr(result)
    assert ProviderAvailabilityProbe is not None
