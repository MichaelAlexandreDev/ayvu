from __future__ import annotations
from . import preview as previews, run as translation_run, settings, translate_options
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Optional
import typer
from rich.table import Table
from .render import _print_network_selection, console
from .options import DEFAULT_CACHE_PATH, DEFAULT_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF_MAX, DEFAULT_TARGET_LANGUAGE, DEFAULT_TRANSLATOR_URL, NETWORK_POLICY_HELP, _invocation_network_policy, _selected_network_policy
from . import report as reports
from .settings import _refuse_invalid_translator_models
from .commands import cache, content, resume, review, translate, translator
from .commands.resume import _offer_detected_translation_resume, _print_processing_translation_states
from .commands.translator import _languages_were_listed
from ...preferences import DEFAULT_BOOKS_DIR, AyvuConfig, ConfigError, FolderNames, TranslationProfile, config_from_preferences
from .contracts import ConfigStore
from ...composition.cli import configuration_store, library_service, managed_glossary_repository, cli_provider_session, open_job_recovery
from ...knowledge.glossary import Entry as GlossaryEntry, GlossaryReference, list_glossaries, read_glossary, create_glossary, update_glossary
from ...kernel import DEFAULT_PERFORMANCE_PROFILE, UserMode
from ...library import Book as LibraryBook, LibraryService, ScanRequest, ListRequest, DescribeRequest, OpenRequest
from ...providers import DiscoveredLanguage
from ...network import NetworkPolicy, network_next_step


GLOSSARY_RULE_TRANSLATE = "translate"


GLOSSARY_RULE_PRESERVE = "preserve"


app = typer.Typer(help="Translate local EPUB files with a local HTTP translator.")


cache_app = typer.Typer(help="Inspect, clean, export, and import the translation cache.")


app.add_typer(cache_app, name="cache")


GUIDED_TRANSLATE_OPTION = "1"


GUIDED_PREVIEW_OPTION = "2"


GUIDED_LIBRARY_OPTION = "3"


GUIDED_SETTINGS_OPTION = "4"


GUIDED_GLOSSARY_OPTION = "5"


GUIDED_HELP_OPTION = "6"


GUIDED_EXIT_OPTION = "0"


GUIDED_DEFAULT_LANGUAGE_OPTION = "1"


GUIDED_OTHER_LANGUAGE_OPTION = "2"


LIBRARY_BACK_OPTION = "0"


SETTINGS_LANGUAGE_OPTION = "1"


SETTINGS_BOOKS_DIR_OPTION = "2"


SETTINGS_FOLDERS_OPTION = "3"


SETTINGS_READER_OPTION = "4"


SETTINGS_EXIT_OPTION = "0"


GLOSSARY_CREATE_OPTION = "1"


GLOSSARY_EDIT_OPTION = "2"


GLOSSARY_PREVIEW_OPTION = "3"


GLOSSARY_BACK_OPTION = "0"


GLOSSARY_RULE_TRANSLATE_OPTION = "1"


GLOSSARY_RULE_PRESERVE_OPTION = "2"


PROFILE_NO_PROFILE_OPTION = "0"


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    mode: Optional[UserMode] = typer.Option(
        None,
        "--mode",
        help="Execution mode (common or developer). If not specified, it's inferred from usage.",
    ),
    preview: Optional[Path] = typer.Option(
        None,
        "--preview",
        help="Generate a translated EPUB preview with default settings.",
    ),
) -> None:
    """Translate local EPUB files with a local HTTP translator."""
    if mode is None:
        mode = UserMode.DEVELOPER
        # COMMON mode is only when running 'ayvu' without subcommands or options (except for help)
        # Typer sets invoked_subcommand when a subcommand is used.
        # ctx.params contains options of the callback itself.
        if ctx.invoked_subcommand is None and not any(v for k, v in ctx.params.items() if k not in {"mode", "network_policy"}):
            mode = UserMode.COMMON
    ctx.ensure_object(dict)
    ctx.obj["mode"] = mode
    ctx.obj["network_policy"] = network_policy
    network_token = _invocation_network_policy.set(network_policy)
    ctx.call_on_close(lambda: _invocation_network_policy.reset(network_token))

    if ctx.invoked_subcommand is not None:
        return

    if preview is not None:
        previews._run_preview(preview, mode=mode)
        return

    config = settings._load_existing_config_or_default()
    recovery = open_job_recovery(settings._processing_dir(config))
    found = _print_processing_translation_states(recovery)
    if _offer_detected_translation_resume(found, recovery, mode=mode):
        return

    if _run_guided_main_flow(ctx, mode=mode):
        return

    console.print(ctx.get_help())


