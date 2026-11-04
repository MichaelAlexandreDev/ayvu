"""What a provider is, what its configuration can do, and whether it answers right now.

Those facts have different lifetimes, so they are separate values: a static descriptor,
a snapshot for one configuration, and expiring availability. Runtime unavailability is
never a capability. A snapshot must state every capability, so absence cannot imply
support, and consumers decide only from these values, never from a name or a URL.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .job import JobError, is_slug, validate_utc
from .provider import valid_reference

CAPABILITY_SCHEMA_VERSION = 1
FINGERPRINT_VERSION = 1
MAX_LANGUAGES = 512
MAX_LANGUAGE_TARGETS = 512
MAX_LIMITS = 32
_LOCALE = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*")


class ProviderCapabilityError(ValueError):
    """A bounded error: messages never include caller data."""


class Capability(StrEnum):
    PLAIN_TEXT_INPUT = "plain_text_input"
    PLAIN_TEXT_OUTPUT = "plain_text_output"
    LANGUAGE_DISCOVERY = "language_discovery"
    ROUTE_DISCOVERY = "route_discovery"
    CONTEXT = "context"
    CONSTRAINTS = "constraints"
    PROTECTED_PARTS = "protected_parts"
    DEADLINE = "deadline"
    BATCH = "batch"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    NATIVE_GLOSSARY = "native_glossary"
    MULTIMODAL_INPUT = "multimodal_input"
    PRE_REQUEST_CANCELLATION = "pre_request_cancellation"
    IN_FLIGHT_CANCELLATION = "in_flight_cancellation"
    SHARED_INSTANCE_CONCURRENCY = "shared_instance_concurrency"
    MODEL_IDENTITY = "model_identity"
    NETWORK_CLASS = "network_class"
    AUTHENTICATION = "authentication"
    RETENTION_CLAIM = "retention_claim"
    TRAINING_CLAIM = "training_claim"
    COST_REPORTING = "cost_reporting"
    RESOURCE_REPORTING = "resource_reporting"


# The optional request features of the provider contract, by the capability that honors them.
FEATURE_CAPABILITIES = {
    "context": Capability.CONTEXT,
    "constraints": Capability.CONSTRAINTS,
    "protected_parts": Capability.PROTECTED_PARTS,
    "deadline": Capability.DEADLINE,
}

# What can change a translation or what it exposes. Operational facts stay out, so a
# later cache identity would not change when, for example, a rate limit does.
FINGERPRINT_CAPABILITIES = frozenset({
    Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT, Capability.CONTEXT, Capability.CONSTRAINTS,
    Capability.PROTECTED_PARTS, Capability.STRUCTURED_OUTPUT, Capability.NATIVE_GLOSSARY, Capability.MULTIMODAL_INPUT,
    Capability.MODEL_IDENTITY, Capability.NETWORK_CLASS, Capability.AUTHENTICATION, Capability.RETENTION_CLAIM,
    Capability.TRAINING_CLAIM,
})


class SupportState(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    CONFIGURATION_DEPENDENT = "configuration_dependent"


class EvidenceSource(StrEnum):
    ADAPTER_CODE = "adapter_code"
    RUNTIME_OBSERVATION = "runtime_observation"
    PROVIDER_DOCUMENTATION = "provider_documentation"
    CONFIGURATION = "configuration"
    NONE = "none"


@dataclass(frozen=True)
class CapabilityFact:
    capability: Capability
    state: SupportState
    evidence: EvidenceSource

    def __post_init__(self) -> None:
        if (type(self.capability) is not Capability or type(self.state) is not SupportState
                or type(self.evidence) is not EvidenceSource):
            raise ProviderCapabilityError("invalid_capability_fact")
        if self.state is SupportState.UNKNOWN:
            consistent = self.evidence is EvidenceSource.NONE
        elif self.state is SupportState.CONFIGURATION_DEPENDENT:
            consistent = self.evidence is EvidenceSource.CONFIGURATION
        else:
            consistent = self.evidence is not EvidenceSource.NONE
        if not consistent:
            raise ProviderCapabilityError("contradictory_capability_claim")


class LimitOrigin(StrEnum):
    """A client setting is Ayvu's own policy and never a limit discovered on a server."""

    SERVER = "server"
    CLIENT_POLICY = "client_policy"


