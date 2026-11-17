"""What Ollama states about itself: its facts, the installed model's identity, and discovery.

The producer is ``ollama`` with the model ``<name>@<digest>``, so new weights under the same name give
a new identity and old cache entries are never reused. A model has no fixed language list, so the
languages are declared here; ``/api/tags`` is the only thing observed, and no document text is sent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from ...provenance import ProducerIdentity, observed_model
from ..capabilities import (
    AvailabilityState, Capability, CapabilityFact, EvidenceSource, LanguageEvidence, LimitOrigin, LimitScope,
    LimitSemantics, LimitUnit, ProviderAvailability, ProviderCapabilitySnapshot, ProviderDescriptor, ProviderLimit,
    SupportState, observation,
)
from ..discovery import DiscoveredLanguage, DiscoveryObservation
from ..errors import ProviderSetupError
from ..llm import LLM_CORE_VERSION, LlmSettings, LlmTranslationProvider, llm_snapshot
from ..ports import ProviderRoute
from .chat import OllamaChatModel
from .client import InstalledModel, OllamaClient, OllamaError

ADAPTER_ID = "ollama"
ADAPTER_VERSION = 1
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_AVAILABILITY_TTL = timedelta(seconds=60)
OLLAMA_DESCRIPTOR = ProviderDescriptor(ADAPTER_ID, ADAPTER_VERSION, "ollama_chat_http", 1, (1,), (1,))
# A model has no fixed list: these are declared, and quality depends on the model chosen.
DECLARED_LANGUAGES = (
    ("ar", "Arabic"), ("de", "German"), ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("hi", "Hindi"),
    ("it", "Italian"), ("ja", "Japanese"), ("ko", "Korean"), ("nl", "Dutch"), ("pl", "Polish"),
    ("pt", "Portuguese"), ("ru", "Russian"), ("sv", "Swedish"), ("tr", "Turkish"), ("uk", "Ukrainian"),
    ("zh", "Chinese"),
)
DECLARED_CODES = frozenset(code for code, _ in DECLARED_LANGUAGES)


def _fact(capability: Capability, state: SupportState, evidence: EvidenceSource) -> CapabilityFact:
    return CapabilityFact(capability, state, evidence)


# The transport facts the LLM core requires from an adapter, each stated from this adapter's code or
# from what the server reports at runtime.
TRANSPORT_FACTS = (
    # The digest comes from /api/tags, observed before the first unit.
    _fact(Capability.MODEL_IDENTITY, SupportState.SUPPORTED, EvidenceSource.RUNTIME_OBSERVATION),
    # Declared, not discovered: the server reports no language list.
    _fact(Capability.LANGUAGE_DISCOVERY, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.ROUTE_DISCOVERY, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.IN_FLIGHT_CANCELLATION, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    # Each worker gets its own client and HTTP session.
    _fact(Capability.SHARED_INSTANCE_CONCURRENCY, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    # The adapter sends no credential.
    _fact(Capability.AUTHENTICATION, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    # Token counts are reported by the server but not recorded yet.
    _fact(Capability.COST_REPORTING, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    # A loopback address proves nothing about privacy, retention or the absence of egress.
    _fact(Capability.NETWORK_CLASS, SupportState.CONFIGURATION_DEPENDENT, EvidenceSource.CONFIGURATION),
    *(_fact(item, SupportState.UNKNOWN, EvidenceSource.NONE) for item in (
        Capability.RETENTION_CLAIM, Capability.TRAINING_CLAIM, Capability.RESOURCE_REPORTING)),
)


def ollama_producer(model: InstalledModel) -> ProducerIdentity:
    return ProducerIdentity(ADAPTER_ID, observed_model(model.name, model.digest),
                            f"ollama_chat@{ADAPTER_VERSION}", f"llm_core@{LLM_CORE_VERSION}")


def ollama_snapshot(model: InstalledModel | None, observed_at: datetime, *, timeout_seconds: float,
                    requests_per_second: float | None = None, max_workers: int = 1) -> ProviderCapabilitySnapshot:
    """The digest is also the configuration reference, so it is part of the capability fingerprint.

    Client limits are Ayvu's own policy and stay out of the fingerprint; the server's are unknown.
    """
    def client_limit(name: str, unit: LimitUnit, scope: LimitScope, value: int | float | None) -> ProviderLimit:
        semantics = LimitSemantics.UNBOUNDED if value is None else LimitSemantics.KNOWN
        return ProviderLimit(name, unit, scope, LimitOrigin.CLIENT_POLICY, semantics, EvidenceSource.CONFIGURATION,
                             value)

    limits = (
        ProviderLimit("server_max_input_chars", LimitUnit.CHARACTERS, LimitScope.REQUEST, LimitOrigin.SERVER,
                      LimitSemantics.UNKNOWN, EvidenceSource.NONE),
        ProviderLimit("server_max_concurrent_requests", LimitUnit.REQUESTS, LimitScope.SHARED, LimitOrigin.SERVER,
                      LimitSemantics.UNKNOWN, EvidenceSource.NONE),
        client_limit("client_request_timeout", LimitUnit.MILLISECONDS, LimitScope.REQUEST, timeout_seconds * 1000),
        client_limit("client_requests_per_second", LimitUnit.REQUESTS_PER_SECOND, LimitScope.SHARED,
                     requests_per_second),
        client_limit("client_max_workers", LimitUnit.REQUESTS, LimitScope.SHARED, max_workers),
    )
    return llm_snapshot(OLLAMA_DESCRIPTOR, TRANSPORT_FACTS, limits, observed_at,
                        configuration_ref=None if model is None else f"ollama_model:{model.digest}")


def find_model(client: OllamaClient, name: str) -> InstalledModel:
    """The installed model of that exact name. Raises ``ProviderSetupError`` with a code, before any book work."""
    try:
        models = client.list_models()
    except OllamaError as error:
        raise ProviderSetupError(_unavailable(error)) from None
    for model in models:
        if model.name == name:
            return model
    raise ProviderSetupError("model_not_installed")


def _unavailable(error: OllamaError) -> str:
    if error.reason == "policy":
        return error.code
    if error.reason == "http":
        return "provider_server_error" if error.status_code >= 500 else "provider_rejected"
    return {"timeout": "provider_timeout", "connection": "provider_unavailable", "malformed": "malformed_models",
            "redirect": "provider_redirect_refused"}.get(error.reason, "provider_request_failed")


class OllamaDiscoveryAdapter:
    """The shared discovery use cases over one server and, for a test, one installed model."""

    descriptor = OLLAMA_DESCRIPTOR

    def __init__(self, client: OllamaClient, model_name: str | None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 ttl: timedelta = DEFAULT_AVAILABILITY_TTL, settings: LlmSettings = LlmSettings()) -> None:
        self._client, self._model_name, self._clock, self._ttl = client, model_name, clock, ttl
        self._settings = settings
        self._model: InstalledModel | None = None

    def __repr__(self) -> str:
        return "OllamaDiscoveryAdapter()"

    def snapshot(self) -> ProviderCapabilitySnapshot:
        rate = getattr(self._client.rate_limiter, "requests_per_second", None)
        return ollama_snapshot(self._model, self._clock(), timeout_seconds=self._client.timeout,
                               requests_per_second=rate)

    def observe(self) -> DiscoveryObservation:
        """Reachable means ``/api/tags`` answered; the selected model, when there is one, must be installed."""
        try:
            if self._model_name is None:
                self._client.list_models()
            else:
                self._model = find_model(self._client, self._model_name)
        except ProviderSetupError as error:
            state = AvailabilityState.DEGRADED if str(error) == "model_not_installed" else AvailabilityState.UNAVAILABLE
            return DiscoveryObservation(observation(state, str(error), self._clock(), self._ttl))
        except OllamaError as error:
            return DiscoveryObservation(observation(AvailabilityState.UNAVAILABLE, _unavailable(error), self._clock(),
                                                    self._ttl))
        evidence = tuple(LanguageEvidence(code, tuple(sorted(DECLARED_CODES - {code}))) for code, _ in DECLARED_LANGUAGES)
        display = tuple(DiscoveredLanguage(code, name, tuple(sorted(DECLARED_CODES - {code})), "declared")
                        for code, name in DECLARED_LANGUAGES)
        return DiscoveryObservation(observation(AvailabilityState.AVAILABLE, None, self._clock(), self._ttl, evidence),
                                    display)

    def route(self, availability: ProviderAvailability, source: str, target: str, now: datetime,
              candidates: tuple[str, ...]) -> ProviderRoute | None:
        """A model translates directly: a declared pair is a direct route, and there is no pivot."""
        if availability.current(now).state is not AvailabilityState.AVAILABLE:
            return None
        return ProviderRoute(source, target) if {source, target} <= DECLARED_CODES else None

    def provider(self) -> LlmTranslationProvider:
        if self._model_name is None:
            raise ProviderSetupError("model_not_selected")
        model = self._model or find_model(self._client, self._model_name)
        return LlmTranslationProvider(OllamaChatModel(self._client, model.name), ollama_producer(model),
                                      settings=self._settings)
