"""Exposes the current LibreTranslate translator through the rich provider contract.

It wraps ``Translator`` and changes nothing in it: HTTP, routing, retries, backoff,
timeouts and rate limiting stay where they are. Errors are classified from typed
attributes and their messages are never read, because a message can quote the
response body or a URL holding credentials.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from .job_events import JobFailure, JobFailureFamily, normalize_failure
from .provenance import NOT_APPLICABLE, UNKNOWN, ProducerIdentity
from .provider import (
    DeadlineFeature, DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderRequest,
    TranslationProviderResult,
)
from .provider_service import FeatureDecision, ProviderInstance, decide_features, failed_result
from .translator import LibreTranslateTranslator, RoutedTranslator, TranslationRoute, Translator, TranslatorError

ADAPTER_ID = "libretranslate"
ADAPTER_VERSION = 1
SUPPORTED_CONFIG_VERSIONS = frozenset({1})
LIBRETRANSLATE_SAFE_POINTS = ("provider_before_request",)
_PRODUCER = ProducerIdentity("libretranslate", UNKNOWN, f"libretranslate_compat@{ADAPTER_VERSION}", NOT_APPLICABLE)
# It cannot honor any optional feature: requirements fail and hints become warnings.
_SUPPORTED_FEATURES: frozenset[str] = frozenset()


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