class LimitSemantics(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    UNBOUNDED = "unbounded"


class LimitUnit(StrEnum):
    CHARACTERS = "characters"
    REQUESTS = "requests"
    REQUESTS_PER_SECOND = "requests_per_second"
    MILLISECONDS = "milliseconds"
    ATTEMPTS = "attempts"


class LimitScope(StrEnum):
    REQUEST = "request"
    INSTANCE = "instance"
    SHARED = "shared"


@dataclass(frozen=True)
class ProviderLimit:
    name: str
    unit: LimitUnit
    scope: LimitScope
    origin: LimitOrigin
    semantics: LimitSemantics
    evidence: EvidenceSource
    value: int | float | None = None

    def __post_init__(self) -> None:
        if (not is_slug(self.name) or type(self.unit) is not LimitUnit or type(self.scope) is not LimitScope
                or type(self.origin) is not LimitOrigin or type(self.semantics) is not LimitSemantics
                or type(self.evidence) is not EvidenceSource):
            raise ProviderCapabilityError("invalid_limit")
        if self.semantics is LimitSemantics.KNOWN:
            if type(self.value) not in (int, float) or not math.isfinite(self.value) or self.value < 0:
                raise ProviderCapabilityError("invalid_limit")
        elif self.value is not None:
            raise ProviderCapabilityError("invalid_limit")
        if self.semantics is LimitSemantics.UNKNOWN:
            consistent = self.evidence is EvidenceSource.NONE
        elif self.origin is LimitOrigin.CLIENT_POLICY:
            consistent = self.evidence is EvidenceSource.CONFIGURATION
        else:
            # Only observing or documenting a server can establish its limit.
            consistent = self.evidence in (EvidenceSource.RUNTIME_OBSERVATION, EvidenceSource.PROVIDER_DOCUMENTATION)
        if not consistent:
            raise ProviderCapabilityError("contradictory_limit_claim")


def _sorted_versions(value: object) -> tuple[int, ...]:
    if (type(value) is not tuple or not value or any(type(item) is not int or item < 1 for item in value)
            or len(set(value)) != len(value)):
        raise ProviderCapabilityError("invalid_descriptor")
    return tuple(sorted(value))


@dataclass(frozen=True)
class ProviderDescriptor:
    """Static identity and compatibility of an adapter. Nothing observed at runtime changes it."""

    adapter_id: str
    adapter_version: int
    protocol_id: str
    protocol_version: int
    request_schema_versions: tuple[int, ...]
    config_versions: tuple[int, ...]

    def __post_init__(self) -> None:
        if (not is_slug(self.adapter_id) or not is_slug(self.protocol_id)
                or type(self.adapter_version) is not int or self.adapter_version < 1
                or type(self.protocol_version) is not int or self.protocol_version < 1):
            raise ProviderCapabilityError("invalid_descriptor")
        object.__setattr__(self, "request_schema_versions", _sorted_versions(self.request_schema_versions))
        object.__setattr__(self, "config_versions", _sorted_versions(self.config_versions))


def _validate_time(value: object, code: str) -> None:
    try:
        validate_utc(value)
    except JobError:
        raise ProviderCapabilityError(code) from None


@dataclass(frozen=True, repr=False)
class ProviderCapabilitySnapshot:
    """What one resolved configuration can do. It must state every capability exactly once."""

    descriptor: ProviderDescriptor
    facts: tuple[CapabilityFact, ...]
    limits: tuple[ProviderLimit, ...]
    observed_at: datetime
    configuration_ref: str | None = None
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ProviderCapabilityError("unsupported_capability_schema")
        if type(self.descriptor) is not ProviderDescriptor:
            raise ProviderCapabilityError("invalid_descriptor")
        if type(self.facts) is not tuple or any(type(item) is not CapabilityFact for item in self.facts):
            raise ProviderCapabilityError("invalid_capability_fact")
        names = [item.capability for item in self.facts]
        if len(names) != len(set(names)) or set(names) != set(Capability):
            raise ProviderCapabilityError("incomplete_snapshot")
        if (type(self.limits) is not tuple or len(self.limits) > MAX_LIMITS
                or any(type(item) is not ProviderLimit for item in self.limits)
                or len({item.name for item in self.limits}) != len(self.limits)):
            raise ProviderCapabilityError("invalid_limit")
        _validate_time(self.observed_at, "invalid_observation_time")
        if not valid_reference(self.configuration_ref):
            raise ProviderCapabilityError("invalid_reference")
        object.__setattr__(self, "facts", tuple(sorted(self.facts, key=lambda item: item.capability.value)))
        object.__setattr__(self, "limits", tuple(sorted(self.limits, key=lambda item: item.name)))

    def __repr__(self) -> str:
        return f"ProviderCapabilitySnapshot(adapter_id={self.descriptor.adapter_id!r})"

    def state_of(self, capability: Capability) -> SupportState:
        return next(item.state for item in self.facts if item.capability is capability)


class AvailabilityState(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


def _valid_locale(value: object) -> bool:
    return isinstance(value, str) and _LOCALE.fullmatch(value) is not None


@dataclass(frozen=True)
class LanguageEvidence:
    """Codes only: names and other server text are not kept."""

    code: str
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not _valid_locale(self.code) or type(self.targets) is not tuple
                or len(self.targets) > MAX_LANGUAGE_TARGETS or not all(_valid_locale(item) for item in self.targets)):
            raise ProviderCapabilityError("invalid_language_evidence")


@dataclass(frozen=True)
class ProviderAvailability:
    state: AvailabilityState
    reason_code: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    languages: tuple[LanguageEvidence, ...] = ()

    def __post_init__(self) -> None:
        if type(self.state) is not AvailabilityState:
            raise ProviderCapabilityError("invalid_availability")
        if (self.state is AvailabilityState.AVAILABLE) != (self.reason_code is None):
            raise ProviderCapabilityError("invalid_availability")
        if self.reason_code is not None and not is_slug(self.reason_code):
            raise ProviderCapabilityError("invalid_availability")
        if (self.observed_at is None) != (self.expires_at is None):
            raise ProviderCapabilityError("invalid_availability")
        if self.observed_at is not None:
            _validate_time(self.observed_at, "invalid_availability")
            _validate_time(self.expires_at, "invalid_availability")
            if self.expires_at <= self.observed_at:
                raise ProviderCapabilityError("invalid_availability")
        elif self.state is not AvailabilityState.UNKNOWN:
            raise ProviderCapabilityError("invalid_availability")
        if (type(self.languages) is not tuple or len(self.languages) > MAX_LANGUAGES
                or any(type(item) is not LanguageEvidence for item in self.languages)
                or len({item.code for item in self.languages}) != len(self.languages)):
            raise ProviderCapabilityError("invalid_language_evidence")
        if (self.state is AvailabilityState.AVAILABLE) != bool(self.languages):
            raise ProviderCapabilityError("invalid_availability")

    def current(self, now: datetime) -> ProviderAvailability:
        """Expired evidence says nothing: neither available nor unavailable."""
        _validate_time(now, "invalid_availability")
        if self.expires_at is not None and now >= self.expires_at:
            return ProviderAvailability(AvailabilityState.UNKNOWN, "evidence_expired", self.observed_at, self.expires_at)
        return self


NOT_PROBED = ProviderAvailability(AvailabilityState.UNKNOWN, "not_probed")


def observation(state: AvailabilityState, reason_code: str | None, now: datetime, ttl: timedelta,
                languages: tuple[LanguageEvidence, ...] = ()) -> ProviderAvailability:
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ProviderCapabilityError("invalid_availability")
    _validate_time(now, "invalid_availability")
    return ProviderAvailability(state, reason_code, now, now + ttl, languages)


@dataclass(frozen=True)
class ProviderRequirements:
    required: tuple[Capability, ...]

    def __post_init__(self) -> None:
        if (type(self.required) is not tuple or any(type(item) is not Capability for item in self.required)
                or len(set(self.required)) != len(self.required)):
            raise ProviderCapabilityError("invalid_requirements")
        object.__setattr__(self, "required", tuple(sorted(self.required, key=lambda item: item.value)))


class NegotiationDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    PREFLIGHT_REQUIRED = "preflight_required"


@dataclass(frozen=True)
class NegotiationResult:
    decision: NegotiationDecision
    unsupported: tuple[Capability, ...] = ()
    unresolved: tuple[Capability, ...] = ()
    availability_reason: str | None = None


def negotiate(snapshot: ProviderCapabilitySnapshot, requirements: ProviderRequirements,
              availability: ProviderAvailability | None = None, *, now: datetime | None = None) -> NegotiationResult:
    """Pure, so it performs no I/O. Only a capability rejects; a state that can change asks for a preflight.

    Without ``availability`` the decision covers capabilities alone and says nothing about reachability.
    """
    if type(snapshot) is not ProviderCapabilitySnapshot or type(requirements) is not ProviderRequirements:
        raise ProviderCapabilityError("invalid_negotiation")
    if availability is not None and (type(availability) is not ProviderAvailability or now is None):
        raise ProviderCapabilityError("invalid_negotiation")
    states = {item: snapshot.state_of(item) for item in requirements.required}
    unsupported = tuple(item for item, state in states.items() if state is SupportState.UNSUPPORTED)
    unresolved = tuple(item for item, state in states.items()
                       if state in (SupportState.UNKNOWN, SupportState.CONFIGURATION_DEPENDENT))
    reason = None
    if availability is not None:
        effective = availability.current(now)
        if effective.state is not AvailabilityState.AVAILABLE:
            reason = effective.reason_code
    if unsupported:
        decision = NegotiationDecision.REJECT
    elif unresolved or reason is not None:
        decision = NegotiationDecision.PREFLIGHT_REQUIRED
    else:
        decision = NegotiationDecision.ACCEPT
    return NegotiationResult(decision, unsupported, unresolved, reason)


def fingerprint_components(snapshot: ProviderCapabilitySnapshot) -> dict:
    """Deterministic and free of time, availability and client policy. Nothing uses it yet; the cache is unchanged."""
    descriptor = snapshot.descriptor
    return {
        "version": FINGERPRINT_VERSION,
        "adapter": [descriptor.adapter_id, descriptor.adapter_version],
        "protocol": [descriptor.protocol_id, descriptor.protocol_version],
        "configuration_ref": snapshot.configuration_ref,
        "capabilities": [[item.capability.value, item.state.value] for item in snapshot.facts
                         if item.capability in FINGERPRINT_CAPABILITIES],
        "server_limits": [[item.name, item.unit.value, item.semantics.value, item.value] for item in snapshot.limits
                          if item.origin is LimitOrigin.SERVER],
    }


def capability_fingerprint(snapshot: ProviderCapabilitySnapshot) -> str:
    text = json.dumps(fingerprint_components(snapshot), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"capfp:v{FINGERPRINT_VERSION}:{hashlib.sha256(text.encode('ascii')).hexdigest()}"


def snapshot_projection(snapshot: ProviderCapabilitySnapshot) -> dict:
    """An allowlist: no configuration reference, endpoint, credential or provider payload."""
    descriptor = snapshot.descriptor
    return {
        "schema_version": snapshot.schema_version,
        "adapter": [descriptor.adapter_id, descriptor.adapter_version],
        "protocol": [descriptor.protocol_id, descriptor.protocol_version],
        "observed_at": snapshot.observed_at.isoformat(timespec="microseconds"),
        "capabilities": {item.capability.value: [item.state.value, item.evidence.value] for item in snapshot.facts},
        "limits": {item.name: [item.origin.value, item.semantics.value, item.value, item.unit.value, item.scope.value]
                   for item in snapshot.limits},
    }


def availability_projection(availability: ProviderAvailability) -> dict:
    """An allowlist: codes and counts only."""
    def stamp(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat(timespec="microseconds")

    return {
        "state": availability.state.value,
        "reason_code": availability.reason_code,
        "observed_at": stamp(availability.observed_at),
        "expires_at": stamp(availability.expires_at),
        "languages": len(availability.languages),
    }
