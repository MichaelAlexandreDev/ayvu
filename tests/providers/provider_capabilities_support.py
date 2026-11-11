from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import math
import pytest
from ayvu.providers.capabilities import (
    FEATURE_CAPABILITIES, FINGERPRINT_CAPABILITIES, MAX_LANGUAGE_TARGETS, MAX_LANGUAGES, MAX_LIMITS, NOT_PROBED,
    AvailabilityState, Capability, CapabilityFact, EvidenceSource, LanguageEvidence, LimitOrigin, LimitScope,
    LimitSemantics, LimitUnit, NegotiationDecision, ProviderAvailability, ProviderCapabilityError,
    ProviderCapabilitySnapshot, ProviderDescriptor, ProviderLimit, ProviderRequirements, SupportState,
    availability_projection, capability_fingerprint, fingerprint_components, negotiate, observation, snapshot_projection,
)
from ayvu.providers.ports import ContextFeature, ConstraintsFeature, DeadlineFeature, ProtectedPartsFeature
from ayvu.providers.service import ProviderAvailabilityProbe, observe_availability

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
