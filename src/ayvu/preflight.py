from __future__ import annotations

from .cli_contracts import PreflightError, PreflightPolicyError

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .cache import TranslationCache
from .domain import LanguagePair, LanguagePairError
from .epub_io import inspect_epub
from .glossary import Glossary, GlossaryError, load_glossary
from .provider_composition import CliProviderSession
from .provider_discovery import DiscoveryOutcome
from .network_policy import NetworkDecision, NetworkPolicy, NetworkPolicyError, resolve_network_policy, network_next_step
from .network_http import NetworkAuthorization
from .translator import (
    RequestRateLimiter,
    RetryPolicy,
    RoutedTranslator,
    TranslationRoute,
    Translator,
    TranslatorError,
    create_translator,
    validate_translator_name,
)


@dataclass(frozen=True)
class TranslationPreflightResult:
    translator: Translator | None
    glossary: Glossary
    route: TranslationRoute | None = None
    translator_factory: Callable[[], Translator] | None = None
    network_decision: NetworkDecision | None = None


def run_translation_preflight(
    epub_path: Path,
    cache_path: Path,
    glossary_path: Path | None,
    translator_name: str,
    url: str,
    timeout: float,
    retries: int,
    language_pair: LanguagePair,
    dry_run: bool,
    cache_only: bool = False,
    requests_per_second: float | None = None,
    retry_backoff: float = 0.5,
    retry_backoff_max: float = 8.0,
    network_policy: NetworkPolicy | str | None = None,
) -> TranslationPreflightResult:
    _check_language_pair(language_pair)
    glossary = _load_checked_glossary(glossary_path)
    translator = None
    translator_factory = None
    rate_limiter = RequestRateLimiter(requests_per_second) if requests_per_second is not None else None
    if cache_only:
        # Retain the old configuration refusals without acquiring a provider or session.
        try:
            validate_translator_name(translator_name)
            RetryPolicy(retries, retry_backoff, retry_backoff_max)
        except TranslatorError as exc:
            raise PreflightError(
                "Não foi possível preparar o tradutor.",
                "Use --translator libretranslate.",
                detail=str(exc),
            ) from exc
    if not cache_only:
        authorization = NetworkAuthorization(resolve_network_policy(network_policy, url))
        if not dry_run:
            try:
                authorization.decision.require("languages")
            except NetworkPolicyError as error:
                raise PreflightPolicyError(error.code, network_next_step(error.code), detail=str(error)) from None
        translator_factory = _create_checked_translator_factory(
            translator_name, url, timeout, retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max, rate_limiter=rate_limiter,
            network_authorization=authorization,
        )
        translator = translator_factory()
    _check_cache(cache_path)
    _check_epub(epub_path)
    route: TranslationRoute | None = None
    if not dry_run and not cache_only:
        route = _resolve_route(translator, language_pair)
        if route is not None and not route.is_direct:
            translator = RoutedTranslator(translator, route)
            base_factory = translator_factory
            translator_factory = lambda: RoutedTranslator(base_factory(), route)
    return TranslationPreflightResult(
        translator=translator,
        glossary=glossary,
        route=route,
        translator_factory=translator_factory,
        network_decision=None if cache_only else authorization.decision,
    )


def _check_language_pair(language_pair: LanguagePair) -> None:
    try:
        language_pair.validate_for_translation()
    except LanguagePairError as exc:
        raise PreflightError(
            "O par de idiomas informado não é válido.",
            "Use códigos de idioma não vazios em --source e --target, por exemplo --source en --target pt.",
            detail=str(exc),
        ) from exc


def _load_checked_glossary(glossary_path: Path | None) -> Glossary:
    try:
        return load_glossary(glossary_path)
    except GlossaryError as exc:
        raise PreflightError(
            "Não foi possível carregar o glossário.",
            "Crie o arquivo, informe o caminho correto, ou remova --glossary para rodar sem glossário.",
            detail=str(exc),
        ) from exc


def _create_checked_translator(
    name: str,
    url: str,
    timeout: float,
    retries: int,
    requests_per_second: float | None,
    retry_backoff: float,
    retry_backoff_max: float,
    rate_limiter: RequestRateLimiter | None = None,
    network_authorization: NetworkAuthorization | None = None,
) -> Translator:
    try:
        return create_translator(
            name,
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
            rate_limiter=rate_limiter,
            network_authorization=network_authorization,
        )
    except TranslatorError as exc:
        raise PreflightError(
            "Não foi possível preparar o tradutor.",
            "Use --translator libretranslate.",
            detail=str(exc),
        ) from exc


def _create_checked_translator_factory(
    name: str,
    url: str,
    timeout: float,
    retries: int,
    requests_per_second: float | None,
    retry_backoff: float,
    retry_backoff_max: float,
    rate_limiter: RequestRateLimiter | None,
    network_authorization: NetworkAuthorization | None = None,
) -> Callable[[], Translator]:
    def factory() -> Translator:
        return _create_checked_translator(
            name,
            url,
            timeout,
            retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
            rate_limiter=rate_limiter,
            network_authorization=network_authorization,
        )

    return factory


def _check_cache(cache_path: Path) -> None:
    try:
        with TranslationCache(cache_path) as cache:
            cache.verify_writable()
    except (OSError, sqlite3.Error) as exc:
        raise PreflightError(
            "Não foi possível criar ou escrever o cache.",
            "Escolha um caminho de cache com permissão de escrita usando --cache, ou ajuste as permissões da pasta do cache.",
            detail=f"Cache em {cache_path}: {exc}",
        ) from exc


def _check_epub(epub_path: Path) -> None:
    try:
        inspect_epub(epub_path)
    except Exception as exc:
        raise PreflightError(
            "Não foi possível ler o EPUB informado.",
            "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
            detail=f"{epub_path}: {exc}",
        ) from exc


def _resolve_route(
    translator: Translator,
    language_pair: LanguagePair,
) -> TranslationRoute | None:
    """Uses only discovery evidence. A translator that cannot be asked gets no hidden translation."""
    if getattr(translator, "list_languages", None) is None:
        return None
    session = CliProviderSession(translator)
    result = session.preflight(language_pair.source, language_pair.target)
    if result.outcome is DiscoveryOutcome.OK:
        route = result.route
        return TranslationRoute(source=route.source, target=route.target, intermediate=route.intermediate)
    if result.outcome is DiscoveryOutcome.NO_ROUTE or result.code == "no_languages":
        if result.code == "no_languages" and language_pair.source == language_pair.target:
            return TranslationRoute(source=language_pair.source, target=language_pair.target)
        raise PreflightError(
            "O par de idiomas não está disponível no tradutor.",
            (
                "Confirme em 'ayvu languages' os idiomas instalados, escolha outro --target, "
                "ou instale o idioma necessário no LibreTranslate."
            ),
            detail=f"No translation route available from '{language_pair.source}' to '{language_pair.target}'.",
        )
    next_step = network_next_step(result.code)
    if next_step is not None:
        raise PreflightPolicyError(result.code, next_step, session.network_decision.diagnostic("languages", result.code))
    raise PreflightError("O tradutor não respondeu.",
                         "Inicie o LibreTranslate, verifique --url e confirme que o servidor está acessível.",
                         detail=result.code or "provider_unavailable")
