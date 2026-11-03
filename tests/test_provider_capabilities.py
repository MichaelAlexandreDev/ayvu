from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import math

import pytest

from ayvu.provider_capabilities import (
    FEATURE_CAPABILITIES, FINGERPRINT_CAPABILITIES, MAX_LANGUAGE_TARGETS, MAX_LANGUAGES, MAX_LIMITS, NOT_PROBED,
    AvailabilityState, Capability, CapabilityFact, EvidenceSource, LanguageEvidence, LimitOrigin, LimitScope,
    LimitSemantics, LimitUnit, NegotiationDecision, ProviderAvailability, ProviderCapabilityError,
    ProviderCapabilitySnapshot, ProviderDescriptor, ProviderLimit, ProviderRequirements, SupportState,
    availability_projection, capability_fingerprint, fingerprint_components, negotiate, observation, snapshot_projection,
)
from ayvu.provider import ContextFeature, ConstraintsFeature, DeadlineFeature, ProtectedPartsFeature
from ayvu.provider_service import ProviderAvailabilityProbe, observe_availability

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
TTL = timedelta(seconds=60)
REFERENCE = 'config:' + 'a' * 64
DESCRIPTOR = ProviderDescriptor('fake_engine', 3, 'fake_protocol', 1, (2, 1), (1,))
EVIDENCE = {SupportState.SUPPORTED: EvidenceSource.ADAPTER_CODE, SupportState.UNSUPPORTED: EvidenceSource.ADAPTER_CODE,
            SupportState.UNKNOWN: EvidenceSource.NONE, SupportState.CONFIGURATION_DEPENDENT: EvidenceSource.CONFIGURATION}
SERVER_LIMIT = ProviderLimit('server_max_input_chars', LimitUnit.CHARACTERS, LimitScope.REQUEST, LimitOrigin.SERVER,
                             LimitSemantics.KNOWN, EvidenceSource.PROVIDER_DOCUMENTATION, 5000)
CLIENT_LIMIT = ProviderLimit('client_rate', LimitUnit.REQUESTS_PER_SECOND, LimitScope.SHARED, LimitOrigin.CLIENT_POLICY,
                             LimitSemantics.KNOWN, EvidenceSource.CONFIGURATION, 2.5)


def facts(**states):
    return tuple(CapabilityFact(item, state, EVIDENCE[state])
                 for item in Capability for state in [states.get(item.value, SupportState.SUPPORTED)])


def snapshot(**changes):
    return ProviderCapabilitySnapshot(**{'descriptor': DESCRIPTOR, 'facts': facts(), 'limits': (SERVER_LIMIT, CLIENT_LIMIT),
                                         'observed_at': NOW, **changes})


def available(now=NOW):
    return observation(AvailabilityState.AVAILABLE, None, now, TTL, (LanguageEvidence('en', ('pt',)),))


def require(*capabilities):
    return ProviderRequirements(tuple(capabilities))


def test_serialized_states_are_exact():
    assert {item.value for item in SupportState} == {'supported', 'unsupported', 'unknown', 'configuration_dependent'}
    assert {item.value for item in AvailabilityState} == {'available', 'degraded', 'unavailable', 'unknown'}
    assert {item.value for item in NegotiationDecision} == {'accept', 'reject', 'preflight_required'}


def test_every_scoped_capability_exists():
    assert {'language_discovery', 'route_discovery', 'plain_text_input', 'plain_text_output', 'context', 'batch', 'streaming',
            'structured_output', 'native_glossary', 'protected_parts', 'multimodal_input', 'shared_instance_concurrency',
            'in_flight_cancellation', 'network_class', 'authentication', 'retention_claim', 'training_claim', 'cost_reporting',
            'resource_reporting'} <= {item.value for item in Capability}


def test_every_request_feature_maps_to_a_capability():
    assert set(FEATURE_CAPABILITIES) == {item.name for item in (
        ContextFeature, ConstraintsFeature, DeadlineFeature, ProtectedPartsFeature)}