app.command()(content.inspect)


app.command('test-translator')(translator.test_translator)


app.command('languages')(translator.languages)


cache_app.command('inspect')(cache.cache_inspect)


cache_app.command('clean')(cache.cache_clean)


cache_app.command('export')(cache.cache_export)


cache_app.command('import')(cache.cache_import)


app.command()(translate.translate)


app.command()(resume.resume)


app.command()(content.extract)


app.command('apply-review')(review.apply_review)


def _run_guided_main_flow(ctx: typer.Context, mode: UserMode) -> bool:
    if mode == UserMode.DEVELOPER:
        return False

    config = _load_or_init_config()
    _print_guided_main_menu()
    choice = typer.prompt("Choose an option", default=GUIDED_PREVIEW_OPTION).strip()
    return _handle_guided_main_choice(choice, ctx, config)


def _load_or_init_config(store: ConfigStore | None = None) -> AyvuConfig:
    store = store or configuration_store()
    snapshot = store.get_preferences()
    _refuse_invalid_translator_models(snapshot)
    if snapshot.error:
        console.print(f"[yellow]Configuração inválida ignorada:[/yellow] {snapshot.error}")
        console.print("Usando configuração padrão. Ajuste o arquivo de configuração se quiser alterar o idioma.")
        return AyvuConfig.default()
    if not snapshot.exists:
        return _init_default_language_config(store)
    if snapshot.read_only:
        console.print("[yellow]Configuração em somente leitura: campos desconhecidos impedem a gravação.[/yellow]")
    return config_from_preferences(snapshot.preferences, snapshot.revision)


def _init_default_language_config(store: ConfigStore) -> AyvuConfig:
    console.print("[yellow]Primeiro uso do modo comum.[/yellow]")
    console.print("Escolha o idioma padrão de leitura/tradução. Você poderá alterá-lo depois em Settings.")
    language = _prompt_target_language_code(DEFAULT_TARGET_LANGUAGE)
    console.print(
        "Escolha a pasta base dos livros. "
        "O Ayvu usará essa pasta para biblioteca, previews, relatórios e traduções."
    )
    books_dir = _prompt_path("Books folder", Path(DEFAULT_BOOKS_DIR))
    folders = _choose_initial_folder_names(books_dir)
    config = AyvuConfig(
        default_target_language=language,
        books_dir=books_dir,
        folders=folders,
        _preferences_revision='missing',
    )
    config, persisted = _save_config(store, config)
    if persisted:
        console.print(f"[green]Idioma padrão salvo:[/green] {language}")
        console.print(f"[green]Pasta base salva:[/green] {books_dir}")
    return config


def _choose_initial_folder_names(books_dir: Path) -> FolderNames:
    config = AyvuConfig(books_dir=books_dir)
    console.print("O Ayvu usará estes nomes de pastas por padrão:")
    _print_feature_folder_paths(config)
    if typer.confirm("Manter estes nomes de pastas?", default=True):
        return config.folders

    updated = _settings_with_folder_names(config)
    _print_feature_folder_paths(updated)
    return updated.folders


def _print_feature_folder_paths(config: AyvuConfig) -> None:
    table = Table(title="Feature folders")
    table.add_column("Feature")
    table.add_column("Folder")
    table.add_row("Original books", str(config.original_dir))
    table.add_row("Translated books", str(config.translated_dir))
    table.add_row("Previews", str(config.preview_dir))
    table.add_row("Reports", str(config.reports_dir))
    table.add_row("Processing states", str(config.processing_dir))
    console.print(table)


def _save_config(
    store: ConfigStore, config: AyvuConfig, fallback: AyvuConfig | None = None,
) -> tuple[AyvuConfig, bool]:
    try:
        result = store.update_preferences(config, config._preferences_revision or 'missing')
    except ConfigError as exc:
        console.print(f"[yellow]Não foi possível salvar a configuração:[/yellow] {exc}")
        console.print("A configuração informada é inválida; os valores anteriores foram mantidos.")
        return fallback or AyvuConfig.default(), False
    if not result.persisted:
        console.print(f"[yellow]Não foi possível salvar a configuração:[/yellow] {result.error}")
        console.print("A mudança será usada nesta execução, mas não foi persistida.")
    revision = result.snapshot.revision if result.snapshot else config._preferences_revision
    return config_from_preferences(result.effective, revision), result.persisted


