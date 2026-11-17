"""What the core itself can claim, and the capabilities each model adapter must state for its transport.

The core states only what its own code does. Model identity, network class, retention and the other
facts of a vendor or a server are the adapter's to declare, and a snapshot is refused unless the
adapter states every one of them, so nothing is claimed by omission.
"""
from __future__ import annotations

from datetime import datetime

from ..capabilities import (
    FEATURE_CAPABILITIES, Capability, CapabilityFact, EvidenceSource, ProviderCapabilitySnapshot, ProviderDescriptor,
    ProviderLimit, SupportState,
)
from .model import LlmCoreError

CORE_FACTS = tuple(
    [CapabilityFact(item, SupportState.SUPPORTED, EvidenceSource.ADAPTER_CODE) for item in (
        Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT,
        # The core asks for a JSON schema and rejects any answer outside it.
        Capability.STRUCTURED_OUTPUT,
        Capability.BATCH,
        # Neighbouring text is rendered into the prompt, never translated or returned.
        Capability.CONTEXT,
        # The glossary is a hint in the prompt; the pipeline still applies it after translation.
        Capability.NATIVE_GLOSSARY,
        Capability.PRE_REQUEST_CANCELLATION)]
    + [CapabilityFact(item, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE) for item in (
        # Placeholders are checked strictly on every result, but protected spans are not sent as a feature.
        Capability.CONSTRAINTS, Capability.PROTECTED_PARTS, Capability.DEADLINE, Capability.STREAMING,
        Capability.MULTIMODAL_INPUT)]
)
TRANSPORT_CAPABILITIES = frozenset(Capability) - {fact.capability for fact in CORE_FACTS}

# Derived from the facts, so what the core claims and what it honours cannot drift apart.
CORE_FEATURES = frozenset(
    name for name, capability in FEATURE_CAPABILITIES.items()
    if any(fact.capability is capability and fact.state is SupportState.SUPPORTED for fact in CORE_FACTS))


def llm_snapshot(descriptor: ProviderDescriptor, transport_facts: tuple[CapabilityFact, ...],
                 limits: tuple[ProviderLimit, ...], observed_at: datetime, *,
                 configuration_ref: str | None = None) -> ProviderCapabilitySnapshot:
    """The core's facts plus the adapter's. The adapter must state exactly the transport capabilities."""
    if (type(transport_facts) is not tuple or any(type(item) is not CapabilityFact for item in transport_facts)
            or len({item.capability for item in transport_facts}) != len(transport_facts)
            or {item.capability for item in transport_facts} != TRANSPORT_CAPABILITIES):
        raise LlmCoreError("invalid_transport_facts")
    return ProviderCapabilitySnapshot(descriptor, CORE_FACTS + transport_facts, limits, observed_at, configuration_ref)
