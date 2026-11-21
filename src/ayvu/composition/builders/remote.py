"""Composition of a remote model provider (#379), in the order that keeps book text on the machine.

1. Refuse what a remote run cannot do here: a preview, a cache-only run, several workers. A dry run is an
   estimate made on this machine (#380) and never reaches this preflight.
2. Decide the network: only ``--network-policy remote``, only HTTPS, only the adapter's declared hosts.
3. Read the credential from the provider's standard environment variable, now, as an opaque reference.
4. Check the project's consent, asking the owner once through the interface's callback.
5. Check the book and the cache; only then build the LLM binding whose models make requests.

A refusal at any step sends nothing. No remote adapter ships yet: Anthropic (AI-07) and OpenAI (AI-08)
register in ``REMOTE_ADAPTERS``; without an entry, a name is not a remote provider.
"""
from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from ...knowledge.glossary import Glossary
from ...network import NetworkDecision, NetworkPolicyError, network_next_step, resolve_network_policy
from ...network.remote_http import RemoteHttpClient
from ...providers import ProviderSetupError, RemoteModelAdapter, check_remote_adapter, credential_from_environment
from ...providers.libretranslate.client import TranslationRoute, TranslatorError
from ...providers.llm import LlmSettings
from ...translation import PreflightError, PreflightPolicyError
from .llm import LlmBinding
from .ollama import transport_controls
from .remote_consent import RemoteEgressNotice, current_gate

# Remote model adapters by translator name. Empty until a provider issue adds one.
REMOTE_ADAPTERS: dict[str, RemoteModelAdapter] = {}


def remote_adapter(translator_name: str) -> RemoteModelAdapter | None:
    return REMOTE_ADAPTERS.get(str(translator_name).strip().lower())


def is_remote(translator_name: str) -> bool:
    return remote_adapter(translator_name) is not None


def egress_notice(translator_name: str) -> RemoteEgressNotice | None:
    """What a remote run would disclose, for the plan, the consent question and the report; None when local."""
    adapter = remote_adapter(translator_name)
    if adapter is None:
        return None
    return RemoteEgressNotice(adapter.ADAPTER_ID, tuple(adapter.ALLOWED_HOSTS), adapter.RETENTION_CLAIM)


@dataclass(frozen=True, repr=False)
class RemotePreflight:
    """What a remote translation hands the rich runtime. Each worker's model gets its own HTTPS client."""

    glossary: Glossary
    binding: LlmBinding
    route: TranslationRoute
    network_decision: NetworkDecision
    retention_claim: str
    translator: None = None

    def __repr__(self) -> str:
        return "RemotePreflight()"

    @staticmethod
    def translator_factory() -> None:
        return None


def require_remote_mode(*, dry_run=False, cache_only=False, preview=False) -> None:
    """A dry run is an estimate made on this machine (#380): selection lets it through without consent, and it
    never reaches the preflight, which still refuses one as a defence."""
    if dry_run or cache_only or preview:
        raise PreflightError("Previews and cache-only runs are not available with a remote provider.",
                             "Run a project translation without --cache-only, or use a local translator.")


def _checked_request(adapter: RemoteModelAdapter, *, model, workers, dry_run, cache_only, language_pair,
                     timeout) -> str:
    try:
        check_remote_adapter(adapter)
    except ProviderSetupError:
        raise PreflightError("The remote provider is not configured correctly.", "Use another translator.",
                             detail="invalid_remote_adapter") from None
    require_remote_mode(dry_run=dry_run, cache_only=cache_only)
    name = adapter.ADAPTER_ID
    if model is None:
        raise PreflightError(f"{name} needs a model, named in translator_models.{name}.",
                             f"Set translator_models.{name} to the provider's model id, or pass --model.")
    if workers != 1:
        raise PreflightError("A remote provider runs with one worker here.", "Use --workers 1.")
    if not {language_pair.source, language_pair.target} <= adapter.DECLARED_CODES:
        raise PreflightError(f"The language pair is not declared for {name}.", "Choose a declared language pair.")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise PreflightError("Invalid remote provider settings.", "Check --timeout.", detail="invalid_timeout")
    return model


def _decision(adapter: RemoteModelAdapter, network_policy, url: str | None) -> NetworkDecision:
    """``--url`` may only name a declared host; by default the adapter's own base URL is used."""
    decision = resolve_network_policy(network_policy, url or adapter.BASE_URL, adapter.ALLOWED_HOSTS)
    try:
        decision.require("translate")
        if not decision.remote:
            # A loopback URL is allowed under remote, but it is not this provider's host.
            raise NetworkPolicyError(decision, "translate", "unsupported_destination")
    except NetworkPolicyError as error:
        raise PreflightPolicyError(error.code, _next_step(error.code), detail=str(error)) from None
    return decision


def _next_step(code: str) -> str:
    step = network_next_step(code) or ""
    return f"A remote provider needs --network-policy remote and HTTPS to the hosts it declares. {step}".strip()


def remote_preflight(*, book_checks, translator_name, epub_path, cache_path, glossary_path, url, timeout,
                     language_pair, network_policy, cache_check, retries=0, requests_per_second=None,
                     retry_backoff=0.5, retry_backoff_max=8.0, dry_run=False, cache_only=False, model=None,
                     workers=1, environ: Mapping[str, str] | None = None,
                     clock: Callable[[], datetime] = lambda: datetime.now(UTC), **_ignored) -> RemotePreflight:
    """Every refusal happens before the book is read and before any request is sent."""
    adapter = remote_adapter(translator_name)
    if adapter is None:
        raise PreflightError("Unknown remote provider.", "Use --translator libretranslate.")
    name = _checked_request(adapter, model=model, workers=workers, dry_run=dry_run, cache_only=cache_only,
                            language_pair=language_pair, timeout=timeout)
    try:
        limiter, delays = transport_controls(timeout, retries, requests_per_second, retry_backoff, retry_backoff_max)
    except TranslatorError as error:
        raise PreflightError("Invalid remote provider settings.", "Check --retries and --requests-per-second.",
                             detail=str(error)) from None
    decision = _decision(adapter, network_policy, url)
    try:
        credential = credential_from_environment(adapter.CREDENTIAL_ENV, os.environ if environ is None else environ)
    except ProviderSetupError as error:
        raise PreflightError(f"The {adapter.ADAPTER_ID} credential is not set.",
                             f"Set {adapter.CREDENTIAL_ENV} in this shell's environment. Ayvu never saves it.",
                             detail=str(error)) from None
    gate = current_gate()
    if gate is None:
        raise PreflightError("A remote provider translates only inside a project.",
                             "Run a project translation (not a preview, dry run or legacy resume).",
                             detail="remote_without_project")
    gate.require(egress_notice(adapter.ADAPTER_ID))
    glossary = book_checks(epub_path=epub_path, cache_path=cache_path, glossary_path=glossary_path,
                           language_pair=language_pair, cache_check=cache_check)

    def model_factory():
        client = RemoteHttpClient(decision, timeout=timeout, rate_limiter=limiter, retry_delays=delays)
        return adapter.model(client, credential, name)

    def snapshot(now: datetime):
        return adapter.snapshot(name, now, timeout_seconds=timeout)

    binding = LlmBinding(model_factory, adapter.producer(name), snapshot, clock=clock, settings=LlmSettings())
    return RemotePreflight(glossary, binding, TranslationRoute(language_pair.source, language_pair.target), decision,
                           adapter.RETENTION_CLAIM)
