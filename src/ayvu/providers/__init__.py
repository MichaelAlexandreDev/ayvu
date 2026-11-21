"""Translation providers: the request and result port, capabilities, discovery, the provider registry, network decisions,
what a remote adapter declares and what usage costs. LibreTranslate and the CLI provider session are adapter modules."""
from __future__ import annotations

from .capabilities import (
    Capability, LimitSemantics, LimitUnit, NegotiationDecision, ProviderCapabilitySnapshot,
    ProviderRequirements, SupportState, capability_fingerprint, negotiate,
)
from .discovery import DiscoveredLanguage, DiscoveryOutcome, LanguagesResult, SYNTHETIC_TEST_TEXT
from .errors import ProviderSetupError
from .network import NetworkClass, ResolvedProviderNetworkDecision, network_refusal
from .ports import (
    ContextFeature, ContextItem, DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, Requirement,
    TranslationProviderRequest, UnitReference, valid_reference,
)
from .prices import (
    PRICE_TABLE_VERSION, ModelPrice, PriceSource, ResolvedPrice, UsageEstimate, UsageRecord, UsageTotals,
    cost_micro_usd, micro_usd, price_overrides, resolve_price,
)
from .remote import RemoteCredential, RemoteModelAdapter, check_remote_adapter, credential_from_environment
from .service import TranslationProvider, execute

__all__ = [
    "PRICE_TABLE_VERSION", "ModelPrice", "PriceSource", "ResolvedPrice", "UsageEstimate", "UsageRecord", "UsageTotals",
    "cost_micro_usd", "micro_usd", "price_overrides", "resolve_price",
    "Capability", "ContextFeature", "DeliveryState", "ProviderUsage", "ContextItem", "DiscoveredLanguage", "DiscoveryOutcome",
    "LanguagesResult", "LimitSemantics", "LimitUnit", "NegotiationDecision", "NetworkClass",
    "ProviderCapabilitySnapshot", "ProviderRequirements", "ProviderRoute", "ProviderSetupError",
    "ProviderStatus", "RemoteCredential", "RemoteModelAdapter", "Requirement", "ResolvedProviderNetworkDecision", "SYNTHETIC_TEST_TEXT", "SupportState",
    "TranslationProvider", "TranslationProviderRequest", "UnitReference", "capability_fingerprint", "check_remote_adapter",
    "credential_from_environment", "execute",
    "negotiate", "network_refusal", "valid_reference",
]
