"""Shared use cases for finding out about providers: what exists, whether it answers, and how to route.

Interfaces call these instead of reaching a provider directly. No code here names a provider or
reads a URL. Static identity, what a configuration can do, runtime availability and policy
eligibility stay separate, and every operation that uses the network presents a decision first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Protocol
from uuid import uuid4

from .job import JobError, validate_utc
from .job_events import JobFailure, JobFailureFamily
from .job_execution import CancellationToken
from .provenance import ProducerIdentity
from .provider import ProviderError, ProviderRoute, ProviderStatus, TranslationProviderRequest, TranslationProviderResult
from .provider_capabilities import (
    NOT_PROBED, AvailabilityState, NegotiationDecision, ProviderAvailability, ProviderCapabilitySnapshot,
    ProviderDescriptor, ProviderRequirements, negotiate,
)
from .provider_network import ProviderOperation, network_binding, network_refusal
from .network_policy import NETWORK_DENIAL_CODES
from .provider_service import ProviderInstance, TranslationProvider, UNKNOWN_PRODUCER, execute, failed_result

# Sent only by an explicit user request to test a provider, and never as a hidden probe.
SYNTHETIC_TEST_TEXT = "Hello world"
DISCOVERY_SAFE_POINT = "discovery_before_request"
DEFAULT_ROUTE_CANDIDATES = ("en",)
MAX_LANGUAGE_NAME_CHARS = 256


class ProviderDiscoveryError(ValueError):
    """A bounded error: messages never include caller data."""


class DiscoveryOutcome(StrEnum):
    OK = "ok"
    UNKNOWN_INSTANCE = "unknown_instance"
    POLICY_DENIED = "policy_denied"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    NO_ROUTE = "no_route"
    UNSUPPORTED = "unsupported"
    INDETERMINATE = "indeterminate"
    CANCELLED = "cancelled"
    DEADLINE_EXPIRED = "deadline_expired"
    FAILED = "failed"


class Freshness(StrEnum):
    FRESH = "fresh"
    CACHED = "cached"
    UNKNOWN = "unknown"


class PolicyEligibility(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    ELIGIBLE = "eligible"
    DENIED = "denied"


@dataclass(frozen=True, repr=False)
class DiscoveredLanguage:
    """For display only. The name is server text: it stays out of evidence and diagnostics."""

    code: str
    name: str
    targets: tuple[str, ...] = ()
    state: str = "installed"

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not self.name or len(self.name) > MAX_LANGUAGE_NAME_CHARS
                or any(ord(character) < 32 or ord(character) == 127 for character in self.name)):
            raise ProviderDiscoveryError("invalid_language_name")

    def __repr__(self) -> str:
        return f"DiscoveredLanguage(code={self.code!r})"


@dataclass(frozen=True)
class DiscoveryObservation:
    availability: ProviderAvailability
    languages: tuple[DiscoveredLanguage, ...] = ()


class ProviderDiscoveryAdapter(Protocol):
    """Implemented by each provider adapter. Discovery must not send document text."""

    @property
    def descriptor(self) -> ProviderDescriptor: ...
    def snapshot(self) -> ProviderCapabilitySnapshot: ...
    def observe(self) -> DiscoveryObservation: ...
    def route(self, availability: ProviderAvailability, source: str, target: str, now: datetime,
              candidates: tuple[str, ...]) -> ProviderRoute | None: ...
    def provider(self) -> TranslationProvider: ...


@dataclass(frozen=True)
class ProviderSummary:
    instance_id: str
    adapter_id: str
    adapter_version: int


@dataclass(frozen=True)
class ProviderDescription:
    instance: ProviderInstance
    descriptor: ProviderDescriptor
    capabilities: ProviderCapabilitySnapshot
    availability: ProviderAvailability
    eligibility: PolicyEligibility
    eligibility_reason: str | None = None


@dataclass(frozen=True)
class AvailabilityResult:
    outcome: DiscoveryOutcome
    availability: ProviderAvailability
    freshness: Freshness
    code: str | None = None


@dataclass(frozen=True)
class LanguagesResult:
    outcome: DiscoveryOutcome
    languages: tuple[DiscoveredLanguage, ...]
    freshness: Freshness
    code: str | None = None


@dataclass(frozen=True)
class RouteResult:
    outcome: DiscoveryOutcome
    route: ProviderRoute | None
    freshness: Freshness
    code: str | None = None


@dataclass(frozen=True, repr=False)
class ProviderTestResult:
    outcome: DiscoveryOutcome
    text: str | None
    producer: ProducerIdentity
    route: ProviderRoute | None
    failure: JobFailure | None
    code: str | None = None

    def __repr__(self) -> str:
        return f"ProviderTestResult(outcome={self.outcome.value!r}, code={self.code!r})"


@dataclass(frozen=True)
class ProviderPreflightResult:
    outcome: DiscoveryOutcome
    route: ProviderRoute | None
    availability: ProviderAvailability
    negotiation: NegotiationDecision
    code: str | None = None


_AVAILABILITY_OUTCOMES = {
    AvailabilityState.AVAILABLE: DiscoveryOutcome.OK,
    AvailabilityState.DEGRADED: DiscoveryOutcome.DEGRADED,
    AvailabilityState.UNAVAILABLE: DiscoveryOutcome.UNAVAILABLE,
    AvailabilityState.UNKNOWN: DiscoveryOutcome.UNKNOWN,
}


class ProviderDiscoveryService:
    """Entries are registered explicitly at a composition root. Nothing is imported or scanned."""

    def __init__(self, *, trusted_issuers: frozenset[str], policy_revision: int, clock: Callable[[], datetime],
                 new_id: Callable[[], str] = lambda: str(uuid4())) -> None:
        self._entries: dict[str, tuple[ProviderInstance, ProviderDiscoveryAdapter]] = {}
        self._observed: dict[str, DiscoveryObservation] = {}
        self._trusted_issuers = trusted_issuers
        self._policy_revision = policy_revision
        self._clock = clock
        self._new_id = new_id

    def __repr__(self) -> str:
        return f"ProviderDiscoveryService(instances={len(self._entries)})"

    def register(self, instance: ProviderInstance, adapter: ProviderDiscoveryAdapter) -> None:
        if type(instance) is not ProviderInstance:
            raise ProviderDiscoveryError("invalid_instance")
        if instance.instance_id in self._entries:
            raise ProviderDiscoveryError("duplicate_instance")
        descriptor = adapter.descriptor
        if (descriptor.adapter_id, descriptor.adapter_version) != (instance.adapter_id, instance.adapter_version):
            raise ProviderDiscoveryError("incompatible_adapter")
        if instance.config_version not in descriptor.config_versions:
            raise ProviderDiscoveryError("incompatible_configuration")
        self._entries[instance.instance_id] = (instance, adapter)

    def list_providers(self) -> tuple[ProviderSummary, ...]:
        """No network: only what is configured."""
        return tuple(ProviderSummary(instance.instance_id, instance.adapter_id, instance.adapter_version)
                     for _, (instance, _adapter) in sorted(self._entries.items()))

    def _blocked(self, instance_id: str, operation: ProviderOperation, decision: object,
                 cancellation: CancellationToken | None, deadline: datetime | None) -> tuple[DiscoveryOutcome, str] | None:
        """Everything that must hold before a request can leave this process."""
        entry = self._entries.get(instance_id) if isinstance(instance_id, str) else None
        if entry is None:
            return DiscoveryOutcome.UNKNOWN_INSTANCE, "unknown_instance"
        now = self._clock()
        refusal = network_refusal(decision, binding=network_binding(instance_id, entry[0].configuration_ref, operation),
                                  trusted_issuers=self._trusted_issuers, policy_revision=self._policy_revision, now=now)
        if refusal is not None:
            return DiscoveryOutcome.POLICY_DENIED, refusal
        if deadline is not None:
            try:
                validate_utc(deadline)
            except JobError:
                raise ProviderDiscoveryError("invalid_deadline") from None
            if now >= deadline:
                return DiscoveryOutcome.DEADLINE_EXPIRED, "deadline_expired"
        if cancellation is not None and cancellation.checkpoint(DISCOVERY_SAFE_POINT) is not None:
            return DiscoveryOutcome.CANCELLED, "cancelled_before_request"
        return None

    def _observe(self, instance_id: str) -> DiscoveryObservation:
        adapter = self._entries[instance_id][1]
        try:
            observation = adapter.observe()
        except Exception:
            # A broken adapter proves nothing about the provider, and its message may quote a URL.
            observation = DiscoveryObservation(ProviderAvailability(AvailabilityState.UNKNOWN, "probe_failed"))
        if type(observation) is not DiscoveryObservation:
            observation = DiscoveryObservation(ProviderAvailability(AvailabilityState.UNKNOWN, "probe_failed"))
        self._observed[instance_id] = observation
        return observation

    def describe(self, instance_id: str, decision: object = None) -> ProviderDescription:
        """No network. Availability is only what was last observed, and stale evidence reads as unknown."""
        entry = self._entries.get(instance_id) if isinstance(instance_id, str) else None
        if entry is None:
            raise ProviderDiscoveryError("unknown_instance")
        instance, adapter = entry
        observation = self._observed.get(instance_id)
        availability = NOT_PROBED if observation is None else observation.availability.current(self._clock())
        if decision is None:
            eligibility, reason = PolicyEligibility.NOT_EVALUATED, None
        else:
            reason = network_refusal(decision, binding=network_binding(instance_id, instance.configuration_ref, ProviderOperation.PREFLIGHT),
                                     trusted_issuers=self._trusted_issuers, policy_revision=self._policy_revision, now=self._clock())
            eligibility = PolicyEligibility.ELIGIBLE if reason is None else PolicyEligibility.DENIED
        return ProviderDescription(instance, adapter.descriptor, adapter.snapshot(), availability, eligibility, reason)

    def availability(self, instance_id: str, decision: object, *, reuse_fresh: bool = False,
                     cancellation: CancellationToken | None = None, deadline: datetime | None = None) -> AvailabilityResult:
        blocked = self._blocked(instance_id, ProviderOperation.AVAILABILITY, decision, cancellation, deadline)
        if blocked is not None:
            return AvailabilityResult(blocked[0], NOT_PROBED, Freshness.UNKNOWN, blocked[1])
        if reuse_fresh and instance_id in self._observed:
            cached = self._observed[instance_id].availability.current(self._clock())
            if cached.state is not AvailabilityState.UNKNOWN:
                return AvailabilityResult(self._availability_outcome(cached), cached, Freshness.CACHED, cached.reason_code)
        observed = self._observe(instance_id).availability
        return AvailabilityResult(self._availability_outcome(observed), observed, Freshness.FRESH, observed.reason_code)

    @staticmethod
    def _availability_outcome(availability: ProviderAvailability) -> DiscoveryOutcome:
        if availability.reason_code in NETWORK_DENIAL_CODES:
            return DiscoveryOutcome.POLICY_DENIED
        return _AVAILABILITY_OUTCOMES[availability.state]

    def languages(self, instance_id: str, decision: object, *, cancellation: CancellationToken | None = None,
                  deadline: datetime | None = None) -> LanguagesResult:
        blocked = self._blocked(instance_id, ProviderOperation.LANGUAGES, decision, cancellation, deadline)
        if blocked is not None:
            return LanguagesResult(blocked[0], (), Freshness.UNKNOWN, blocked[1])
        observation = self._observe(instance_id)
        return LanguagesResult(self._availability_outcome(observation.availability), observation.languages,
                               Freshness.FRESH, observation.availability.reason_code)

    def resolve_route(self, instance_id: str, decision: object, source: str, target: str, *,
                      candidates: tuple[str, ...] = DEFAULT_ROUTE_CANDIDATES, cancellation: CancellationToken | None = None,
                      deadline: datetime | None = None) -> RouteResult:
        blocked = self._blocked(instance_id, ProviderOperation.ROUTE, decision, cancellation, deadline)
        if blocked is not None:
            return RouteResult(blocked[0], None, Freshness.UNKNOWN, blocked[1])
        return self._route_from(instance_id, self._observe(instance_id).availability, source, target, candidates)

    def _route_from(self, instance_id: str, availability: ProviderAvailability, source: str, target: str,
                    candidates: tuple[str, ...]) -> RouteResult:
        if availability.state is not AvailabilityState.AVAILABLE:
            return RouteResult(self._availability_outcome(availability), None, Freshness.FRESH, availability.reason_code)
        try:
            route = self._entries[instance_id][1].route(availability, source, target, self._clock(), candidates)
        except Exception:
            return RouteResult(DiscoveryOutcome.UNKNOWN, None, Freshness.FRESH, "route_failed")
        if route is None:
            return RouteResult(DiscoveryOutcome.NO_ROUTE, None, Freshness.FRESH, "no_route")
        return RouteResult(DiscoveryOutcome.OK, route, Freshness.FRESH)

    def test_provider(self, instance_id: str, decision: object, source: str, target: str, *,
                      cancellation: CancellationToken | None = None, deadline: datetime | None = None) -> ProviderTestResult:
        """Sends only the known synthetic text, and only because a person asked for this test."""
        blocked = self._blocked(instance_id, ProviderOperation.TEST, decision, cancellation, deadline)
        if blocked is not None:
            return ProviderTestResult(blocked[0], None, UNKNOWN_PRODUCER, None, None, blocked[1])
        try:
            request = TranslationProviderRequest(self._new_id(), SYNTHETIC_TEST_TEXT, source, target)
        except ProviderError as error:
            return ProviderTestResult(DiscoveryOutcome.FAILED, None, UNKNOWN_PRODUCER, None, None, str(error))
        try:
            provider = self._entries[instance_id][1].provider()
        except Exception:
            result = failed_result(request, JobFailure(JobFailureFamily.INTERNAL, "provider_unavailable_to_create"))
        else:
            result = execute(provider, request)
        return self._test_result(result)

    @staticmethod
    def _test_result(result: TranslationProviderResult) -> ProviderTestResult:
        if result.status is ProviderStatus.SUCCEEDED:
            return ProviderTestResult(DiscoveryOutcome.OK, result.text, result.producer, result.route, None)
        code = result.failure.code
        outcome = DiscoveryOutcome.CANCELLED if result.status is ProviderStatus.CANCELLED else DiscoveryOutcome.FAILED
        if result.failure.family is JobFailureFamily.POLICY:
            outcome = DiscoveryOutcome.POLICY_DENIED
        return ProviderTestResult(outcome, None, result.producer, result.route, result.failure, code)

    def preflight(self, instance_id: str, decision: object, requirements: ProviderRequirements, source: str, target: str, *,
                  candidates: tuple[str, ...] = DEFAULT_ROUTE_CANDIDATES, cancellation: CancellationToken | None = None,
                  deadline: datetime | None = None) -> ProviderPreflightResult:
        """Uses the same evidence as discovery. There is no hidden translation request."""
        entry = self._entries.get(instance_id) if isinstance(instance_id, str) else None
        if entry is None:
            return ProviderPreflightResult(DiscoveryOutcome.UNKNOWN_INSTANCE, None, NOT_PROBED, NegotiationDecision.REJECT, "unknown_instance")
        negotiation = negotiate(entry[1].snapshot(), requirements)
        if negotiation.decision is NegotiationDecision.REJECT:
            # Decided from static facts, so an unsupported requirement costs no I/O.
            return ProviderPreflightResult(DiscoveryOutcome.UNSUPPORTED, None, NOT_PROBED, negotiation.decision, "unsupported_requirement")
        blocked = self._blocked(instance_id, ProviderOperation.PREFLIGHT, decision, cancellation, deadline)
        if blocked is not None:
            return ProviderPreflightResult(blocked[0], None, NOT_PROBED, negotiation.decision, blocked[1])
        availability = self._observe(instance_id).availability
        routed = self._route_from(instance_id, availability, source, target, candidates)
        if routed.outcome is not DiscoveryOutcome.OK:
            return ProviderPreflightResult(routed.outcome, None, availability, negotiation.decision, routed.code)
        if negotiation.decision is not NegotiationDecision.ACCEPT:
            # Unknown or configuration-dependent facts never read as support.
            return ProviderPreflightResult(DiscoveryOutcome.INDETERMINATE, routed.route, availability, negotiation.decision, "unresolved_requirement")
        return ProviderPreflightResult(DiscoveryOutcome.OK, routed.route, availability, negotiation.decision)
