"""Honest capabilities: the core states its own facts and the adapter must state every transport fact."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ayvu.providers.capabilities import (
    Capability, CapabilityFact, EvidenceSource, NegotiationDecision, ProviderRequirements, SupportState, negotiate,
)
from ayvu.providers.llm import CORE_FACTS, TRANSPORT_CAPABILITIES, LlmCoreError, llm_snapshot
from ayvu.providers.llm.facts import CORE_FEATURES
from tests.support.llm_fakes import FAKE_DESCRIPTOR, FAKE_TRANSPORT_FACTS

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def test_the_core_claims_what_its_code_does_and_leaves_transport_to_the_adapter():
    supported = {fact.capability for fact in CORE_FACTS if fact.state is SupportState.SUPPORTED}
    assert supported == {Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT, Capability.STRUCTURED_OUTPUT,
                         Capability.BATCH, Capability.CONTEXT, Capability.NATIVE_GLOSSARY,
                         Capability.PRE_REQUEST_CANCELLATION}
    assert {Capability.MODEL_IDENTITY, Capability.NETWORK_CLASS, Capability.RETENTION_CLAIM} <= TRANSPORT_CAPABILITIES
    assert CORE_FEATURES == frozenset({"context"})


def test_a_snapshot_with_the_adapter_facts_is_complete_and_negotiates_structured_output():
    snapshot = llm_snapshot(FAKE_DESCRIPTOR, FAKE_TRANSPORT_FACTS, (), NOW)
    assert snapshot.state_of(Capability.MODEL_IDENTITY) is SupportState.SUPPORTED
    requirements = ProviderRequirements((Capability.STRUCTURED_OUTPUT, Capability.BATCH))
    assert negotiate(snapshot, requirements).decision is NegotiationDecision.ACCEPT


@pytest.mark.parametrize("facts", [
    FAKE_TRANSPORT_FACTS[1:],
    FAKE_TRANSPORT_FACTS + (CapabilityFact(Capability.BATCH, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),),
    FAKE_TRANSPORT_FACTS + FAKE_TRANSPORT_FACTS[:1],
    list(FAKE_TRANSPORT_FACTS),
], ids=["missing", "overriding_the_core", "duplicate", "not_a_tuple"])
def test_an_adapter_must_state_exactly_the_transport_capabilities(facts):
    with pytest.raises(LlmCoreError, match="invalid_transport_facts"):
        llm_snapshot(FAKE_DESCRIPTOR, facts, (), NOW)
