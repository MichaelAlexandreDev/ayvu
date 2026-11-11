"""The guided library: list the books folder and open a book or its translations."""
from __future__ import annotations

from collections import Counter
import typer
from rich.table import Table
from ....preferences import AyvuConfig
from ....composition.cli import library_service
from ....library import Book as LibraryBook, LibraryService, ScanRequest, ListRequest, DescribeRequest, OpenRequest

from ..render import console


LIBRARY_BACK_OPTION = "0"


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
