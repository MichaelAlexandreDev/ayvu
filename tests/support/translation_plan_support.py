from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
import pytest
from ayvu.provenance.model import ProducerIdentity
from ayvu.knowledge.cache.identity_values import (
    CacheEligibilityState, CacheIneligibleReason, CanonicalValue, OpaqueSemanticRevision, SemanticRouteHop,
)
from ayvu.providers.ports import ProviderRoute
from ayvu.providers.capabilities import (
    Capability, CapabilityFact, EvidenceSource, ProviderCapabilitySnapshot, ProviderDescriptor, ProviderRequirements, SupportState,
    capability_fingerprint,
)
from ayvu.translation.plan import (
    LINGUISTIC_STAGES, ResolvedTranslationPlan, TextStage, TranslationPlanError, resolve_plan,
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)


PRODUCER = ProducerIdentity('engine@1', 'model@1', 'adapter@1', 'not_applicable')


ROUTE = ProviderRoute('en', 'pt')


GLOSSARY = 'glossary:' + 'a' * 64


OTHER = 'glossary:' + 'b' * 64


EVIDENCE = {SupportState.SUPPORTED: EvidenceSource.ADAPTER_CODE, SupportState.UNSUPPORTED: EvidenceSource.ADAPTER_CODE,
            SupportState.UNKNOWN: EvidenceSource.NONE, SupportState.CONFIGURATION_DEPENDENT: EvidenceSource.CONFIGURATION}


def snapshot(**states):
    facts = tuple(CapabilityFact(item, state, EVIDENCE[state])
                  for item in Capability for state in [states.get(item.value, SupportState.SUPPORTED)])
    return ProviderCapabilitySnapshot(ProviderDescriptor('engine', 1, 'proto', 1, (1,), (1,)), facts, (), NOW)


def plan(**changes):
    values = {'producer': PRODUCER, 'route': ROUTE, 'capabilities': capability_fingerprint(snapshot()), 'chunk_limit': 3000,
              'glossary_ref': GLOSSARY, **changes}
    return ResolvedTranslationPlan(**values)


def bump(stage_id):
    return tuple(replace(item, version=2) if item.stage_id == stage_id else item for item in LINGUISTIC_STAGES)
