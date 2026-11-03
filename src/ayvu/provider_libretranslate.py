"""Exposes the current LibreTranslate translator through the rich provider contract.

It wraps ``Translator`` and changes nothing in it: HTTP, routing, retries, backoff,
timeouts and rate limiting stay where they are. Errors are classified from typed
attributes and their messages are never read, because a message can quote the
response body or a URL holding credentials.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from .job_events import JobFailure, JobFailureFamily, normalize_failure
from .provenance import NOT_APPLICABLE, UNKNOWN, ProducerIdentity
from .provider import (
    DeadlineFeature, DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderRequest,
    TranslationProviderResult,
)
from .provider_capabilities import (
    FEATURE_CAPABILITIES, AvailabilityState, Capability, CapabilityFact, EvidenceSource, LanguageEvidence, LimitOrigin,
    LimitScope, LimitSemantics, LimitUnit, MAX_LANGUAGE_TARGETS, MAX_LANGUAGES, ProviderAvailability,
    ProviderCapabilityError, ProviderCapabilitySnapshot, ProviderDescriptor, ProviderLimit, SupportState, observation,
)
from .provider_service import FeatureDecision, ProviderInstance, decide_features, failed_result
from .translator import (
    LibreTranslateTranslator, RouteResolutionError, RoutedTranslator, TranslationRoute, Translator, TranslatorError,
    TranslatorLanguage, resolve_translation_route,
)

ADAPTER_ID = "libretranslate"
ADAPTER_VERSION = 1
SUPPORTED_CONFIG_VERSIONS = frozenset({1})
LIBRETRANSLATE_SAFE_POINTS = ("provider_before_request",)
_PRODUCER = ProducerIdentity("libretranslate", UNKNOWN, f"libretranslate_compat@{ADAPTER_VERSION}", NOT_APPLICABLE)
DEFAULT_AVAILABILITY_TTL = timedelta(seconds=60)

LIBRETRANSLATE_DESCRIPTOR = ProviderDescriptor(
    adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION, protocol_id="libretranslate_http", protocol_version=1,
    request_schema_versions=(1,), config_versions=tuple(sorted(SUPPORTED_CONFIG_VERSIONS)),
)


def _fact(capability: Capability, state: SupportState) -> CapabilityFact:
    evidence = {SupportState.UNKNOWN: EvidenceSource.NONE,
                SupportState.CONFIGURATION_DEPENDENT: EvidenceSource.CONFIGURATION}.get(state, EvidenceSource.ADAPTER_CODE)
    return CapabilityFact(capability, state, evidence)


# Facts about this adapter, stated from its code. The evidence is not the server, whose
# version can change: `/languages` is the only thing observed at runtime.
_FACTS = tuple(
    [_fact(item, SupportState.SUPPORTED) for item in (
        Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT, Capability.LANGUAGE_DISCOVERY,
        Capability.ROUTE_DISCOVERY, Capability.PRE_REQUEST_CANCELLATION)]
    + [_fact(item, SupportState.UNSUPPORTED) for item in (
        Capability.CONTEXT, Capability.CONSTRAINTS, Capability.PROTECTED_PARTS, Capability.DEADLINE, Capability.BATCH,
        Capability.STREAMING, Capability.STRUCTURED_OUTPUT, Capability.NATIVE_GLOSSARY, Capability.MULTIMODAL_INPUT,
        # A requests timeout is not cancellation.
        Capability.IN_FLIGHT_CANCELLATION,
        # Each task gets its own HTTP session; one instance is not shared across threads.
        Capability.SHARED_INSTANCE_CONCURRENCY)]
    + [_fact(item, SupportState.UNKNOWN) for item in (
        Capability.MODEL_IDENTITY, Capability.RETENTION_CLAIM, Capability.TRAINING_CLAIM, Capability.COST_REPORTING,
        Capability.RESOURCE_REPORTING)]
    # A loopback address proves nothing about privacy, retention or the absence of egress.
    + [_fact(item, SupportState.CONFIGURATION_DEPENDENT) for item in (Capability.NETWORK_CLASS, Capability.AUTHENTICATION)]
)

# Derived from the facts, so what the adapter claims and what it honors cannot drift apart.
_SUPPORTED_FEATURES = frozenset(
    name for name, capability in FEATURE_CAPABILITIES.items()
    if any(fact.capability is capability and fact.state is SupportState.SUPPORTED for fact in _FACTS))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _http_failure(status_code: int | None) -> JobFailure:
    if status_code in (401, 403):
        return JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_unauthorized")
    if status_code == 429:
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_rate_limited", True)
    if status_code is not None and status_code >= 500:
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_server_error", True)
    return JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_rejected")


def _classify(error: TranslatorError) -> tuple[JobFailure, DeliveryState]:
    if error.reason == "http":
        return _http_failure(error.status_code), DeliveryState.COMPLETED
    if error.reason == "malformed":
        return JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "malformed_response"), DeliveryState.COMPLETED
    if error.reason == "timeout":
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_timeout", True), DeliveryState.UNKNOWN
    if error.reason == "connection":
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_unavailable", True), DeliveryState.UNKNOWN
    if error.reason == "request":
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_request_failed", True), DeliveryState.UNKNOWN
    return JobFailure(JobFailureFamily.INTERNAL, "unexpected_error"), DeliveryState.UNKNOWN


class LibreTranslateProvider:
    """One translator, used as today. Not a capability claim: see ``_SUPPORTED_FEATURES``."""

    def __init__(self, translator: Translator, *, clock: Callable[[], datetime] = _utc_now,
                 cleanup: Callable[[], None] | None = None) -> None:
        self._translator = translator
        self._clock = clock
        self._cleanup = cleanup

    def __repr__(self) -> str:
        return "LibreTranslateProvider()"

    def close(self) -> None:
        if self._cleanup is not None:
            self._cleanup()

    def _effective_route(self, request: TranslationProviderRequest) -> ProviderRoute:
        """The route ``RoutedTranslator`` will really take for these locales."""
        source, target = request.source_locale, request.target_locale
        if isinstance(self._translator, RoutedTranslator):
            route = self._translator.route
            if not route.is_direct and (route.source, route.target) == (source, target):
                return ProviderRoute(source, target, route.intermediate)
        return ProviderRoute(source, target)

    def _refusal(self, request: TranslationProviderRequest, route: ProviderRoute,
                 decision: FeatureDecision) -> TranslationProviderResult | None:
        """Everything decided before any HTTP request is made."""
        def refuse(failure: JobFailure) -> TranslationProviderResult:
            return failed_result(request, failure, producer=_PRODUCER, delivery=DeliveryState.NOT_SENT,
                                 warnings=decision.ignored_advisory, route=route)

        if decision.unsupported_required:
            return refuse(JobFailure(JobFailureFamily.CAPABILITY, "unsupported_required_feature"))
        if request.route is not None and request.route != route:
            return refuse(JobFailure(JobFailureFamily.CAPABILITY, "route_conflict"))
        deadline = request.feature(DeadlineFeature)
        if deadline is not None and self._clock() >= deadline.at:
            return refuse(JobFailure(JobFailureFamily.RESOURCE, "deadline_expired"))
        if request.cancellation is not None and request.cancellation.checkpoint("provider_before_request") is not None:
            return refuse(JobFailure(JobFailureFamily.CANCELLED, "cancelled_before_request"))
        return None

    def execute(self, request: TranslationProviderRequest) -> TranslationProviderResult:
        route = self._effective_route(request)
        decision = decide_features(request, _SUPPORTED_FEATURES)
        warnings = decision.ignored_advisory
        refusal = self._refusal(request, route, decision)
        if refusal is not None:
            return refusal

        def failed(failure: JobFailure, delivery: DeliveryState) -> TranslationProviderResult:
            return failed_result(request, failure, producer=_PRODUCER, delivery=delivery, warnings=warnings, route=route)

        try:
            text = self._translator.translate(request.text, request.source_locale, request.target_locale)
        except TranslatorError as error:
            return failed(*_classify(error))
        except Exception as error:
            return failed(normalize_failure(error), DeliveryState.UNKNOWN)
        # The existing path accepts an empty translation; a rich result never does.
        if not isinstance(text, str) or (not text.strip() and request.text.strip()):
            return failed(JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "empty_result"), DeliveryState.COMPLETED)
        return TranslationProviderResult(
            request.request_id, ProviderStatus.SUCCEEDED, _PRODUCER, DeliveryState.COMPLETED, text=text, route=route,
            warnings=warnings, usage=ProviderUsage(characters_submitted=len(request.text)))


class LibreTranslateProviderFactory:
    """Each ``create`` invokes ``translator_factory`` once, so each parallel task keeps its own
    HTTP session, while a rate limiter given to that factory stays shared, as it is today."""

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION
    supported_config_versions = SUPPORTED_CONFIG_VERSIONS

    def __init__(self, translator_factory: Callable[[], Translator], *, route: TranslationRoute | None = None,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        self._translator_factory = translator_factory
        self._route = route
        self._clock = clock

    def __repr__(self) -> str:
        return "LibreTranslateProviderFactory()"

    def create(self, instance: ProviderInstance) -> LibreTranslateProvider:
        base = self._translator_factory()
        translator = RoutedTranslator(base, self._route) if self._route is not None else base
        cleanup = base.session.close if isinstance(base, LibreTranslateTranslator) else None
        return LibreTranslateProvider(translator, clock=self._clock, cleanup=cleanup)


def _unknown_server_limit(name: str, unit: LimitUnit, scope: LimitScope) -> ProviderLimit:
    return ProviderLimit(name, unit, scope, LimitOrigin.SERVER, LimitSemantics.UNKNOWN, EvidenceSource.NONE)


def _client_limit(name: str, unit: LimitUnit, scope: LimitScope, value: int | float | None) -> ProviderLimit:
    semantics = LimitSemantics.UNBOUNDED if value is None else LimitSemantics.KNOWN
    return ProviderLimit(name, unit, scope, LimitOrigin.CLIENT_POLICY, semantics, EvidenceSource.CONFIGURATION, value)


def libretranslate_snapshot(translator: LibreTranslateTranslator, observed_at: datetime, *,
                            configuration_ref: str | None = None) -> ProviderCapabilitySnapshot:
    """The URL is deliberately not read: no capability is inferred from an address."""
    return ProviderCapabilitySnapshot(
        LIBRETRANSLATE_DESCRIPTOR, _FACTS,
        (
            _unknown_server_limit("server_max_input_chars", LimitUnit.CHARACTERS, LimitScope.REQUEST),
            _unknown_server_limit("server_max_concurrent_requests", LimitUnit.REQUESTS, LimitScope.SHARED),
            _unknown_server_limit("server_request_quota", LimitUnit.REQUESTS, LimitScope.SHARED),
            _client_limit("client_request_timeout", LimitUnit.MILLISECONDS, LimitScope.REQUEST, translator.timeout * 1000),
            _client_limit("client_retries", LimitUnit.ATTEMPTS, LimitScope.REQUEST, translator.retries),
            # The limiter's own rate: it is what really applies when a limiter is shared.
            _client_limit("client_requests_per_second", LimitUnit.REQUESTS_PER_SECOND, LimitScope.SHARED,
                          translator.rate_limiter.requests_per_second),
        ),
        observed_at, configuration_ref)


def _probe_failure(error: TranslatorError) -> tuple[AvailabilityState, str]:
    if error.reason == "malformed":
        return AvailabilityState.DEGRADED, "malformed_languages"
    if error.reason == "timeout":
        return AvailabilityState.UNAVAILABLE, "provider_timeout"
    if error.reason == "connection":
        return AvailabilityState.UNAVAILABLE, "provider_unavailable"
    if error.reason == "http":
        if error.status_code in (401, 403):
            return AvailabilityState.UNAVAILABLE, "provider_unauthorized"
        if error.status_code is not None and error.status_code >= 500:
            return AvailabilityState.UNAVAILABLE, "provider_server_error"
        return AvailabilityState.UNAVAILABLE, "provider_rejected"
    return AvailabilityState.UNAVAILABLE, "provider_request_failed"


class LibreTranslateAvailabilityProbe:
    """Discovers through `/languages` alone. It sends no document text and calls no `/health`.

    It inherits the translator's timeout and retries, so a composition root bounds its
    cost by giving it a translator with ``retries=0``.
    """

    def __init__(self, translator: LibreTranslateTranslator, *, clock: Callable[[], datetime] = _utc_now,
                 ttl: timedelta = DEFAULT_AVAILABILITY_TTL) -> None:
        self._translator = translator
        self._clock = clock
        self._ttl = ttl

    def __repr__(self) -> str:
        return "LibreTranslateAvailabilityProbe()"

    def probe(self) -> ProviderAvailability:
        def observed(state: AvailabilityState, reason: str | None,
                     languages: tuple[LanguageEvidence, ...] = ()) -> ProviderAvailability:
            return observation(state, reason, self._clock(), self._ttl, languages)

        try:
            languages = self._translator.list_languages()
        except TranslatorError as error:
            return observed(*_probe_failure(error))
        if not languages:
            return observed(AvailabilityState.DEGRADED, "no_languages")
        if len(languages) > MAX_LANGUAGES or any(len(item.targets) > MAX_LANGUAGE_TARGETS for item in languages):
            return observed(AvailabilityState.DEGRADED, "discovery_too_large")
        try:
            evidence = tuple(sorted((LanguageEvidence(item.code, tuple(sorted(item.targets))) for item in languages),
                                    key=lambda item: item.code))
            return observed(AvailabilityState.AVAILABLE, None, evidence)
        except ProviderCapabilityError:
            return observed(AvailabilityState.DEGRADED, "malformed_languages")


def discover_route(availability: ProviderAvailability, source: str, target: str, now: datetime,
                   intermediate_candidates: tuple[str, ...] = ("en",)) -> ProviderRoute | None:
    """A route derived only from fresh `/languages` evidence, using the current routing rules."""
    current = availability.current(now)
    if current.state is not AvailabilityState.AVAILABLE:
        return None
    languages = tuple(TranslatorLanguage(item.code, item.code, item.targets) for item in current.languages)
    try:
        route = resolve_translation_route(languages, source, target, intermediate_candidates)
    except RouteResolutionError:
        return None
    return ProviderRoute(route.source, route.target, route.intermediate)
