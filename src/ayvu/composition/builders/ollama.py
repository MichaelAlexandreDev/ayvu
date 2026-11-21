"""Composition of the Ollama provider: the client under the run's network decision, the model's discovery
before any book work, the LLM binding the rich runtime translates with, and the CLI discovery session.

Discovery runs before the job: the producer names the model's digest, and the job records its producer
when it starts. A missing model, an unreachable server or a refused network policy stops the run here,
with nothing of the book read or sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from ...knowledge.glossary import Glossary
from ...network import NetworkDecision, NetworkPolicyError, network_next_step, resolve_network_policy
from ...network.http import NetworkAuthorization
from ...providers import ProviderSetupError
from ...providers.libretranslate.client import RequestRateLimiter, RetryPolicy, TranslationRoute, TranslatorError
from ...providers.llm import LlmSettings
from ...providers.ollama.chat import OllamaChatModel
from ...providers.ollama.client import OllamaClient
from ...providers.ollama.discovery import (
    DECLARED_CODES, DEFAULT_OLLAMA_URL, OllamaDiscoveryAdapter, find_model, ollama_producer, ollama_snapshot,
)
from ...providers.session import CliProviderSession
from ...translation import PreflightError, PreflightPolicyError
from .llm import LlmBinding

OLLAMA = "ollama"
CLI_INSTANCE_ID = "ollama_cli"
# The CLI's --url defaults to LibreTranslate's address; for Ollama that default means Ollama's own.
LIBRETRANSLATE_DEFAULT_URL = "http://localhost:5000"


@dataclass(frozen=True)
class OllamaSettings:
    """``parallelism`` bounds the document workers; Ollama's own OLLAMA_NUM_PARALLEL bounds the server."""

    parallelism: int = 1
    llm: LlmSettings = LlmSettings()


@dataclass(frozen=True, repr=False)
class OllamaPreflight:
    """What a translation with Ollama hands the rich runtime. The translator slot stays empty, because
    the binding creates each worker's model, with its own HTTP session, itself."""

    glossary: Glossary
    binding: LlmBinding
    route: TranslationRoute
    network_decision: NetworkDecision
    translator: None = None

    def __repr__(self) -> str:
        return "OllamaPreflight()"

    @staticmethod
    def translator_factory() -> None:
        return None


def ollama_url(url: str | None) -> str:
    return DEFAULT_OLLAMA_URL if url in (None, LIBRETRANSLATE_DEFAULT_URL) else url


def transport_controls(timeout: float, retries: int, requests_per_second: float | None, retry_backoff: float,
               retry_backoff_max: float) -> tuple[RequestRateLimiter | None, tuple[float, ...]]:
    """LibreTranslate's validation and backoff: one shared limiter, and the delay before each retry.

    Raises ``TranslatorError`` for an invalid value; each caller reports it in its own terms.
    """
    if not timeout > 0:
        raise TranslatorError("timeout must be greater than zero")
    policy = RetryPolicy(retries, retry_backoff, retry_backoff_max)
    limiter = None if requests_per_second is None else RequestRateLimiter(requests_per_second)
    return limiter, tuple(policy.delay_for(attempt) for attempt in range(1, policy.max_attempts))


def _refusal(code: str, model: str) -> PreflightError:
    if code == "model_not_installed":
        return PreflightError("The Ollama model is not installed.",
                              f"Run 'ollama pull {model}', or set translator_models.ollama to a name from 'ollama list'.",
                              detail=code)
    return PreflightError("Could not reach Ollama.", "Start Ollama ('ollama serve') and check --url.", detail=code)


def _checked_request(*, model: str | None, workers: int, dry_run: bool, cache_only: bool, language_pair,
                     settings: OllamaSettings) -> str:
    require_supported_mode(dry_run=dry_run, cache_only=cache_only)
    if model is None:
        raise PreflightError("Ollama needs an installed model, named in translator_models.ollama.",
                             "Run 'ollama list' and set translator_models.ollama to one of its names, such as qwen2.5:7b.")
    if workers > settings.parallelism:
        raise PreflightError(f"Ollama runs with at most {settings.parallelism} worker(s) here.",
                             f"Use --workers {settings.parallelism} with --translator ollama.")
    if not {language_pair.source, language_pair.target} <= DECLARED_CODES:
        raise PreflightError("The language pair is not declared for Ollama.",
                             "See 'ayvu languages --translator ollama' for the declared languages.")
    return model