@pytest.mark.parametrize('state', list(SupportState))
def test_each_support_state_has_exactly_its_valid_evidence(state):
    for evidence in EvidenceSource:
        valid = {SupportState.UNKNOWN: evidence is EvidenceSource.NONE,
                 SupportState.CONFIGURATION_DEPENDENT: evidence is EvidenceSource.CONFIGURATION}.get(state, evidence is not EvidenceSource.NONE)
        if valid:
            assert CapabilityFact(Capability.BATCH, state, evidence).state is state
        else:
            with pytest.raises(ProviderCapabilityError, match='^contradictory_capability_claim$'):
                CapabilityFact(Capability.BATCH, state, evidence)


@pytest.mark.parametrize('capability,state,evidence', [
    ('batch', SupportState.SUPPORTED, EvidenceSource.ADAPTER_CODE),
    (Capability.BATCH, 'supported', EvidenceSource.ADAPTER_CODE),
    (Capability.BATCH, SupportState.SUPPORTED, 'adapter_code'),
])
def test_facts_are_typed(capability, state, evidence):
    with pytest.raises(ProviderCapabilityError, match='^invalid_capability_fact$'):
        CapabilityFact(capability, state, evidence)


def test_absent_capability_never_implies_support():
    partial = tuple(item for item in facts() if item.capability is not Capability.BATCH)
    with pytest.raises(ProviderCapabilityError, match='^incomplete_snapshot$'):
        snapshot(facts=partial)
    with pytest.raises(ProviderCapabilityError, match='^incomplete_snapshot$'):
        snapshot(facts=())


def test_a_capability_cannot_be_stated_twice():
    duplicated = facts() + (CapabilityFact(Capability.BATCH, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),)
    with pytest.raises(ProviderCapabilityError, match='^incomplete_snapshot$'):
        snapshot(facts=duplicated)


@pytest.mark.parametrize('changes,code', [
    ({'schema_version': 2}, 'unsupported_capability_schema'), ({'schema_version': True}, 'unsupported_capability_schema'),
    ({'descriptor': 'x'}, 'invalid_descriptor'), ({'facts': list(facts())}, 'invalid_capability_fact'),
    ({'facts': ('x',)}, 'invalid_capability_fact'), ({'limits': [SERVER_LIMIT]}, 'invalid_limit'), ({'limits': ('x',)}, 'invalid_limit'),
    ({'limits': (SERVER_LIMIT, SERVER_LIMIT)}, 'invalid_limit'),
    ({'limits': tuple(replace(CLIENT_LIMIT, name=f'l{i}') for i in range(MAX_LIMITS + 1))}, 'invalid_limit'),
    ({'observed_at': datetime(2026, 11, 3)}, 'invalid_observation_time'), ({'observed_at': 'now'}, 'invalid_observation_time'),
    ({'observed_at': datetime(2026, 11, 3, tzinfo=timezone(timedelta(hours=1)))}, 'invalid_observation_time'),
    ({'configuration_ref': 'plain'}, 'invalid_reference'), ({'configuration_ref': 'http://user:pw@host'}, 'invalid_reference'),
], ids=lambda value: value if isinstance(value, str) and '_' in value else None)
def test_invalid_snapshots_fail_with_bounded_codes(changes, code):
    with pytest.raises(ProviderCapabilityError, match=f'^{code}$'):
        snapshot(**changes)


def test_snapshot_order_is_canonical_and_lookup_works():
    forward = snapshot()
    backward = snapshot(facts=tuple(reversed(facts())), limits=(CLIENT_LIMIT, SERVER_LIMIT))
    assert forward == backward and forward.state_of(Capability.BATCH) is SupportState.SUPPORTED


@pytest.mark.parametrize('changes', [
    {'adapter_id': 'Bad Name'}, {'protocol_id': '../x'}, {'adapter_version': 0}, {'adapter_version': True}, {'protocol_version': 0},
    {'request_schema_versions': ()}, {'request_schema_versions': [1]}, {'config_versions': (1, 1)}, {'config_versions': (0,)},
])
def test_invalid_descriptors_are_refused(changes):
    with pytest.raises(ProviderCapabilityError, match='^invalid_descriptor$'):
        replace(DESCRIPTOR, **changes)