def _print_guided_main_menu() -> None:
    table = Table(title="Ayvu")
    table.add_column("Option")
    table.add_column("Action")
    table.add_row(GUIDED_TRANSLATE_OPTION, "Translate a book")
    table.add_row(GUIDED_PREVIEW_OPTION, "Generate preview")
    table.add_row(GUIDED_LIBRARY_OPTION, "Open library")
    table.add_row(GUIDED_SETTINGS_OPTION, "Settings")
    table.add_row(GUIDED_GLOSSARY_OPTION, "Glossaries")
    table.add_row(GUIDED_HELP_OPTION, "Show command help")
    table.add_row(GUIDED_EXIT_OPTION, "Exit")
    console.print(table)


def _handle_guided_main_choice(choice: str, ctx: typer.Context, config: AyvuConfig) -> bool:
    if choice == GUIDED_TRANSLATE_OPTION:
        _run_guided_translation(config)
        return True

    if choice == GUIDED_PREVIEW_OPTION:
        _run_guided_preview(config)
        return True

    if choice == GUIDED_LIBRARY_OPTION:
        _run_guided_library(config)
        return True

    if choice == GUIDED_SETTINGS_OPTION:
        _run_guided_settings(config)
        return True

    if choice == GUIDED_GLOSSARY_OPTION:
        _run_guided_glossaries()
        return True

    if choice == GUIDED_HELP_OPTION:
        console.print(ctx.get_help())
        return True

    if choice == GUIDED_EXIT_OPTION:
        console.print("Canceled.")
        return True

    console.print("[red]Unknown option.[/red]")
    console.print(ctx.get_help())
    return True


