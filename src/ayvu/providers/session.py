"""One provider behind the shared provider discovery use cases (#158): LibreTranslate, or another adapter.

The session carries the network decision each operation must present. The caller passes the translator
in, so translation preflight and the CLI composition share one adapter.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from .capabilities import Capability, ProviderRequirements
from .discovery import (
    LanguagesResult, ProviderDiscoveryAdapter, ProviderDiscoveryService, ProviderPreflightResult, ProviderTestResult,
)
from .libretranslate.adapter import ADAPTER_ID, ADAPTER_VERSION, LibreTranslateDiscoveryAdapter
from .network import NetworkClass, ProviderOperation, ResolvedProviderNetworkDecision, network_binding
from .service import ProviderInstance
from .libretranslate.client import LibreTranslateTranslator
from ..network import NetworkDecision, NetworkPolicy, resolve_network_policy

CLI_INSTANCE_ID = "libretranslate_cli"
_ISSUER = "cli_composition"  # the issuer name since #158, a value and not a module path
_POLICY_REVISION = 1
_DECISION_LIFETIME = timedelta(minutes=5)
_PREFLIGHT_REQUIREMENTS = ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.ROUTE_DISCOVERY))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CliProviderSession:
    """One translator behind the shared use cases, with the decision each operation must present."""

    def __init__(self, translator: LibreTranslateTranslator, *, clock: Callable[[], datetime] = _utc_now) -> None:
        decision = getattr(translator, "network_decision", None)
        if decision is None:
            # Compatibility for non-HTTP injected translators; production supplies the snapshot.
            decision = resolve_network_policy(None, "http://localhost:5000")
        self._open(decision, ProviderInstance(CLI_INSTANCE_ID, ADAPTER_ID, ADAPTER_VERSION, 1),
                   LibreTranslateDiscoveryAdapter(translator, clock=clock), clock)

    @classmethod
    def for_adapter(cls, network_decision: NetworkDecision, instance_id: str, adapter: ProviderDiscoveryAdapter,
                    *, clock: Callable[[], datetime] = _utc_now) -> CliProviderSession:
        """Another provider's discovery adapter, under the same per-run network decision."""
        descriptor = adapter.descriptor
        session = cls.__new__(cls)
        session._open(network_decision, ProviderInstance(instance_id, descriptor.adapter_id, descriptor.adapter_version, 1),
                      adapter, clock)
        return session

    def _open(self, decision: NetworkDecision, instance: ProviderInstance, adapter: ProviderDiscoveryAdapter,
              clock: Callable[[], datetime]) -> None:
        self._clock, self.network_decision, self._instance_id = clock, decision, instance.instance_id
        self._service = ProviderDiscoveryService(trusted_issuers=frozenset({_ISSUER}), policy_revision=_POLICY_REVISION, clock=clock)
        self._service.register(instance, adapter)

    def __repr__(self) -> str:
        return "CliProviderSession()"

    def _decision(self, operation: ProviderOperation) -> ResolvedProviderNetworkDecision:
        now = self._clock()
        return ResolvedProviderNetworkDecision(
            allowed=self.network_decision.allowed,
            network_class=(NetworkClass.LOOPBACK_ONLY if self.network_decision.allowed else
                           NetworkClass.STRICT_OFFLINE if self.network_decision.policy is NetworkPolicy.NONE else NetworkClass.UNCLASSIFIED),
            reason_code=self.network_decision.reason,
            binding=network_binding(self._instance_id, None, operation), policy_revision=_POLICY_REVISION, issuer=_ISSUER,
            issued_at=now, expires_at=now + _DECISION_LIFETIME)

    def languages(self) -> LanguagesResult:
        return self._service.languages(self._instance_id, self._decision(ProviderOperation.LANGUAGES))

    def test(self, source: str, target: str) -> ProviderTestResult:
        return self._service.test_provider(self._instance_id, self._decision(ProviderOperation.TEST), source, target)

    def preflight(self, source: str, target: str) -> ProviderPreflightResult:
        return self._service.preflight(self._instance_id, self._decision(ProviderOperation.PREFLIGHT),
                                       _PREFLIGHT_REQUIREMENTS, source, target)
