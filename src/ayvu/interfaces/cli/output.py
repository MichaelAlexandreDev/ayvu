"""Output book locations and existing-file decisions."""
from __future__ import annotations

from pathlib import Path
from typing import NoReturn
import typer
from .settings import _uses_internal_storage_defaults
from ...preferences import AyvuConfig
from ...kernel import OutputPlan, UserMode, default_preview_books_dir, default_translated_books_dir

from .render import console


EXISTING_OUTPUT_OVERWRITE_OPTION = "1"


EXISTING_OUTPUT_RENAME_OPTION = "2"


EXISTING_OUTPUT_CANCEL_OPTION = "0"


def _ensure_preview_input_exists(epub_path: Path) -> None:
    if epub_path.is_file():
        return
    console.print(f"[red]Preview EPUB was not found:[/red] {epub_path}")
    console.print("Choose a valid local EPUB file.")
    raise typer.Exit(code=1)


def _print_preview_output_location(output_path: Path, max_documents: int) -> None:
    console.print(f"[yellow]Preview output folder:[/yellow] {output_path.parent}")
    console.print(f"[yellow]Preview EPUB name:[/yellow] {output_path.name}")
    console.print(f"Preview will translate up to {max_documents} EPUB documents.")


def _confirm_default_output_location(output_plan: OutputPlan, input_path: Path, mode: UserMode) -> OutputPlan:
    if output_plan.explicit_output or output_plan.dry_run or mode == UserMode.DEVELOPER:
        return output_plan

    output_path = output_plan.path
    console.print(f"[yellow]Default output folder:[/yellow] {output_path.parent}")
    console.print(f"[yellow]Translated EPUB name:[/yellow] {output_path.name}")
    console.print(_original_epub_location_message(input_path))

    if typer.confirm("Keep this output location?", default=True):
        return output_plan

    return output_plan.with_path(_prompt_output_path(output_path))


def _original_epub_location_message(input_path: Path) -> str:
    if input_path.parent.name == "Original":
        return f"Original EPUB stays in Original: {input_path}"
    return f"Original EPUB remains unchanged: {input_path}"


def _prompt_output_path(default_path: Path) -> Path:
    raw_path = typer.prompt("Output EPUB path", default=str(default_path))
    output_path = Path(raw_path).expanduser()
    if not output_path.name:
        console.print("[red]Canceled:[/red] output path was not changed.")
        raise typer.Exit(code=1)
    if output_path.suffix.lower() != ".epub":
        return output_path.with_suffix(".epub")
    return output_path


def _resolve_existing_output_conflict(output_plan: OutputPlan, overwrite: bool, mode: UserMode) -> OutputPlan:
    if not output_plan.blocks_existing_file(overwrite):
        return output_plan
    if mode == UserMode.DEVELOPER:
        _cancel_existing_output(output_plan.path)

    console.print(f"[yellow]Output path:[/yellow] {output_plan.path}")
    console.print("[yellow]Translated EPUB already exists.[/yellow]")
    action = _prompt_existing_output_action()
    if action == EXISTING_OUTPUT_OVERWRITE_OPTION:
        return output_plan
    if action == EXISTING_OUTPUT_RENAME_OPTION:
        return output_plan.with_path(_prompt_available_output_path(output_plan.path))

    _cancel_existing_output(output_plan.path)


def _prompt_existing_output_action() -> str:
    console.print(f"{EXISTING_OUTPUT_OVERWRITE_OPTION}. Overwrite existing EPUB")
    console.print(f"{EXISTING_OUTPUT_RENAME_OPTION}. Choose another name")
    console.print(f"{EXISTING_OUTPUT_CANCEL_OPTION}. Cancel")

    while True:
        action = typer.prompt("Choose an option", default=EXISTING_OUTPUT_RENAME_OPTION).strip()
        if action in {
            EXISTING_OUTPUT_OVERWRITE_OPTION,
            EXISTING_OUTPUT_RENAME_OPTION,
            EXISTING_OUTPUT_CANCEL_OPTION,
        }:
            return action
        console.print("[red]Invalid option.[/red] Choose 1, 2, or 0.")


def _prompt_available_output_path(default_path: Path) -> Path:
    while True:
        output_path = _prompt_output_path(default_path)
        if not output_path.exists():
            console.print(f"[green]Final output path:[/green] {output_path}")
            return output_path
        console.print(f"[red]Output path already exists:[/red] {output_path}")
        if not typer.confirm("Choose another output path?", default=True):
            _cancel_existing_output(output_path)


def _cancel_existing_output(output_path: Path) -> NoReturn:
    console.print("[red]Canceled:[/red] existing output was not changed.")
    console.print(f"[yellow]Output path:[/yellow] {output_path}")
    console.print("Use --overwrite to replace it or --output to choose another EPUB path.")
    raise typer.Exit(code=1)


def _confirm_existing_preview_overwrite(output_path: Path, mode: UserMode) -> bool:
    if mode == UserMode.DEVELOPER:
        return False

    console.print(f"[yellow]Preview output path:[/yellow] {output_path}")
    console.print("[yellow]Preview EPUB already exists.[/yellow]")
    return typer.confirm("Overwrite existing preview EPUB?", default=False)


def _translated_books_dir(config: AyvuConfig) -> Path:
    if _uses_internal_storage_defaults(config):
        return default_translated_books_dir()
    return config.translated_dir


def _preview_books_dir(config: AyvuConfig) -> Path:
    if _uses_internal_storage_defaults(config):
        return default_preview_books_dir()
    return config.preview_dir
