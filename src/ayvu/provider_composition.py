"""Provider wiring selected by the explicit CLI composition boundary (#158).

The decision made here says plainly that no network policy ran: the CLI has none yet, and
it allows what the user asked for, as it always has. It looks at no URL to claim otherwise.
"""
from __future__ import annotations

from .cli_contracts import ProviderSetupError

from datetime import UTC, datetime, timedelta
from typing import Callable

from .provider_capabilities import Capability, ProviderRequirements
from .provider_discovery import LanguagesResult, ProviderDiscoveryService, ProviderPreflightResult, ProviderTestResult
from .provider_libretranslate import ADAPTER_ID, ADAPTER_VERSION, LibreTranslateDiscoveryAdapter
from .provider_network import NetworkClass, ProviderOperation, ResolvedProviderNetworkDecision, network_binding
from .provider_service import ProviderInstance
from .translator import LibreTranslateTranslator, TranslatorError

CLI_INSTANCE_ID = "libretranslate_cli"
_ISSUER = "cli_composition"
_POLICY_REVISION = 0
_DECISION_LIFETIME = timedelta(minutes=5)
_PREFLIGHT_REQUIREMENTS = ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.ROUTE_DISCOVERY))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CliProviderSession:
    """One translator behind the shared use cases, with the decision each operation must present."""

    def __init__(self, translator: LibreTranslateTranslator, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._service = ProviderDiscoveryService(trusted_issuers=frozenset({_ISSUER}), policy_revision=_POLICY_REVISION, clock=clock)
        self._service.register(ProviderInstance(CLI_INSTANCE_ID, ADAPTER_ID, ADAPTER_VERSION, 1),
                               LibreTranslateDiscoveryAdapter(translator, clock=clock))

    def __repr__(self) -> str:
        return "CliProviderSession()"

    def _decision(self, operation: ProviderOperation) -> ResolvedProviderNetworkDecision:
        now = self._clock()
        return ResolvedProviderNetworkDecision(
            allowed=True, network_class=NetworkClass.UNCLASSIFIED, reason_code="legacy_cli_unrestricted",
            binding=network_binding(CLI_INSTANCE_ID, None, operation), policy_revision=_POLICY_REVISION, issuer=_ISSUER,
            issued_at=now, expires_at=now + _DECISION_LIFETIME)

    def languages(self) -> LanguagesResult:
        return self._service.languages(CLI_INSTANCE_ID, self._decision(ProviderOperation.LANGUAGES))

    def test(self, source: str, target: str) -> ProviderTestResult:
        return self._service.test_provider(CLI_INSTANCE_ID, self._decision(ProviderOperation.TEST), source, target)

    def preflight(self, source: str, target: str) -> ProviderPreflightResult:
        return self._service.preflight(CLI_INSTANCE_ID, self._decision(ProviderOperation.PREFLIGHT),
                                       _PREFLIGHT_REQUIREMENTS, source, target)


def cli_provider_session(*, url: str, timeout: float, retries: int, requests_per_second: float | None = None,
                         retry_backoff: float = 0.5, retry_backoff_max: float = 8.0) -> CliProviderSession:
    try:
        translator = LibreTranslateTranslator(
            url=url, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max)
    except TranslatorError:
        # Its message is not forwarded: a bad setting is reported with a code.
        raise ProviderSetupError("invalid_provider_settings") from None
    return CliProviderSession(translator)
