"""Book execution adapters, progress displays and checkpoint actions."""
from __future__ import annotations

from pathlib import Path
import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from ...translation import ItemOutcome
from .contracts import ValidationResult
from ...composition.cli import _BookHooks, _BookRequest, _BookOutcome, validate_output, _translate_without_project as execute_translate_without_project, _translate_in_project as execute_translate_in_project
from ...translation import ResumeStateError

from .render import console


class _BatchItemExit(typer.Exit):
    """Presentation exit with the content-free outcome of the active item."""

    def __init__(self, outcome: ItemOutcome) -> None:
        super().__init__(code=1)
        self.outcome = outcome


def _translation_progress_display() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def _validate_with_progress(output_path: Path) -> ValidationResult:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Validando EPUB", total=None)

        def on_progress(index: int, total: int, _name: str) -> None:
            progress.update(
                task,
                total=total,
                completed=index,
                description=f"Validando EPUB {index}/{total}",
            )

        return validate_output(output_path, on_progress=on_progress)


def _translate_without_project(book: _BookRequest, hooks: _BookHooks, *, validate: bool = True) -> _BookOutcome:
    return execute_translate_without_project(book, hooks, validate=validate)


def _translate_in_project(book: _BookRequest, hooks: _BookHooks) -> _BookOutcome:
    return execute_translate_in_project(book, hooks)


def _checkpoint_action(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except (OSError, ResumeStateError) as exc:
        console.print(f"[red]Resume state check failed:[/red] {exc}")
        console.print("Choose a writable processing directory or fix permissions for Documentos/Livros/Processando.")
        raise typer.Exit(code=1) from exc