def _run_guided_translation(config: AyvuConfig) -> None:
    epub_path = Path(typer.prompt("EPUB path")).expanduser()
    profile_name, profile = _choose_guided_translation_profile(config)
    target = _choose_guided_target_language(
        translate_options._resolve_target_language(None, profile, default_target=config.default_target_language)
    )
    glossary_path = _choose_guided_profile_glossary(profile) or _choose_guided_translation_glossary()
    translation_run._run_translation(
        epub_path=epub_path,
        output=None,
        review_output=None,
        source=None,
        target=target,
        translator_name="libretranslate",
        url=DEFAULT_TRANSLATOR_URL,
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


def _run_guided_glossaries() -> None:
    directory = translate_options.default_glossaries_dir()
    _print_guided_glossary_menu(directory)
    choice = typer.prompt("Choose a glossary action", default=GLOSSARY_BACK_OPTION).strip()
    if choice == GLOSSARY_CREATE_OPTION:
        _create_guided_glossary(directory)
        return
    if choice == GLOSSARY_EDIT_OPTION:
        _edit_guided_glossary(directory)
        return
    if choice == GLOSSARY_PREVIEW_OPTION:
        _preview_guided_glossary(directory)
        return
    if choice == GLOSSARY_BACK_OPTION:
        console.print("Glossaries closed.")
        return

    console.print("[red]Invalid glossary action.[/red] Choose 1, 2, 3, or 0.")


def _print_guided_glossary_menu(directory: Path) -> None:
    console.print(f"[yellow]Glossaries folder:[/yellow] {directory}")
    table = Table(title="Glossaries")
    table.add_column("Option")
    table.add_column("Action")
    table.add_row(GLOSSARY_CREATE_OPTION, "Create glossary")
    table.add_row(GLOSSARY_EDIT_OPTION, "Edit saved glossary")
    table.add_row(GLOSSARY_PREVIEW_OPTION, "Preview saved glossary")
    table.add_row(GLOSSARY_BACK_OPTION, "Back")
    console.print(table)


def _create_guided_glossary(directory: Path) -> Path | None:
    repository = managed_glossary_repository(directory)
    items = _list_glossary_files(repository)
    if items is None:
        return None
    path = _prompt_new_glossary_path(directory, items)
    if path is None:
        return None
    entries = _prompt_guided_glossary_terms(())
    return _save_guided_glossary(repository, path, entries)


def _edit_guided_glossary(directory: Path) -> Path | None:
    repository = managed_glossary_repository(directory)
    items = _list_glossary_files(repository)
    if not items:
        if items is not None:
            console.print("[yellow]No saved glossaries found.[/yellow]")
        return None
    item = _prompt_saved_glossary_path(items)
    if item is None:
        return None
    snapshot = _load_guided_glossary(repository, item.reference)
    if snapshot is None:
        return None
    entries = _prompt_guided_glossary_terms(snapshot.entries)
    return _save_guided_glossary(repository, directory / item.reference.value, entries, snapshot.revision)


def _preview_guided_glossary(directory: Path) -> None:
    repository = managed_glossary_repository(directory)
    items = _list_glossary_files(repository)
    if not items:
        if items is not None:
            console.print("[yellow]No saved glossaries found.[/yellow]")
        return
    item = _prompt_saved_glossary_path(items)
    if item is None:
        return
    snapshot = _load_guided_glossary(repository, item.reference)
    if snapshot is not None:
        _print_guided_glossary_preview(snapshot.entries)


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


def _prompt_new_glossary_path(directory: Path, items: tuple) -> Path | None:
    while True:
        raw_name = typer.prompt("Glossary name", default="glossary").strip()
        stem = reports._safe_filename_part(Path(raw_name).stem or raw_name).lower()
        path = directory / f"{stem}.json"
        if not any(item.reference.value.casefold() == path.name.casefold() for item in items):
            return path
        console.print(f"[yellow]Glossary already exists:[/yellow] {path}")
        if not typer.confirm("Choose another glossary name?", default=True):
            console.print("Glossary was not created.")
            return None


def _prompt_guided_glossary_terms(glossary: tuple[GlossaryEntry, ...]) -> tuple[GlossaryEntry, ...]:
    entries = list(glossary)
    if entries:
        _print_guided_glossary_preview(tuple(entries))
    while True:
        term = typer.prompt("Original term (leave empty to finish)", default="").strip()
        if not term:
            if not entries:
                console.print("[yellow]No glossary terms were added.[/yellow]")
            return tuple(entries)
        entry = _prompt_guided_glossary_entry(term)
        replaced = _upsert_glossary_entry(entries, entry)
        if replaced:
            console.print(f"[green]Term updated:[/green] {entry.term}")
        else:
            console.print(f"[green]Term added:[/green] {entry.term}")
        _print_guided_glossary_preview(tuple(entries))
        if not typer.confirm("Add another term?", default=True):
            return tuple(entries)


def _prompt_guided_glossary_entry(term: str) -> GlossaryEntry:
    console.print(f"{GLOSSARY_RULE_TRANSLATE_OPTION}. Use a preferred translation")
    console.print(f"{GLOSSARY_RULE_PRESERVE_OPTION}. Preserve without translation")
    while True:
        rule_choice = typer.prompt("Choose term rule", default=GLOSSARY_RULE_TRANSLATE_OPTION).strip()
        if rule_choice == GLOSSARY_RULE_TRANSLATE_OPTION:
            translation = _prompt_required_text("Desired translation")
            return GlossaryEntry(term=term, rule=GLOSSARY_RULE_TRANSLATE, translation=translation)
        if rule_choice == GLOSSARY_RULE_PRESERVE_OPTION:
            return GlossaryEntry(term=term, rule=GLOSSARY_RULE_PRESERVE)
        console.print("[red]Invalid term rule.[/red] Choose 1 or 2.")


def _prompt_required_text(prompt: str) -> str:
    while True:
        value = typer.prompt(prompt).strip()
        if value:
            return value
        console.print("[red]Value must not be empty.[/red]")


def _upsert_glossary_entry(entries: list[GlossaryEntry], entry: GlossaryEntry) -> bool:
    for index, existing in enumerate(entries):
        if existing.term.casefold() == entry.term.casefold():
            entries[index] = entry
            return True
    entries.append(entry)
    return False


def _save_guided_glossary(repository, path: Path, glossary: tuple[GlossaryEntry, ...], revision=None) -> Path | None:
    if not glossary:
        console.print("[yellow]Glossary was not saved because it has no terms.[/yellow]")
        return None
    _print_guided_glossary_preview(glossary)
    if not typer.confirm("Save this glossary?", default=True):
        console.print("Glossary was not saved.")
        return None
    reference = GlossaryReference(path.name)
    if revision is None:
        result = create_glossary(repository, reference, glossary)
    else:
        result = update_glossary(repository, reference, glossary, revision)
    if result.failure is not None:
        console.print(f"[red]Glossary was not saved:[/red] {_glossary_failure(result.failure)}")
        return None
    console.print(f"[green]Glossary saved:[/green] {path}")
    return path


_GLOSSARY_STATUS = {
    "invalid": "invalid file",
    "oversized": "too large",
    "unsafe-link": "link, not accepted",
    "unsafe-multilink": "hard link, not accepted",
}


_GLOSSARY_FAILURES = {
    "invalid": "the file is not a valid glossary.",
    "oversized": "the file is above the glossary limits.",
    "unsafe-link": "links are not accepted in the glossary folder.",
    "unsafe-multilink": "a file with more than one hard link is not accepted in the glossary folder.",
    "missing": "the file no longer exists.",
    "stale-revision": "the file changed after it was opened. Open it again and repeat the change.",
    "already-exists": "a glossary with this name already exists.",
    "catalog-too-large": "the glossary folder has more than 1000 files.",
}


def _glossary_failure(code: str) -> str:
    return _GLOSSARY_FAILURES.get(code, f"the glossary could not be used ({code}).")


def _load_guided_glossary(repository, reference):
    result = read_glossary(repository, reference)
    if result.failure is not None:
        console.print(f"[red]Could not load glossary:[/red] {_glossary_failure(result.failure)}")
        return None
    return result.value


def _prompt_saved_glossary_path(items: tuple, back_label: str = "Back"):
    _print_saved_glossaries(items)
    console.print(f"{GLOSSARY_BACK_OPTION}. {back_label}")
    while True:
        choice = typer.prompt("Choose glossary", default=GLOSSARY_BACK_OPTION).strip()
        if choice == GLOSSARY_BACK_OPTION:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1]
        console.print(f"[red]Invalid glossary.[/red] Choose 1-{len(items)} or 0.")


