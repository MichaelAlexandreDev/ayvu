from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import math

import pytest

from ayvu.providers.capabilities import FEATURE_CAPABILITIES, MAX_LIMITS, AvailabilityState, Capability, CapabilityFact, EvidenceSource, LimitOrigin, LimitScope, LimitSemantics, LimitUnit, NegotiationDecision, ProviderCapabilityError, ProviderLimit, SupportState
from ayvu.providers.ports import ContextFeature, ConstraintsFeature, DeadlineFeature, ProtectedPartsFeature

from tests.providers.provider_capabilities_support import CLIENT_LIMIT, DESCRIPTOR
from tests.providers.provider_capabilities_support import SERVER_LIMIT, facts
from tests.providers.provider_capabilities_support import snapshot


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
