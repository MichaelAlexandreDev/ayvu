"""Translation plans, routes and execution settings shown to the user."""
from __future__ import annotations

from typing import Protocol
from rich.table import Table
from .contracts import TranslationResumeState, SelectedChapter
from ...kernel import LanguagePair, UserMode

from .render import console
from .options import DEFAULT_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF_MAX


class _RouteView(Protocol):
    @property
    def is_direct(self) -> bool: ...

    def describe(self) -> str: ...


def _print_translation_plan(
    language_pair: LanguagePair,
    source_inferred: bool,
    mode: UserMode,
    profile_name: str | None = None,
    profile_style: str | None = None,
    performance_profile_name: str | None = None,
    workers: int = 1,
    requests_per_second: float | None = None,
    retries: int = 2,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    retry_backoff_max: float = DEFAULT_RETRY_BACKOFF_MAX,
    local_sequential: bool = False,
) -> None:
    table = Table(title="Translation plan")
    table.add_column("Field")
    table.add_column("Value")
    source_value = language_pair.source
    if source_inferred and mode == UserMode.DEVELOPER:
        source_value = f"{language_pair.source} (inferido do EPUB)"
    table.add_row("From", source_value)
    table.add_row("To", language_pair.target)
    if profile_name:
        table.add_row("Profile", profile_name)
    if profile_style:
        table.add_row("Profile style", profile_style)
    if performance_profile_name:
        table.add_row("Performance profile", performance_profile_name)
    for label, value in _execution_control_rows(
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    ):
        table.add_row(label, f"{workers} configured; sequential local analysis" if local_sequential and label == "Workers" else value)
    console.print(table)
    if source_inferred and mode == UserMode.COMMON:
        console.print(f"[dim]Idioma de origem detectado do EPUB: {language_pair.source}[/dim]")


def _execution_control_rows(
    *,
    workers: int,
    requests_per_second: float | None,
    retries: int,
    retry_backoff: float,
    retry_backoff_max: float,
) -> tuple[tuple[str, str], ...]:
    return (
        ("Workers", _format_workers(workers)),
        ("Rate limit", _format_rate_limit(requests_per_second, workers=workers)),
        ("Retry policy", _format_retry_policy(retries, retry_backoff, retry_backoff_max)),
    )


def _format_workers(workers: int) -> str:
    if workers == 1:
        return "1 (sequential)"
    return f"{workers} (parallel, reported in EPUB order)"


def _format_rate_limit(requests_per_second: float | None, *, workers: int) -> str:
    if requests_per_second is None:
        return "none"
    suffix = " shared across workers" if workers > 1 else ""
    return f"{_format_number(requests_per_second)} requests/second{suffix}"


def _format_retry_policy(retries: int, retry_backoff: float, retry_backoff_max: float) -> str:
    if retries <= 0:
        return "disabled"
    return (
        f"{retries} retries, {_format_number(retry_backoff)}s initial backoff, "
        f"{_format_number(retry_backoff_max)}s max"
    )


def _format_execution_summary(state: TranslationResumeState) -> str:
    return (
        f"{_format_workers(state.workers)}; "
        f"{_format_rate_limit(state.requests_per_second, workers=state.workers)}; "
        f"{_format_retry_policy(state.retries, state.retry_backoff, state.retry_backoff_max)}"
    )


def _format_number(value: float) -> str:
    return f"{value:g}"


def _print_translation_route(route: _RouteView | None, mode: UserMode) -> None:
    if route is None:
        return
    if mode == UserMode.DEVELOPER:
        console.print(f"[cyan]Route:[/cyan] {route.describe()}")
    if route.is_direct:
        return
    if mode == UserMode.COMMON:
        console.print(f"[yellow]A tradução passará por 2 etapas ({route.describe()}).[/yellow]")
        console.print("[yellow]A qualidade pode ficar comprometida por causa do idioma intermediário.[/yellow]")
    else:
        console.print("[yellow]Rota intermediária em uso. A qualidade pode ficar comprometida.[/yellow]")


def _print_selected_chapters(chapters: list[SelectedChapter]) -> None:
    table = Table(title="Selected chapters")
    table.add_column("#")
    table.add_column("Name")
    table.add_column("Title")
    table.add_column("Archive path")
    for chapter in chapters:
        table.add_row(
            str(chapter.index),
            chapter.name,
            chapter.title or "-",
            chapter.archive_path,
        )
    console.print(table)
