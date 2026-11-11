"""test-translator and languages: talk to the local translator."""
from __future__ import annotations


from typing import Optional
import typer
from ....composition.cli import cli_provider_session
from ....kernel import UserMode
from ....providers import ProviderSetupError
from ....providers import SYNTHETIC_TEST_TEXT, DiscoveryOutcome, LanguagesResult
from ....network import NetworkPolicy, network_next_step

from ..render import _print_expected_error, _print_languages, _print_network_selection, console
from ..options import DEFAULT_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF_MAX, DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE, DEFAULT_TRANSLATOR_URL, NETWORK_POLICY_HELP, _selected_network_policy


def test_translator(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    url: str = typer.Option(DEFAULT_TRANSLATOR_URL, "--url", help="LibreTranslate base URL or /translate endpoint."),
    source: str = typer.Option(DEFAULT_SOURCE_LANGUAGE, "--source"),
    target: str = typer.Option(DEFAULT_TARGET_LANGUAGE, "--target"),
    timeout: float = typer.Option(10.0, "--timeout"),
    retries: int = typer.Option(1, "--retries"),
    requests_per_second: Optional[float] = typer.Option(
        None,
        "--requests-per-second",
        help="Maximum translator HTTP requests per second. Omit for no rate limit.",
    ),
    retry_backoff: float = typer.Option(
        DEFAULT_RETRY_BACKOFF,
        "--retry-backoff",
        help="Initial retry backoff delay in seconds.",
    ),
    retry_backoff_max: float = typer.Option(
        DEFAULT_RETRY_BACKOFF_MAX,
        "--retry-backoff-max",
        help="Maximum retry backoff delay in seconds.",
    ),
) -> None:
    """Test connectivity with the local translator by sending the text "Hello world"."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        session = cli_provider_session(
            network_policy=_selected_network_policy(network_policy),
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
        )
        result = session.test(source, target)
        _print_network_selection(session.network_decision)
        failure_code = None if result.outcome is DiscoveryOutcome.OK else result.code
    except ProviderSetupError as exc:
        failure_code = str(exc)
    if failure_code is not None:
        _print_expected_error(
            "O teste do tradutor falhou.",
            network_next_step(failure_code) or "Inicie o LibreTranslate, verifique --url e tente novamente.",
            mode,
            detail=session.network_decision.diagnostic("translate", failure_code) if network_next_step(failure_code) else failure_code,
        )
        raise typer.Exit(code=1)
    console.print(f"[green]Translator OK:[/green] {SYNTHETIC_TEST_TEXT} -> {result.text}")


def languages(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    url: str = typer.Option(DEFAULT_TRANSLATOR_URL, "--url", help="LibreTranslate base URL or /translate endpoint."),
    timeout: float = typer.Option(10.0, "--timeout"),
    retries: int = typer.Option(1, "--retries"),
    requests_per_second: Optional[float] = typer.Option(
        None,
        "--requests-per-second",
        help="Maximum translator HTTP requests per second. Omit for no rate limit.",
    ),
    retry_backoff: float = typer.Option(
        DEFAULT_RETRY_BACKOFF,
        "--retry-backoff",
        help="Initial retry backoff delay in seconds.",
    ),
    retry_backoff_max: float = typer.Option(
        DEFAULT_RETRY_BACKOFF_MAX,
        "--retry-backoff-max",
        help="Maximum retry backoff delay in seconds.",
    ),
) -> None:
    """List languages reported by the local LibreTranslate server."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        session = cli_provider_session(
            network_policy=_selected_network_policy(network_policy),
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
        )
        result = session.languages()
        _print_network_selection(session.network_decision)
        failure_code = None if _languages_were_listed(result) else result.code
    except ProviderSetupError as exc:
        failure_code = str(exc)
    if failure_code is not None:
        _print_expected_error(
            "Não foi possível listar os idiomas.",
            network_next_step(failure_code) or "Inicie o LibreTranslate, verifique --url e tente novamente.",
            mode,
            detail=session.network_decision.diagnostic("languages", failure_code) if network_next_step(failure_code) else failure_code,
        )
        raise typer.Exit(code=1)

    _print_languages(result.languages)


def _languages_were_listed(result: LanguagesResult) -> bool:
    # A server with no installed language answered correctly: it just has nothing to list.
    return result.outcome is DiscoveryOutcome.OK or result.code == "no_languages"
