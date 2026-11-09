"""Provider ports, an explicitly populated registry, and strict result checking.

No code here knows a provider by name. A registry is filled at a composition root
and never imports anything, so user data cannot cause code to load.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Protocol

from .job import is_slug
from .job_events import JobFailure, JobFailureFamily, JobWarning, normalize_failure
from .provider_capabilities import AvailabilityState, ProviderAvailability
from .provenance import UNKNOWN, ProducerIdentity
from .provider import (
    DeliveryState, ProviderError, ProviderStatus, Requirement, TranslationProviderRequest, TranslationProviderResult,
    valid_reference,
)

UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)


class ProviderRegistryError(ProviderError):
    pass


class TranslationProvider(Protocol):
    def execute(self, request: TranslationProviderRequest) -> TranslationProviderResult: ...
    def close(self) -> None: ...


class ProviderAvailabilityProbe(Protocol):
    def probe(self) -> ProviderAvailability: ...


class TranslationProviderFactory(Protocol):
    """Owns creation. It implies no singleton: each ``create`` is a provider its caller closes."""

    adapter_id: str
    adapter_version: int
    supported_config_versions: frozenset[int]

    def create(self, instance: ProviderInstance) -> TranslationProvider: ...


@dataclass(frozen=True, repr=False)
class ProviderInstance:
    """A stable reference to one configured provider. Its configuration is opaque here."""

    instance_id: str
    adapter_id: str
    adapter_version: int
    config_version: int
    configuration_ref: str | None = None

    def __post_init__(self) -> None:
        if (not is_slug(self.instance_id) or not is_slug(self.adapter_id)
                or type(self.adapter_version) is not int or self.adapter_version < 1
                or type(self.config_version) is not int or self.config_version < 1
                or not valid_reference(self.configuration_ref)):
            raise ProviderRegistryError("invalid_instance")

    def __repr__(self) -> str:
        return f"ProviderInstance(instance_id={self.instance_id!r}, adapter_id={self.adapter_id!r})"


class ProviderRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[ProviderInstance, TranslationProviderFactory]] = {}

    def __repr__(self) -> str:
        return f"ProviderRegistry(instances={len(self._entries)})"

    def register(self, instance: ProviderInstance, factory: TranslationProviderFactory) -> None:
        if type(instance) is not ProviderInstance:
            raise ProviderRegistryError("invalid_instance")
        if instance.instance_id in self._entries:
            raise ProviderRegistryError("duplicate_instance")
        if (factory.adapter_id, factory.adapter_version) != (instance.adapter_id, instance.adapter_version):
            raise ProviderRegistryError("incompatible_adapter")
        if instance.config_version not in factory.supported_config_versions:
            raise ProviderRegistryError("incompatible_configuration")
        self._entries[instance.instance_id] = (instance, factory)

    def instance_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    @contextmanager
    def open(self, instance_id: str) -> Iterator[TranslationProvider]:
        """A new provider for this block, closed when it ends, however it ends."""
        entry = self._entries.get(instance_id) if isinstance(instance_id, str) else None
        if entry is None:
            raise ProviderRegistryError("unknown_instance")
        instance, factory = entry
        try:
            provider = factory.create(instance)
        except Exception:
            # A factory's exception may quote a URL or a credential.
            raise ProviderRegistryError("factory_failed") from None
        if not callable(getattr(provider, "execute", None)) or not callable(getattr(provider, "close", None)):
            raise ProviderRegistryError("factory_failed")
        try:
            yield provider
        except BaseException:
            try:
                provider.close()
            except Exception:
                pass  # The failure of the block is the one worth reporting.
            raise
        try:
            provider.close()
        except Exception:
            raise ProviderRegistryError("cleanup_failed") from None


@dataclass(frozen=True)
class FeatureDecision:
    unsupported_required: tuple[str, ...]
    ignored_advisory: tuple[JobWarning, ...]


def decide_features(request: TranslationProviderRequest, supported: frozenset[str]) -> FeatureDecision:
    """Say what a provider cannot honor, so a requirement fails and a hint is never silently dropped."""
    required = []
    ignored = []
    for item in request.features:
        if item.name in supported:
            continue
        if item.requirement is Requirement.REQUIRED:
            required.append(item.name)
        else:
            ignored.append(JobWarning(f"ignored_{item.name}"))
    return FeatureDecision(tuple(required), tuple(ignored))


def observe_availability(probe: ProviderAvailabilityProbe) -> ProviderAvailability:
    """A broken probe proves nothing about the provider, so the answer is unknown."""
    try:
        availability = probe.probe()
    except Exception:
        return ProviderAvailability(AvailabilityState.UNKNOWN, "probe_failed")
    if type(availability) is not ProviderAvailability:
        return ProviderAvailability(AvailabilityState.UNKNOWN, "probe_failed")
    return availability


def failed_result(request: TranslationProviderRequest, failure: JobFailure, *,
                  producer: ProducerIdentity = UNKNOWN_PRODUCER, delivery: DeliveryState = DeliveryState.UNKNOWN,
                  warnings: tuple[JobWarning, ...] = (), route: object = None) -> TranslationProviderResult:
    status = ProviderStatus.CANCELLED if failure.family is JobFailureFamily.CANCELLED else ProviderStatus.FAILED
    return TranslationProviderResult(request.request_id, status, producer, delivery, failure=failure, route=route,
                                     warnings=warnings)


def execute(provider: TranslationProvider, request: TranslationProviderRequest) -> TranslationProviderResult:
    """Run a provider so that nothing it does can be mistaken for a valid translation."""
    if type(request) is not TranslationProviderRequest:
        raise ProviderRegistryError("invalid_request")
    try:
        result = provider.execute(request)
    except Exception as error:
        return failed_result(request, normalize_failure(error))
    if type(result) is not TranslationProviderResult:
        return failed_result(request, JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "malformed_provider_result"))
    if result.request_id != request.request_id:
        return failed_result(request, JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "result_identity_conflict"),
                             producer=result.producer)
    # Only a success claims to have used a route. A refusal keeps its own code and delivery state.
    if (result.status is ProviderStatus.SUCCEEDED and request.route is not None and result.route is not None
            and result.route != request.route):
        return failed_result(request, JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "result_route_conflict"),
                             producer=result.producer)
    return result