def test_descriptor_versions_are_canonical_and_frozen():
    assert DESCRIPTOR.request_schema_versions == (1, 2)
    with pytest.raises(FrozenInstanceError):
        DESCRIPTOR.adapter_version = 9


@pytest.mark.parametrize('origin,semantics,evidence,value,valid', [
    (LimitOrigin.SERVER, LimitSemantics.KNOWN, EvidenceSource.RUNTIME_OBSERVATION, 5, True),
    (LimitOrigin.SERVER, LimitSemantics.KNOWN, EvidenceSource.PROVIDER_DOCUMENTATION, 5, True),
    (LimitOrigin.SERVER, LimitSemantics.KNOWN, EvidenceSource.CONFIGURATION, 5, False),
    (LimitOrigin.SERVER, LimitSemantics.KNOWN, EvidenceSource.ADAPTER_CODE, 5, False),
    (LimitOrigin.SERVER, LimitSemantics.KNOWN, EvidenceSource.NONE, 5, False),
    (LimitOrigin.SERVER, LimitSemantics.UNKNOWN, EvidenceSource.NONE, None, True),
    (LimitOrigin.SERVER, LimitSemantics.UNKNOWN, EvidenceSource.CONFIGURATION, None, False),
    (LimitOrigin.SERVER, LimitSemantics.UNBOUNDED, EvidenceSource.PROVIDER_DOCUMENTATION, None, True),
    (LimitOrigin.SERVER, LimitSemantics.UNBOUNDED, EvidenceSource.CONFIGURATION, None, False),
    (LimitOrigin.CLIENT_POLICY, LimitSemantics.KNOWN, EvidenceSource.CONFIGURATION, 5, True),
    (LimitOrigin.CLIENT_POLICY, LimitSemantics.KNOWN, EvidenceSource.RUNTIME_OBSERVATION, 5, False),
    (LimitOrigin.CLIENT_POLICY, LimitSemantics.UNBOUNDED, EvidenceSource.CONFIGURATION, None, True),
    (LimitOrigin.CLIENT_POLICY, LimitSemantics.UNBOUNDED, EvidenceSource.NONE, None, False),
])
def test_a_client_setting_is_never_a_discovered_server_limit(origin, semantics, evidence, value, valid):
    build = lambda: ProviderLimit('limit', LimitUnit.REQUESTS, LimitScope.SHARED, origin, semantics, evidence, value)
    if valid:
        assert build().origin is origin
    else:
        with pytest.raises(ProviderCapabilityError, match='^contradictory_limit_claim$'):
            build()


@pytest.mark.parametrize('semantics,value', [
    (LimitSemantics.KNOWN, None), (LimitSemantics.KNOWN, -1), (LimitSemantics.KNOWN, True), (LimitSemantics.KNOWN, '5'),
    (LimitSemantics.KNOWN, math.inf), (LimitSemantics.KNOWN, math.nan), (LimitSemantics.UNBOUNDED, 5), (LimitSemantics.UNKNOWN, 0),
])
def test_limit_values_match_their_semantics(semantics, value):
    with pytest.raises(ProviderCapabilityError, match='^invalid_limit$'):
        ProviderLimit('limit', LimitUnit.REQUESTS, LimitScope.SHARED, LimitOrigin.CLIENT_POLICY, semantics, EvidenceSource.CONFIGURATION, value)


@pytest.mark.parametrize('position,value', [(0, 'Bad Name'), (1, 'requests'), (2, 'shared'), (3, 'server'), (4, 'known'), (5, 'none')])
def test_limit_fields_are_typed(position, value):
    arguments = ['limit', LimitUnit.REQUESTS, LimitScope.SHARED, LimitOrigin.CLIENT_POLICY, LimitSemantics.KNOWN, EvidenceSource.CONFIGURATION, 5]
    arguments[position] = value
    with pytest.raises(ProviderCapabilityError, match='^invalid_limit$'):
        ProviderLimit(*arguments)


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


@pytest.mark.parametrize('capability', sorted(FINGERPRINT_CAPABILITIES))
def test_fingerprint_changes_for_each_output_or_safety_relevant_fact(capability):
    changed = snapshot(facts=facts(**{capability.value: SupportState.UNSUPPORTED}))
    assert capability_fingerprint(changed) != capability_fingerprint(snapshot())


