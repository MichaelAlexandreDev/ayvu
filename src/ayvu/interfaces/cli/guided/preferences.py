"""Guided settings: default language, books folder, folder names, reader app and model label."""
from __future__ import annotations
from .language import _prompt_target_language_code
from .. import settings

from dataclasses import replace
from pathlib import Path
import typer
from rich.table import Table
from ....preferences import AyvuConfig, ConfigError, FolderNames, config_from_preferences, is_valid_model_label, MODEL_TRANSLATORS
from ..contracts import ConfigStore

from ..render import console
from ....composition.cli import require_translation_model, discover_translator_models
from ....providers import ProviderSetupError
from ..options import _selected_network_policy
from ....translation import PreflightError


SETTINGS_LANGUAGE_OPTION = "1"


SETTINGS_BOOKS_DIR_OPTION = "2"


SETTINGS_FOLDERS_OPTION = "3"


SETTINGS_READER_OPTION = "4"


SETTINGS_MODEL_LABEL_OPTION = "5"


MODEL_LABEL_TRANSLATOR = "libretranslate"


REMOVE_VALUE = "-"


SETTINGS_EXIT_OPTION = "0"


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


def _run_guided_settings(config: AyvuConfig) -> None:
    store = settings.configuration_store()
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
        if choice == SETTINGS_MODEL_LABEL_OPTION:
            current = _update_guided_settings(store, current, _settings_with_model_label(current))
            continue
        if choice == "6":
            current = _update_guided_settings(store, current, _settings_with_translator_model(current))
            continue
        if choice == SETTINGS_EXIT_OPTION:
            console.print("Settings closed.")
            return

        console.print("[red]Invalid setting.[/red] Choose 1, 2, 3, 4, 5, 6, or 0.")


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
    table.add_row("Translator model label", config.translator_models.get(MODEL_LABEL_TRANSLATOR) or "-")
    table.add_row("Ollama model", config.translator_models.get("ollama") or "-")
    table.add_row("Translation profiles", _format_profile_names(config))
    console.print(table)


def _print_settings_menu() -> None:
    console.print(f"{SETTINGS_LANGUAGE_OPTION}. Change default language")
    console.print(f"{SETTINGS_BOOKS_DIR_OPTION}. Change books folder")
    console.print(f"{SETTINGS_FOLDERS_OPTION}. Change feature folder names")
    console.print(f"{SETTINGS_READER_OPTION}. Change reader app")
    console.print(f"{SETTINGS_MODEL_LABEL_OPTION}. Change translator model label")
    console.print("6. Choose translator and model")
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


def _settings_with_model_label(config: AyvuConfig) -> AyvuConfig:
    """The owner declares the model; Ayvu never invents or copies one from the server."""
    current = config.translator_models.get(MODEL_LABEL_TRANSLATOR)
    console.print("O rótulo identifica o modelo que o seu servidor LibreTranslate usa; ele não é verificado no servidor.")
    console.print(f"Digite {REMOVE_VALUE} para remover o rótulo.")
    value = typer.prompt("Translator model label", default=current or "").strip()
    label = None if value in ("", REMOVE_VALUE) else value
    if value == "" and current is not None:
        label = current
    if label == current:
        return config
    if label is not None and not is_valid_model_label(label):
        console.print("[red]Rótulo de modelo inválido.[/red] Use letras minúsculas, números, - ou _, "
                      "começando por letra ou número, com até 48 caracteres.")
        return config
    # Other translators' entries, such as translator_models.ollama, are kept as they are.
    others = {name: value for name, value in config.translator_models.items() if name != MODEL_LABEL_TRANSLATOR}
    if label is None:
        console.print("Without a model label, new translations are refused.")
        return replace(config, translator_models=others)
    return replace(config, translator_models={**others, MODEL_LABEL_TRANSLATOR: label})


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


def _ensure_guided_model_label(config: AyvuConfig) -> AyvuConfig:
    """Ask in place, using the same validation and persistence as settings option 5."""
    try:
        require_translation_model(config.translator_models, MODEL_LABEL_TRANSLATOR)
        return config
    except PreflightError as error:
        console.print(error.summary)
        console.print(error.next_step)
    console.print("Enter cancels.")
    updated = _settings_with_model_label(config)
    if not updated.translator_models.get(MODEL_LABEL_TRANSLATOR):
        console.print("Canceled.")
        raise typer.Exit(code=1)
    return _update_guided_settings(settings.configuration_store(), config, updated)


def _settings_with_translator_model(config, translator=None):
    name = translator or typer.prompt("Translator (libretranslate or ollama; Enter cancels)", default="").strip().lower()
    if not name:
        return config
    if name not in MODEL_TRANSLATORS:
        console.print("[red]Unknown translator.[/red] Choose libretranslate or ollama.")
        return config
    if name == 'libretranslate':
        return _settings_with_model_label(config)
    try:
        models = discover_translator_models(translator_name=name, network_policy=_selected_network_policy(None))
    except ProviderSetupError:
        console.print("Model discovery failed. Enter an installed model name manually.")
        models = ()
    for index, model in enumerate(models, 1):
        console.print(f"{index}. {model}")
    value = typer.prompt("Ollama model (number or name; Enter cancels)", default="").strip()
    if not value:
        return config
    if value.isdigit() and 1 <= int(value) <= len(models):
        value = models[int(value) - 1]
    if not MODEL_TRANSLATORS[name](value):
        console.print("[red]Invalid translator model.[/red]")
        return config
    return replace(config, translator_models={**config.translator_models, name: value})
