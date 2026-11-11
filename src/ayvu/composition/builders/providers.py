"""Provider wiring selected by the explicit CLI composition boundary (#158).

The request snapshot is shared with the translator and the existing #128 use cases.
"""
from __future__ import annotations

from ...network import NetworkAuthorization
from ...network import NetworkPolicy, resolve_network_policy
from ...provider_session import CliProviderSession
from ...providers import ProviderSetupError
from ...v1.translator import LibreTranslateTranslator, TranslatorError


def cli_provider_session(*, url: str, timeout: float, retries: int, requests_per_second: float | None = None,
                         retry_backoff: float = 0.5, retry_backoff_max: float = 8.0,
                         network_policy: NetworkPolicy | str | None = None) -> CliProviderSession:
    try:
        translator = LibreTranslateTranslator(
            url=url, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            network_authorization=NetworkAuthorization(resolve_network_policy(network_policy, url)))
    except TranslatorError:
        # Its message is not forwarded: a bad setting is reported with a code.
        raise ProviderSetupError("invalid_provider_settings") from None
    return CliProviderSession(translator)