def _print_saved_glossaries(items: tuple) -> None:
    table = Table(title="Saved glossaries")
    table.add_column("Option")
    table.add_column("Name")
    table.add_column("Terms")
    for index, item in enumerate(items, start=1):
        table.add_row(str(index), Path(item.reference.value).stem,
                      str(item.count) if item.status == "valid" else _GLOSSARY_STATUS.get(item.status, item.status))
    console.print(table)


def _print_guided_glossary_preview(glossary: tuple[GlossaryEntry, ...]) -> None:
    if not glossary:
        console.print("[yellow]Glossary has no terms yet.[/yellow]")
        return
    table = Table(title="Glossary preview")
    table.add_column("Term")
    table.add_column("Rule")
    table.add_column("Translation")
    for entry in glossary:
        table.add_row(entry.term, entry.rule, entry.translation or "-")
    console.print(table)


def _list_glossary_files(repository):
    result = list_glossaries(repository)
    if result.failure is not None:
        console.print(f"[yellow]Could not list glossaries:[/yellow] {_glossary_failure(result.failure)}")
        return None
    return result.value


_LIBRARY_WARNINGS = {
    "root_unavailable": "the books folder could not be read.",
    "directory_unavailable": "a library folder could not be read.",
    "directory_escape": "a library folder points outside the books folder, so it was skipped.",
    "invalid_folder": "a library folder name is invalid, so it was skipped.",
    "entry_limit": "the library has too many files, so only the first 10000 are listed.",
    "invalid_entry": "an entry is not a file inside the books folder, so it was skipped.",
    "entry_unavailable": "a file could not be read, so it was skipped.",
    "invalid_filename": "a file name has invalid characters or is too long, so it was skipped.",
}


def _run_guided_library(config: AyvuConfig) -> None:
    service = library_service(config)
    scan = service.scan_request(ScanRequest(service.root_ref))
    books = service.list_entries(ListRequest(service.root_ref)).books
    if not books:
        console.print("[yellow]Library has no EPUB books yet.[/yellow]")
        console.print(f"Original folder: {config.original_dir}")
        console.print(f"Translated folder: {config.translated_dir}")
        _print_library_warnings(scan.warnings)
        return

    _print_library_books(books)
    _print_library_warnings(scan.warnings)
    book = _prompt_library_book(books)
    if book is None:
        return
    _run_guided_library_book(service, book, config)


def _print_library_books(books: tuple[LibraryBook, ...]) -> None:
    table = Table(title="Library")
    table.add_column("Option")
    table.add_column("Book")
    table.add_column("Original")
    table.add_column("Translations")

    for index, book in enumerate(books, start=1):
        table.add_row(
            str(index),
            book.name,
            "yes" if book.has_original else "-",
            _display_book_translations(book),
        )
    console.print(table)
    console.print(f"{LIBRARY_BACK_OPTION}. Back")


