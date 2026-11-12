from dataclasses import FrozenInstanceError, replace
import json

import pytest

from ayvu.providers.capabilities import FINGERPRINT_CAPABILITIES, NOT_PROBED, AvailabilityState, Capability, EvidenceSource, LanguageEvidence, LimitOrigin, LimitScope, LimitSemantics, LimitUnit, ProviderAvailability, ProviderCapabilityError, ProviderLimit, SupportState, availability_projection, capability_fingerprint, fingerprint_components, snapshot_projection

from tests.providers.provider_capabilities_support import CLIENT_LIMIT, DESCRIPTOR, LATER, REFERENCE
from tests.providers.provider_capabilities_support import SERVER_LIMIT, available, facts
from tests.providers.provider_capabilities_support import snapshot


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
