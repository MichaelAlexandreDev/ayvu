"""Provider wiring selected by the explicit CLI composition boundary (#158).

The request snapshot is shared with the translator and the existing #128 use cases.
"""
from __future__ import annotations

from ...network.http import NetworkAuthorization
from ...network import NetworkPolicy, resolve_network_policy
from ...providers.session import CliProviderSession
from ...providers import ProviderSetupError
from ...preferences import AyvuConfig
from ...translation import PreflightError
from .selection import select_translator
from ...providers.libretranslate.client import LibreTranslateTranslator, TranslatorError
from .ollama import OLLAMA, ollama_provider_session


def cli_provider_session(*, url: str, timeout: float, retries: int, requests_per_second: float | None = None,
                         retry_backoff: float = 0.5, retry_backoff_max: float = 8.0,
                         network_policy: NetworkPolicy | str | None = None, translator_name: str = "libretranslate",
                         model: str | None = None, translator_models=None) -> CliProviderSession:
    if translator_name not in ("libretranslate", OLLAMA):
        raise ProviderSetupError("unsupported_translator")
    try:
        model = select_translator(AyvuConfig(translator_models=translator_models or {}),
                                  translator=translator_name, model=model).model
    except PreflightError:
        raise ProviderSetupError("invalid_provider_model") from None
    if translator_name == OLLAMA:
        return ollama_provider_session(url=url, timeout=timeout, requests_per_second=requests_per_second,
                                       network_policy=network_policy, model=model)
    try:
        translator = LibreTranslateTranslator(
            url=url, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            network_authorization=NetworkAuthorization(resolve_network_policy(network_policy, url)))
    except TranslatorError:
        # Its message is not forwarded: a bad setting is reported with a code.
        raise ProviderSetupError("invalid_provider_settings") from None
    return CliProviderSession(translator)


def discover_translator_models(*, translator_name, **settings):
    from .ollama import discover_ollama_models

    if translator_name != OLLAMA:
        raise ProviderSetupError('unsupported_model_discovery')
    return discover_ollama_models(**settings)
