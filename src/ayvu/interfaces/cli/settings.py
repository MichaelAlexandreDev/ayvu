"""Where the CLI keeps its files and how it reads the saved configuration, shared by the commands."""
from __future__ import annotations

from pathlib import Path
import typer
from .render import console, _print_expected_error
from ...preferences import DEFAULT_BOOKS_DIR, AyvuConfig, FolderNames, config_from_preferences
from .contracts import ConfigStore
from ...composition.cli import configuration_store, default_processing_dir, select_translator
from ...translation import PreflightError


def _load_existing_config_or_default(store: ConfigStore | None = None) -> AyvuConfig:
    store = store or configuration_store()
    snapshot = store.get_preferences()
    _refuse_invalid_translator_models(snapshot)
    if snapshot.error:
        console.print(f"[yellow]Configuração inválida ignorada:[/yellow] {snapshot.error}")
        console.print("Usando configuração padrão. Ajuste o arquivo de configuração quando possível.")
        return AyvuConfig.default()
    if snapshot.read_only:
        console.print("[yellow]Configuração em somente leitura: campos desconhecidos impedem a gravação.[/yellow]")
    return config_from_preferences(snapshot.preferences, snapshot.revision)


def _refuse_invalid_translator_models(snapshot) -> None:
    if snapshot.error is not None and snapshot.error.field in {'profiles.*.translator', 'profiles.*.model'}:
        console.print("[red]Invalid profile translator or model.[/red]")
        raise typer.Exit(code=1)
    if snapshot.error is not None and snapshot.error.field == "translator_models":
        console.print("[red]Rótulo de modelo inválido em translator_models.[/red]")
        console.print("Use de 1 a 48 caracteres: letras minúsculas, números, _ ou -, começando por letra ou número.")
        console.print("For translator_models.ollama, use the name of an installed model, such as qwen2.5:7b.")
        raise typer.Exit(code=1)
    if snapshot.error is not None and snapshot.error.field == "prices":
        console.print("[red]Invalid price in prices.[/red]")
        console.print('Use prices.<translator>.<model> = {"input": "0.25", "output": "1.25"}, '
                      "in US dollars per million tokens; cached_input is optional.")
        raise typer.Exit(code=1)


def _display_optional_path(path: Path | None) -> str:
    if path is None:
        return "-"
    return str(path)


def _processing_dir(config: AyvuConfig) -> Path:
    if _uses_internal_storage_defaults(config):
        return default_processing_dir()
    return config.processing_dir


def _uses_internal_storage_defaults(config: AyvuConfig) -> bool:
    return config.books_dir == Path(DEFAULT_BOOKS_DIR) and config.folders == FolderNames()


def _select_translator(config, mode, **choices):
    try:
        return select_translator(config, **choices)
    except PreflightError as error:
        _print_expected_error(error.summary, error.next_step, mode, detail=error.detail)
        raise typer.Exit(code=1) from None