def _prompt_library_book(books: tuple[LibraryBook, ...]) -> LibraryBook | None:
    while True:
        choice = typer.prompt("Choose a book", default=LIBRARY_BACK_OPTION).strip()
        if choice == LIBRARY_BACK_OPTION:
            console.print("Library closed.")
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(books):
            return books[int(choice) - 1]
        console.print(f"[red]Invalid book.[/red] Choose 1-{len(books)} or 0.")


def _print_library_warnings(warnings: tuple[object, ...]) -> None:
    """Without this, a folder that cannot be read would look like a library with nothing in it."""
    reasons = Counter(warning.reason for warning in warnings)
    for reason, count in sorted(reasons.items()):
        console.print(f"[yellow]Library warning ({count}):[/yellow] {_LIBRARY_WARNINGS.get(reason, reason)}")


def _run_guided_library_book(service: LibraryService, book: LibraryBook, config: AyvuConfig) -> None:
    book = service.describe_entry(DescribeRequest(book.ref)).book
    if book is None:
        return
    while True:
        actions = _library_book_actions(book)
        _print_library_book_actions(book, actions)
        choice = typer.prompt("Choose a library action", default=LIBRARY_BACK_OPTION).strip()
        if choice == LIBRARY_BACK_OPTION:
            console.print("Back to library.")
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(actions):
            console.print(f"[red]Invalid action.[/red] Choose 1-{len(actions)} or 0.")
            continue

        label, ref = actions[int(choice) - 1]
        if ref is None:
            _print_library_book_info(book, config)
            continue
        _open_library_book_path(service, ref, label)
        return


def _library_book_actions(book: LibraryBook) -> list[tuple[str, str | None]]:
    actions: list[tuple[str, str | None]] = []
    if book.original:
        actions.append(("Open original", book.original.ref))
    for translation in book.translations:
        actions.append((f"Open {translation.label}", translation.ref))
    actions.append(("Show information", None))
    return actions


def _print_library_book_actions(book: LibraryBook, actions: list[tuple[str, str | None]]) -> None:
    table = Table(title=book.name)
    table.add_column("Option")
    table.add_column("Action")
    for index, action in enumerate(actions, start=1):
        table.add_row(str(index), action[0])
    console.print(table)
    console.print(f"{LIBRARY_BACK_OPTION}. Back")


def _print_library_book_info(book: LibraryBook, config: AyvuConfig) -> None:
    table = Table(title="Book information")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Book", book.name)
    table.add_row("Original EPUB", str(config.original_dir / book.original.filename) if book.original else "-")
    table.add_row("Translations", _display_book_translations(book))
    table.add_row("Translation files", "\n".join(str(config.translated_dir / t.filename) for t in book.translations) or "-")
    console.print(table)


def _open_library_book_path(service: LibraryService, ref: str, label: str) -> None:
    console.print("[yellow]O leitor externo é outro processo: fica fora da garantia offline do HTTP do Ayvu, inclusive com --network-policy none.[/yellow]")
    result = service.open_entry(OpenRequest(ref, include_display_path=True))
    if result.status != "opened":
        messages = {
            "unavailable": "No EPUB reader app is configured or available on this system.",
            "configuration_invalid": "Reader app command is invalid.",
            "launch_failed": "Could not open EPUB with the configured reader.",
            "unauthorized": "EPUB file was not found or is no longer authorized.",
            "stale": "EPUB file was not found or is no longer authorized.",
        }
        console.print(f"[red]Could not open EPUB:[/red] {messages.get(result.status, 'Could not open EPUB.')}")
        if result.status in {"unavailable", "configuration_invalid", "launch_failed"}:
            console.print("Configure Reader app in Settings or install a default EPUB reader.")
        else:
            console.print("Open the library again to see the current files.")
        return

    console.print(f"[green]{label}:[/green] {result.display_path}")


def _display_book_translations(book: LibraryBook) -> str:
    if not book.translations:
        return "-"
    return ", ".join(translation.label for translation in book.translations)


