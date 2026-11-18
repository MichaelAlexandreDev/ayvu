"""Guided translation and preview, with the choice of profile and glossary."""
from __future__ import annotations
from .glossaries import _list_glossary_files, _load_guided_glossary, _prompt_saved_glossary_path
from .language import _choose_guided_target_language
from .preferences import _ensure_guided_model_label, _settings_with_translator_model, _update_guided_settings

from .. import preview as previews, run as translation_run, translate_options, settings
from pathlib import Path
import typer
from rich.table import Table
from ....preferences import AyvuConfig, TranslationProfile
from ....composition.cli import managed_glossary_repository
from ....kernel import DEFAULT_PERFORMANCE_PROFILE, UserMode

from ..render import console
from ..options import DEFAULT_CACHE_PATH, DEFAULT_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF_MAX, DEFAULT_TRANSLATOR_URL


PROFILE_NO_PROFILE_OPTION = "0"


def _run_guided_translation(config: AyvuConfig) -> None:
    if not _offers_translator_choice(config):
        config = _ensure_guided_model_label(config)  # LibreTranslate only: asked first, as before #377.
    epub_path = Path(typer.prompt("EPUB path")).expanduser()
    profile_name, profile = _choose_guided_translation_profile(config)
    selected = _guided_translator(config, profile)
    config = selected.config
    target = _choose_guided_target_language(
        translate_options._resolve_target_language(None, profile, default_target=config.default_target_language),
        translator_name=selected.translator, url=selected.url,
    )
    glossary_path = _choose_guided_profile_glossary(profile) or _choose_guided_translation_glossary()
    translation_run._run_translation(
        epub_path=epub_path,
        output=None,
        review_output=None,
        source=None,
        target=target,
        translator_name=selected.translator,
        url=selected.url,
        cache_path=DEFAULT_CACHE_PATH,
        glossary_path=glossary_path,
        profile_name=profile_name,
        performance_profile_name=DEFAULT_PERFORMANCE_PROFILE,
        dry_run=False,
        fail_fast=False,
        chapter_selection=None,
        overwrite=False,
        timeout=30.0,
        retries=2,
        requests_per_second=None,
        retry_backoff=DEFAULT_RETRY_BACKOFF,
        retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
        chunk_limit=3000,
        workers=1,
        mode=UserMode.COMMON,
        config=config,
    )


def _run_guided_preview(config: AyvuConfig) -> None:
    # Ollama has no preview yet, so guided preview stays on LibreTranslate and asks nothing new.
    config = _ensure_guided_model_label(config)
    epub_path = Path(typer.prompt("EPUB path")).expanduser()
    target = _choose_guided_target_language(config.default_target_language)
    previews._run_preview(epub_path, target=target, mode=UserMode.COMMON, config=config)


def _choose_guided_translation_profile(
    config: AyvuConfig,
) -> tuple[str | None, TranslationProfile | None]:
    if not config.profiles:
        return None, None

    profiles = tuple(sorted(config.profiles.items()))
    console.print("[yellow]Translation profiles are configured.[/yellow]")
    _print_translation_profiles(profiles)
    console.print(f"{PROFILE_NO_PROFILE_OPTION}. Run without profile")

    while True:
        choice = typer.prompt("Choose profile", default=PROFILE_NO_PROFILE_OPTION).strip()
        if choice == PROFILE_NO_PROFILE_OPTION:
            return None, None
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            name, profile = profiles[int(choice) - 1]
            console.print(f"[green]Profile selected:[/green] {name}")
            return name, profile
        if choice in config.profiles:
            console.print(f"[green]Profile selected:[/green] {choice}")
            return choice, config.profiles[choice]
        console.print(f"[red]Invalid profile.[/red] Choose 1-{len(profiles)} or 0.")


def _print_translation_profiles(profiles: tuple[tuple[str, TranslationProfile], ...]) -> None:
    table = Table(title="Translation profiles")
    table.add_column("Option")
    table.add_column("Name")
    table.add_column("Target")
    table.add_column("Glossary")
    table.add_column("Style")
    for index, (name, profile) in enumerate(profiles, start=1):
        table.add_row(
            str(index),
            name,
            profile.target_language or "-",
            str(profile.glossary) if profile.glossary else "-",
            profile.style or "-",
        )
    console.print(table)


def _choose_guided_profile_glossary(profile: TranslationProfile | None) -> Path | None:
    glossary_path = translate_options._resolve_glossary_path(None, profile)
    if glossary_path is None:
        return None
    console.print(f"[green]Profile glossary:[/green] {glossary_path}")
    return glossary_path


def _choose_guided_translation_glossary() -> Path | None:
    directory = translate_options.default_glossaries_dir()
    repository = managed_glossary_repository(directory)
    items = _list_glossary_files(repository)
    if not items:
        return None
    console.print("[yellow]Saved glossaries are available.[/yellow]")
    while True:
        item = _prompt_saved_glossary_path(items, back_label="Run without glossary")
        if item is None:
            return None
        if _load_guided_glossary(repository, item.reference) is not None:
            path = directory / item.reference.value
            console.print(f"[green]Glossary selected:[/green] {path}")
            return path
        console.print("Choose another glossary or run without glossary.")


def _offers_translator_choice(config, profile=None) -> bool:
    """Only an Ollama model in the configuration or a profile naming a translator makes it a choice.

    Without one, guided translation runs LibreTranslate and asks exactly what it asked before #377.
    """
    if "ollama" in config.translator_models:
        return True
    profiles = config.profiles.values() if profile is None else (profile,)
    return any(item.translator is not None for item in profiles)


def _guided_translator(config, profile=None, *, preview=False):
    selected = settings._select_translator(config, UserMode.COMMON, profile=profile, preview=preview)
    name = (typer.prompt("Translator (libretranslate or ollama)", default=selected.translator).strip()
            if _offers_translator_choice(config, profile) else selected.translator)
    selected = settings._select_translator(config, UserMode.COMMON, translator=name, profile=profile, preview=preview)
    if selected.model is None:
        if selected.translator == 'libretranslate':
            config = _ensure_guided_model_label(config)
        else:
            updated = _settings_with_translator_model(config, selected.translator)
            config = _update_guided_settings(settings.configuration_store(), config, updated)
        selected = settings._select_translator(config, UserMode.COMMON, translator=name, profile=profile, preview=preview)
    if selected.model is None:
        console.print("Canceled.")
        raise typer.Exit(code=1)
    return selected
