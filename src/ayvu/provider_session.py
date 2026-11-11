"""One LibreTranslate translator behind the shared provider discovery use cases (#158).

The session carries the network decision each operation must present. The caller passes the translator
in, so translation preflight and the CLI composition share one adapter.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from .provider_capabilities import Capability, ProviderRequirements
from .provider_discovery import LanguagesResult, ProviderDiscoveryService, ProviderPreflightResult, ProviderTestResult
from .provider_libretranslate import ADAPTER_ID, ADAPTER_VERSION, LibreTranslateDiscoveryAdapter
from .provider_network import NetworkClass, ProviderOperation, ResolvedProviderNetworkDecision, network_binding
from .provider_service import ProviderInstance
from .translator import LibreTranslateTranslator
from .network_policy import NetworkPolicy, resolve_network_policy

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
        self._clock = clock
        self.network_decision = getattr(translator, "network_decision", None)
        if self.network_decision is None:
            # Compatibility for non-HTTP injected translators; production supplies the snapshot.
            self.network_decision = resolve_network_policy(None, "http://localhost:5000")
        self._service = ProviderDiscoveryService(trusted_issuers=frozenset({_ISSUER}), policy_revision=_POLICY_REVISION, clock=clock)
        self._service.register(ProviderInstance(CLI_INSTANCE_ID, ADAPTER_ID, ADAPTER_VERSION, 1),
                               LibreTranslateDiscoveryAdapter(translator, clock=clock))

    def __repr__(self) -> str:
        return "CliProviderSession()"

    def _decision(self, operation: ProviderOperation) -> ResolvedProviderNetworkDecision:
        now = self._clock()
        return ResolvedProviderNetworkDecision(
            allowed=self.network_decision.allowed,
            network_class=(NetworkClass.LOOPBACK_ONLY if self.network_decision.allowed else
                           NetworkClass.STRICT_OFFLINE if self.network_decision.policy is NetworkPolicy.NONE else NetworkClass.UNCLASSIFIED),
            reason_code=self.network_decision.reason,
            binding=network_binding(CLI_INSTANCE_ID, None, operation), policy_revision=_POLICY_REVISION, issuer=_ISSUER,
            issued_at=now, expires_at=now + _DECISION_LIFETIME)

    def languages(self) -> LanguagesResult:
        return self._service.languages(CLI_INSTANCE_ID, self._decision(ProviderOperation.LANGUAGES))

    def test(self, source: str, target: str) -> ProviderTestResult:
        return self._service.test_provider(CLI_INSTANCE_ID, self._decision(ProviderOperation.TEST), source, target)

    def preflight(self, source: str, target: str) -> ProviderPreflightResult:
        return self._service.preflight(CLI_INSTANCE_ID, self._decision(ProviderOperation.PREFLIGHT),
                                       _PREFLIGHT_REQUIREMENTS, source, target)
