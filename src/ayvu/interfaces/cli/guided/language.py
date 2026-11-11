"""Choosing the target language in the guided flow."""
from __future__ import annotations


import typer
from rich.table import Table
from ..commands.translator import _languages_were_listed
from ....composition.cli import cli_provider_session
from ....providers import DiscoveredLanguage
from ....network import network_next_step

from ..render import _print_network_selection, console
from ..options import DEFAULT_TRANSLATOR_URL, _selected_network_policy


GUIDED_DEFAULT_LANGUAGE_OPTION = "1"


GUIDED_OTHER_LANGUAGE_OPTION = "2"


def _choose_guided_target_language(default_target: str) -> str:
    console.print(f"[yellow]Default target language:[/yellow] {default_target}")
    console.print(f"{GUIDED_DEFAULT_LANGUAGE_OPTION}. Use default target language ({default_target})")
    console.print(f"{GUIDED_OTHER_LANGUAGE_OPTION}. Outro idioma")

    while True:
        choice = typer.prompt("Choose target language", default=GUIDED_DEFAULT_LANGUAGE_OPTION).strip()
        if choice == GUIDED_DEFAULT_LANGUAGE_OPTION:
            return default_target
        if choice == GUIDED_OTHER_LANGUAGE_OPTION:
            return _prompt_target_language_code(default_target)
        console.print("[red]Invalid option.[/red] Choose 1 or 2.")


def _prompt_target_language_code(default_target: str) -> str:
    available_languages = _load_languages_for_guided_selection()
    if available_languages:
        _print_guided_language_choices(available_languages)
    else:
        console.print("Enter a language code manually.")

    target = typer.prompt("Target language option or code", default=default_target).strip()
    selected = _language_code_from_guided_choice(target, available_languages)
    if selected:
        return selected
    return target or default_target


def _print_guided_language_choices(languages: tuple[DiscoveredLanguage, ...]) -> None:
    table = Table(title="LibreTranslate languages")
    table.add_column("Option")
    table.add_column("Language")
    table.add_column("Code")
    table.add_column("State")

    for index, language in enumerate(languages, start=1):
        table.add_row(str(index), language.name, language.code, language.state)
    console.print(table)


def _language_code_from_guided_choice(choice: str, languages: tuple[DiscoveredLanguage, ...]) -> str | None:
    if not choice.isdigit():
        return None

    index = int(choice)
    if 1 <= index <= len(languages):
        return languages[index - 1].code
    return None


def _load_languages_for_guided_selection() -> tuple[DiscoveredLanguage, ...]:
    session = cli_provider_session(url=DEFAULT_TRANSLATOR_URL, timeout=10.0, retries=1,
                                   network_policy=_selected_network_policy())
    result = session.languages()
    _print_network_selection(session.network_decision)
    if _languages_were_listed(result):
        return result.languages
    console.print(f"[yellow]Could not list LibreTranslate languages:[/yellow] {result.code}")
    if network_next_step(result.code):
        console.print(session.network_decision.diagnostic("languages", result.code))
        console.print(network_next_step(result.code))
    return ()