@pytest.mark.parametrize('capability', sorted(set(Capability) - FINGERPRINT_CAPABILITIES))
def test_fingerprint_ignores_operational_facts(capability):
    changed = snapshot(facts=facts(**{capability.value: SupportState.UNSUPPORTED}))
    assert capability_fingerprint(changed) == capability_fingerprint(snapshot())


@pytest.mark.parametrize('changes', [
    {'configuration_ref': REFERENCE}, {'descriptor': replace(DESCRIPTOR, adapter_id='other')},
    {'descriptor': replace(DESCRIPTOR, adapter_version=4)}, {'descriptor': replace(DESCRIPTOR, protocol_id='other')},
    {'descriptor': replace(DESCRIPTOR, protocol_version=2)}, {'limits': (replace(SERVER_LIMIT, value=9999), CLIENT_LIMIT)},
], ids=range(6))
def test_fingerprint_changes_with_identity_configuration_and_server_limits(changes):
    assert capability_fingerprint(snapshot(**changes)) != capability_fingerprint(snapshot())


@pytest.mark.parametrize('changes', [
    {'observed_at': LATER}, {'limits': (SERVER_LIMIT, replace(CLIENT_LIMIT, value=99.0))}, {'limits': (SERVER_LIMIT,)},
    {'facts': tuple(reversed(facts()))},
], ids=['time', 'client-value', 'client-removed', 'order'])
def test_fingerprint_ignores_time_client_policy_and_order(changes):
    assert capability_fingerprint(snapshot(**changes)) == capability_fingerprint(snapshot())


def test_fingerprint_is_stable_and_pinned():
    assert capability_fingerprint(snapshot()) == capability_fingerprint(snapshot())
    assert capability_fingerprint(snapshot()).startswith('capfp:v1:')
    components = fingerprint_components(snapshot())
    assert components['version'] == 1 and json.dumps(components)


def test_projections_are_allowlists():
    subject = snapshot(configuration_ref=REFERENCE)
    projected = snapshot_projection(subject)
    assert REFERENCE not in json.dumps(projected) and REFERENCE not in repr(subject)
    assert projected['capabilities']['batch'] == ['supported', 'adapter_code']
    assert projected['limits']['client_rate'] == ['client_policy', 'known', 2.5, 'requests_per_second', 'shared']
    assert set(projected) == {'schema_version', 'adapter', 'protocol', 'observed_at', 'capabilities', 'limits'}


def test_availability_projection_has_only_codes_and_counts():
    assert availability_projection(NOT_PROBED) == {
        'state': 'unknown', 'reason_code': 'not_probed', 'observed_at': None, 'expires_at': None, 'languages': 0}
    assert availability_projection(available())['languages'] == 1
    assert 'pt' not in json.dumps(availability_projection(available()))


def test_errors_never_quote_values():
    secret = 'https://user:hunter2@example.test'
    for build in (lambda: snapshot(configuration_ref=secret), lambda: LanguageEvidence(secret),
                  lambda: ProviderAvailability(AvailabilityState.UNKNOWN, secret),
                  lambda: ProviderLimit(secret, LimitUnit.REQUESTS, LimitScope.SHARED, LimitOrigin.SERVER, LimitSemantics.UNKNOWN, EvidenceSource.NONE)):
        with pytest.raises(ProviderCapabilityError) as caught:
            build()
        assert 'hunter2' not in str(caught.value) and 'hunter2' not in repr(caught.value)


def test_values_are_frozen():
    with pytest.raises(FrozenInstanceError):
        snapshot().observed_at = LATER
    with pytest.raises(FrozenInstanceError):
        available().state = AvailabilityState.UNKNOWN


def test_fingerprint_capabilities_are_exactly_the_documented_ones():
    assert {item.value for item in FINGERPRINT_CAPABILITIES} == {
        'plain_text_input', 'plain_text_output', 'context', 'constraints', 'protected_parts', 'structured_output',
        'native_glossary', 'multimodal_input', 'model_identity', 'network_class', 'authentication', 'retention_claim',
        'training_claim',
    }