def ollama_preflight(*, book_checks, epub_path, cache_path, glossary_path, url, timeout, retries, language_pair,
                     network_policy, cache_check, requests_per_second=None, retry_backoff=0.5, retry_backoff_max=8.0,
                     dry_run=False, cache_only=False, model=None, workers=1, settings: OllamaSettings = OllamaSettings(),
                     clock: Callable[[], datetime] = lambda: datetime.now(UTC), **_ignored) -> OllamaPreflight:
    """Refuses what Ollama cannot run, checks the book and the cache, then finds the model by name.

    ``book_checks`` is the provider-independent part of the translation preflight, given by the CLI root.
    """
    name = _checked_request(model=model, workers=workers, dry_run=dry_run, cache_only=cache_only,
                            language_pair=language_pair, settings=settings)
    try:
        limiter, delays = transport_controls(timeout, retries, requests_per_second, retry_backoff, retry_backoff_max)
    except TranslatorError as error:
        raise PreflightError("Invalid Ollama settings.", "Check --timeout, --retries and --requests-per-second.",
                             detail=str(error)) from None
    authorization = NetworkAuthorization(resolve_network_policy(network_policy, ollama_url(url)))
    try:
        authorization.decision.require("languages")
    except NetworkPolicyError as error:
        raise PreflightPolicyError(error.code, network_next_step(error.code), detail=str(error)) from None
    glossary = book_checks(epub_path=epub_path, cache_path=cache_path, glossary_path=glossary_path,
                           language_pair=language_pair, cache_check=cache_check)
    discovery = OllamaClient(authorization, timeout=timeout, rate_limiter=limiter)
    try:
        installed = find_model(discovery, name)
    except ProviderSetupError as error:
        raise _refusal(str(error), name) from None
    finally:
        discovery.close()

    def model_factory() -> OllamaChatModel:
        client = OllamaClient(authorization, timeout=timeout, rate_limiter=limiter, retry_delays=delays)
        return OllamaChatModel(client, installed.name)

    def snapshot(now: datetime):
        return ollama_snapshot(installed, now, timeout_seconds=timeout, requests_per_second=requests_per_second,
                               max_workers=settings.parallelism)

    binding = LlmBinding(model_factory, ollama_producer(installed), snapshot, clock=clock, settings=settings.llm)
    return OllamaPreflight(glossary, binding, TranslationRoute(language_pair.source, language_pair.target),
                           authorization.decision)


def ollama_provider_session(*, url: str, timeout: float, requests_per_second: float | None = None,
                            network_policy=None, model: str | None = None, **_ignored) -> CliProviderSession:
    """``test-translator`` and ``languages``. A test needs the model; the language list does not."""
    try:
        limiter, _ = transport_controls(timeout, 0, requests_per_second, 0.0, 0.0)
    except TranslatorError:
        # Its message is not forwarded: a bad setting is reported with a code.
        raise ProviderSetupError("invalid_provider_settings") from None
    decision = resolve_network_policy(network_policy, ollama_url(url))
    client = OllamaClient(NetworkAuthorization(decision), timeout=timeout, rate_limiter=limiter)
    return CliProviderSession.for_adapter(decision, CLI_INSTANCE_ID, OllamaDiscoveryAdapter(client, model))


def discover_ollama_models(*, url='http://localhost:11434', timeout=3.0, network_policy=None):
    """Settings discovery uses the same policy and adapter as translation."""
    from ...providers.ollama.discovery import discover_model_names

    client = OllamaClient(NetworkAuthorization(resolve_network_policy(network_policy, url)), timeout=timeout)
    try:
        return discover_model_names(client)
    finally:
        client.close()


def require_supported_mode(*, dry_run=False, cache_only=False, preview=False):
    """A dry run with Ollama is an estimate made without Ollama (#380): selection lets it through, and it never
    reaches the preflight, which still refuses one as a defence."""
    if dry_run or cache_only or preview:
        raise PreflightError("Previews and cache-only runs are not available with Ollama yet.",
                             "Run the translation without --cache-only, or use --translator libretranslate.")
