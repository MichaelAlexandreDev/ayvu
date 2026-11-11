"""Translation providers: the request and result port, capabilities, discovery, the provider registry and network decisions. LibreTranslate and the CLI provider session are adapter modules."""
from __future__ import annotations

from .capabilities import (
    Capability, LimitSemantics, LimitUnit, NegotiationDecision, ProviderCapabilitySnapshot,
    ProviderRequirements, SupportState, capability_fingerprint, negotiate,
)
from .discovery import DiscoveredLanguage, DiscoveryOutcome, LanguagesResult, SYNTHETIC_TEST_TEXT
from .errors import ProviderSetupError
from .network import NetworkClass, ResolvedProviderNetworkDecision, network_refusal
from .ports import (
    ContextFeature, ContextItem, ProviderRoute, ProviderStatus, Requirement, TranslationProviderRequest,
    UnitReference, valid_reference,
)
from .service import TranslationProvider, execute

__all__ = [
    "Capability", "ContextFeature", "ContextItem", "DiscoveredLanguage", "DiscoveryOutcome",
    "LanguagesResult", "LimitSemantics", "LimitUnit", "NegotiationDecision", "NetworkClass",
    "ProviderCapabilitySnapshot", "ProviderRequirements", "ProviderRoute", "ProviderSetupError",
    "ProviderStatus", "Requirement", "ResolvedProviderNetworkDecision", "SYNTHETIC_TEST_TEXT", "SupportState",
    "TranslationProvider", "TranslationProviderRequest", "UnitReference", "capability_fingerprint", "execute",
    "negotiate", "network_refusal", "valid_reference",
]