def _run_guided_settings(config: AyvuConfig) -> None:
    store = configuration_store()
    current = config
    while True:
        _print_settings(current)
        _print_settings_menu()
        choice = typer.prompt("Choose a setting", default=SETTINGS_EXIT_OPTION).strip()
        if choice == SETTINGS_LANGUAGE_OPTION:
            current = _update_guided_settings(store, current, _settings_with_default_language(current))
            continue
        if choice == SETTINGS_BOOKS_DIR_OPTION:
            current = _update_guided_settings(store, current, _settings_with_books_dir(current))
            continue
        if choice == SETTINGS_FOLDERS_OPTION:
            current = _update_guided_settings(store, current, _settings_with_folder_names(current))
            continue
        if choice == SETTINGS_READER_OPTION:
            current = _update_guided_settings(store, current, _settings_with_reader_app(current))
            continue
        if choice == SETTINGS_EXIT_OPTION:
            console.print("Settings closed.")
            return

        console.print("[red]Invalid setting.[/red] Choose 1, 2, 3, 4, or 0.")


def _print_settings(config: AyvuConfig) -> None:
    table = Table(title="Settings")
    table.add_column("Preference")
    table.add_column("Current value")
    table.add_row("Default target language", config.default_target_language)
    table.add_row("Books folder", str(config.books_dir))
    table.add_row("Original folder", config.folders.original)
    table.add_row("Translated folder", config.folders.translated)
    table.add_row("Preview folder", config.folders.preview)
    table.add_row("Reports folder", config.folders.reports)
    table.add_row("Processing folder", config.folders.processing)
    table.add_row("Reader app", config.reader_app or "-")
    table.add_row("Translation profiles", _format_profile_names(config))
    console.print(table)


def _print_settings_menu() -> None:
    console.print(f"{SETTINGS_LANGUAGE_OPTION}. Change default language")
    console.print(f"{SETTINGS_BOOKS_DIR_OPTION}. Change books folder")
    console.print(f"{SETTINGS_FOLDERS_OPTION}. Change feature folder names")
    console.print(f"{SETTINGS_READER_OPTION}. Change reader app")
    console.print(f"{SETTINGS_EXIT_OPTION}. Back")


def _update_guided_settings(store: ConfigStore, current: AyvuConfig, updated: AyvuConfig) -> AyvuConfig:
    if updated == current:
        console.print("Settings unchanged.")
        return current
    effective, persisted = _save_config(store, updated, fallback=current)
    if persisted:
        console.print("[green]Settings saved.[/green]")
    return effective


def _settings_with_default_language(config: AyvuConfig) -> AyvuConfig:
    language = _prompt_target_language_code(config.default_target_language)
    if language == config.default_target_language:
        return config
    return replace(config, default_target_language=language)


def _settings_with_books_dir(config: AyvuConfig) -> AyvuConfig:
    books_dir = _prompt_path("Books folder", config.books_dir)
    if books_dir == config.books_dir:
        return config
    return replace(config, books_dir=books_dir)


def _settings_with_folder_names(config: AyvuConfig) -> AyvuConfig:
    try:
        folders = FolderNames.from_dict(
            {
                "original": _prompt_folder_name("Original folder name", config.folders.original),
                "translated": _prompt_folder_name("Translated folder name", config.folders.translated),
                "preview": _prompt_folder_name("Preview folder name", config.folders.preview),
                "reports": _prompt_folder_name("Reports folder name", config.folders.reports),
                "processing": _prompt_folder_name("Processing folder name", config.folders.processing),
            }
        )
    except ConfigError as exc:
        console.print(f"[red]Invalid folder name:[/red] {exc}")
        return config
    if folders == config.folders:
        return config
    return replace(config, folders=folders)


def _settings_with_reader_app(config: AyvuConfig) -> AyvuConfig:
    reader_app = _prompt_optional_text("Reader app command", config.reader_app)
    if reader_app == config.reader_app:
        return config
    return replace(config, reader_app=reader_app)


def _format_profile_names(config: AyvuConfig) -> str:
    if not config.profiles:
        return "-"
    return ", ".join(sorted(config.profiles))


def _prompt_path(prompt: str, current: Path) -> Path:
    raw_path = typer.prompt(prompt, default=str(current)).strip()
    if not raw_path or raw_path == str(current):
        return current
    return Path(raw_path).expanduser()


def _prompt_folder_name(prompt: str, current: str) -> str:
    value = typer.prompt(prompt, default=current).strip()
    return value or current


def _prompt_optional_text(prompt: str, current: str | None) -> str | None:
    value = typer.prompt(prompt, default=current or "").strip()
    return value or None


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


if __name__ == "__main__":
    app()
