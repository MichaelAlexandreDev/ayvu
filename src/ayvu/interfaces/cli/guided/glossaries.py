"""The guided glossary menu: create, edit and preview managed glossaries."""
from __future__ import annotations


from .. import translate_options
from pathlib import Path
import typer
from rich.table import Table
from .. import report as reports
from ....composition.cli import managed_glossary_repository
from ....knowledge.glossary import Entry as GlossaryEntry, GlossaryReference, list_glossaries, read_glossary, create_glossary, update_glossary

from ..render import console


GLOSSARY_RULE_TRANSLATE = "translate"


GLOSSARY_RULE_PRESERVE = "preserve"


GLOSSARY_CREATE_OPTION = "1"


GLOSSARY_EDIT_OPTION = "2"


GLOSSARY_PREVIEW_OPTION = "3"


GLOSSARY_BACK_OPTION = "0"


GLOSSARY_RULE_TRANSLATE_OPTION = "1"


GLOSSARY_RULE_PRESERVE_OPTION = "2"


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
