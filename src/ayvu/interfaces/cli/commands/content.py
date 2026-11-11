"""inspect and extract: read an EPUB and show or export its visible content."""
from __future__ import annotations


from pathlib import Path
import typer
from rich.table import Table
from ....composition.cli import open_content_use_cases
from ....kernel import UserMode

from ..render import _BOOK_PROTECTED, _TRANSLATION_FAILURES, _print_content_failure, _print_epub_read_error, console


def inspect(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Show basic information about an EPUB."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        result = open_content_use_cases().inspect_path(epub_path)
    except Exception:
        _print_epub_read_error("inspection_failed", mode)
        raise typer.Exit(code=1) from None
    if result.failure is not None:
        if _TRANSLATION_FAILURES.get(result.failure.code) is _BOOK_PROTECTED:
            # Being protected is the answer to "what is this book", so it is not an error. Its data stays hidden.
            console.print(f"[yellow]{_BOOK_PROTECTED[0]}[/yellow]")
            console.print("Os dados do livro não são exibidos, e o Ayvu não traduz nem remove essa proteção.")
            return
        _print_content_failure(result.failure.code, mode)
        raise typer.Exit(code=1)
    table = Table(title="EPUB information")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Path", str(epub_path))
    table.add_row("Title", result.title or "-")
    table.add_row("Authors", ", ".join(result.authors) if result.authors else "-")
    table.add_row("Language", result.language or "-")
    table.add_row("Documents", str(result.document_count))
    table.add_row("Items", str(result.item_count))
    console.print(table)


def extract(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    output: Path = typer.Option(..., "--output", "-o", help="Directory where Markdown files will be written."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow writing into an existing non-empty directory."),
) -> None:
    """Extract visible text from EPUB documents to Markdown files without translating."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        result = open_content_use_cases().export_path(epub_path, output, overwrite=overwrite)
    except Exception:
        _print_epub_read_error("visible_content_export_failed", mode)
        raise typer.Exit(code=1) from None
    if result.failure is not None:
        if result.failure.code == "output_not_empty":
            console.print(f"[red]Output directory is not empty:[/red] {output}")
            console.print("Use --overwrite to write into it.")
            raise typer.Exit(code=1)
        if result.partial_publication:
            console.print(
                f"[red]Partially extracted {result.completed_count} Markdown files to[/red] {output}; "
                f"{result.failed_count + result.not_started_count} were not published."
            )
        _print_content_failure(result.failure.code, mode)
        raise typer.Exit(code=1)
    console.print(f"[green]Extracted {result.completed_count} Markdown files to[/green] {output}")
