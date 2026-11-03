from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import NoReturn, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .cache import CacheError, CacheImportReport, CacheSummaryRow, TranslationCache
from .cli_progress import TranslationProgress, TranslationProgressSnapshot
from .config import (
    DEFAULT_BOOKS_DIR,
    AyvuConfig,
    ConfigError,
    ConfigStore,
    FolderNames,
    TranslationProfile,
    default_glossaries_dir,
)
from .domain import (
    ChapterSelection,
    ChapterSelectionError,
    DEFAULT_PERFORMANCE_PROFILE,
    LanguagePair,
    OutputPlan,
    PERFORMANCE_PROFILES,
    PerformanceProfile,
    TranslationMemoryError,
    TranslationMemoryOptions,
    TranslationOptions,
    UserMode,
    default_preview_books_dir,
    default_translated_books_dir,
)
from .epub_io import (
    ReviewApplyReport,
    SelectedChapter,
    TranslationReport,
    apply_reviewed_epub,
    detect_epub_language,
    extract_markdown,
    inspect_epub,
    resolve_chapter_selection,
    translate_epub,
)
from .glossary import (
    GLOSSARY_RULE_PRESERVE,
    GLOSSARY_RULE_TRANSLATE,
    Glossary,
    GlossaryEntry,
    GlossaryError,
    load_glossary,
    save_glossary,
)
from .html_translate import HtmlTranslationStats
from .library import LibraryBook, LibraryOpenError, open_library_epub, scan_library
from .preflight import PreflightError, run_translation_preflight
from .project import ProjectError
from .project_service import resolve_project
from .project_store import ProjectJsonRepository, build_project_request
from .resume import (
    InvalidResumeState,
    ResumeStateError,
    ResumeStateScan,
    ResumeStateStore,
    TranslationResumeState,
    default_processing_dir,
)
from .review_export import ReviewSegment, write_review_csv
from .review_import import ReviewImportError, read_review_csv
from .translator import LibreTranslateTranslator, TranslationRoute, TranslatorError, TranslatorLanguage
from .validation import validate_output_epub


app = typer.Typer(help="Translate local EPUB files with a local HTTP translator.")
cache_app = typer.Typer(help="Inspect, clean, export, and import the translation cache.")
app.add_typer(cache_app, name="cache")
console = Console()
DEFAULT_SOURCE_LANGUAGE = "en"
DEFAULT_TARGET_LANGUAGE = "pt"
DEFAULT_TRANSLATOR_URL = "http://localhost:5000"
DEFAULT_CACHE_PATH = Path(".cache/traducoes.sqlite")
DEFAULT_PREVIEW_DOCUMENT_LIMIT = 12
DEFAULT_RETRY_BACKOFF = 0.5
DEFAULT_RETRY_BACKOFF_MAX = 8.0
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
EXISTING_OUTPUT_OVERWRITE_OPTION = "1"
EXISTING_OUTPUT_RENAME_OPTION = "2"
EXISTING_OUTPUT_CANCEL_OPTION = "0"


@dataclass(frozen=True)
class TranslationRunResult:
    report: TranslationReport
    validation_warnings: list[str]
    markdown_report_path: Path | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class BatchTranslationResult:
    epub_path: Path
    succeeded: bool
    output_path: Path | None = None
    report_path: Path | None = None
    detail: str = ""


@dataclass(frozen=True)
class ExecutionControls:
    performance_profile_name: str
    workers: int
    requests_per_second: float | None
    retries: int
    retry_backoff: float
    retry_backoff_max: float
    chunk_limit: int


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
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
        if ctx.invoked_subcommand is None and not any(v for k, v in ctx.params.items() if k != "mode"):
            mode = UserMode.COMMON
    ctx.ensure_object(dict)
    ctx.obj["mode"] = mode

    if ctx.invoked_subcommand is not None:
        return

    if preview is not None:
        _run_preview(preview, mode=mode)
        return

    config = _load_existing_config_or_default()
    scan = _print_processing_translation_states(ResumeStateStore(_processing_dir(config)))
    if _offer_detected_translation_resume(scan.running, mode=mode):
        return

    if _run_guided_main_flow(ctx, mode=mode):
        return

    console.print(ctx.get_help())


@app.command()
def inspect(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Show basic information about an EPUB."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        info = inspect_epub(epub_path)
    except Exception as exc:
        _print_epub_read_error(str(exc), mode)
        raise typer.Exit(code=1) from exc
    table = Table(title="EPUB information")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Path", str(info.path))
    table.add_row("Title", info.title or "-")
    table.add_row("Authors", ", ".join(info.authors) if info.authors else "-")
    table.add_row("Language", info.language or "-")
    table.add_row("Documents", str(info.document_count))
    table.add_row("Items", str(info.item_count))
    console.print(table)


@app.command("test-translator")
def test_translator(
    ctx: typer.Context,
    url: str = typer.Option(DEFAULT_TRANSLATOR_URL, "--url", help="LibreTranslate base URL or /translate endpoint."),
    source: str = typer.Option(DEFAULT_SOURCE_LANGUAGE, "--source"),
    target: str = typer.Option(DEFAULT_TARGET_LANGUAGE, "--target"),
    timeout: float = typer.Option(10.0, "--timeout"),
    retries: int = typer.Option(1, "--retries"),
    requests_per_second: Optional[float] = typer.Option(
        None,
        "--requests-per-second",
        help="Maximum translator HTTP requests per second. Omit for no rate limit.",
    ),
    retry_backoff: float = typer.Option(
        DEFAULT_RETRY_BACKOFF,
        "--retry-backoff",
        help="Initial retry backoff delay in seconds.",
    ),
    retry_backoff_max: float = typer.Option(
        DEFAULT_RETRY_BACKOFF_MAX,
        "--retry-backoff-max",
        help="Maximum retry backoff delay in seconds.",
    ),
) -> None:
    """Test connectivity with the local translator."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        translator = LibreTranslateTranslator(
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
        )
        translated = translator.translate("Hello world", source, target)
    except TranslatorError as exc:
        _print_expected_error(
            "O teste do tradutor falhou.",
            "Inicie o LibreTranslate, verifique --url e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Translator OK:[/green] Hello world -> {translated}")


@app.command("languages")
def languages(
    ctx: typer.Context,
    url: str = typer.Option(DEFAULT_TRANSLATOR_URL, "--url", help="LibreTranslate base URL or /translate endpoint."),
    timeout: float = typer.Option(10.0, "--timeout"),
    retries: int = typer.Option(1, "--retries"),
    requests_per_second: Optional[float] = typer.Option(
        None,
        "--requests-per-second",
        help="Maximum translator HTTP requests per second. Omit for no rate limit.",
    ),
    retry_backoff: float = typer.Option(
        DEFAULT_RETRY_BACKOFF,
        "--retry-backoff",
        help="Initial retry backoff delay in seconds.",
    ),
    retry_backoff_max: float = typer.Option(
        DEFAULT_RETRY_BACKOFF_MAX,
        "--retry-backoff-max",
        help="Maximum retry backoff delay in seconds.",
    ),
) -> None:
    """List languages reported by the local LibreTranslate server."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        translator = LibreTranslateTranslator(
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
        )
        available_languages = translator.list_languages()
    except TranslatorError as exc:
        _print_expected_error(
            "Não foi possível listar os idiomas.",
            "Inicie o LibreTranslate, verifique --url e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    _print_languages(available_languages)


@cache_app.command("inspect")
def cache_inspect(
    ctx: typer.Context,
    cache_path: Path = typer.Option(DEFAULT_CACHE_PATH, "--cache", help="SQLite cache path."),
    source: Optional[str] = typer.Option(None, "--source", help="Filter by source language."),
    target: Optional[str] = typer.Option(None, "--target", help="Filter by target language."),
    before: Optional[str] = typer.Option(
        None,
        "--before",
        help="Only include entries created before this date (YYYY-MM-DD or ISO datetime).",
    ),
) -> None:
    """Show cached translations grouped by language pair."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    before_value = _normalize_cache_before(before, mode)
    try:
        with TranslationCache(cache_path) as cache:
            rows = cache.summary(source_lang=source, target_lang=target, before=before_value)
    except (CacheError, OSError, sqlite3.Error) as exc:
        _print_expected_error(
            "Não foi possível inspecionar o cache.",
            "Verifique o caminho informado em --cache e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    _print_cache_summary(rows, cache_path)


@cache_app.command("clean")
def cache_clean(
    ctx: typer.Context,
    cache_path: Path = typer.Option(DEFAULT_CACHE_PATH, "--cache", help="SQLite cache path."),
    source: Optional[str] = typer.Option(None, "--source", help="Remove only this source language."),
    target: Optional[str] = typer.Option(None, "--target", help="Remove only this target language."),
    before: Optional[str] = typer.Option(
        None,
        "--before",
        help="Remove only entries created before this date (YYYY-MM-DD or ISO datetime).",
    ),
    all_entries: bool = typer.Option(False, "--all", help="Allow cleaning the whole cache."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show how many entries would be removed."),
    yes: bool = typer.Option(False, "--yes", help="Confirm destructive cleanup."),
) -> None:
    """Remove cache entries by filter or clear the whole cache."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    before_value = _normalize_cache_before(before, mode)
    _validate_cache_clean_options(
        source=source,
        target=target,
        before=before_value,
        all_entries=all_entries,
        dry_run=dry_run,
        yes=yes,
        mode=mode,
    )

    try:
        with TranslationCache(cache_path) as cache:
            if dry_run:
                matched = _count_cache_entries(cache, source=source, target=target, before=before_value)
                console.print(f"[yellow]Dry run:[/yellow] {_plural_cache_entries(matched)} would be removed.")
                return

            report = cache.delete_entries(source_lang=source, target_lang=target, before=before_value)
    except (CacheError, OSError, sqlite3.Error) as exc:
        _print_expected_error(
            "Não foi possível limpar o cache.",
            "Verifique o caminho informado em --cache e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Deleted {_plural_cache_entries(report.deleted)}.[/green]")


@cache_app.command("export")
def cache_export(
    ctx: typer.Context,
    output: Path = typer.Argument(..., dir_okay=False, help="JSON file to write."),
    cache_path: Path = typer.Option(DEFAULT_CACHE_PATH, "--cache", help="SQLite cache path."),
    source: Optional[str] = typer.Option(None, "--source", help="Export only this source language."),
    target: Optional[str] = typer.Option(None, "--target", help="Export only this target language."),
    before: Optional[str] = typer.Option(
        None,
        "--before",
        help="Export only entries created before this date (YYYY-MM-DD or ISO datetime).",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing an existing JSON file."),
) -> None:
    """Export cached translations to readable JSON."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    before_value = _normalize_cache_before(before, mode)
    try:
        with TranslationCache(cache_path) as cache:
            report = cache.export_json(
                output,
                source_lang=source,
                target_lang=target,
                before=before_value,
                overwrite=overwrite,
            )
    except (CacheError, OSError, sqlite3.Error) as exc:
        _print_expected_error(
            "Não foi possível exportar o cache.",
            "Verifique --cache, o caminho de saída e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Cache export saved to:[/green] {report.path}")
    console.print(f"Entries exported: {report.exported}")


@cache_app.command("import")
def cache_import(
    ctx: typer.Context,
    input_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSON export to import.",
    ),
    cache_path: Path = typer.Option(DEFAULT_CACHE_PATH, "--cache", help="SQLite cache path."),
    replace: bool = typer.Option(False, "--replace", help="Replace existing cache entries."),
) -> None:
    """Import cached translations from an Ayvu JSON export."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        with TranslationCache(cache_path) as cache:
            report = cache.import_json(input_path, replace=replace)
    except (CacheError, OSError, sqlite3.Error) as exc:
        _print_expected_error(
            "Não foi possível importar o cache.",
            "Confirme que o arquivo foi exportado pelo Ayvu e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    _print_cache_import_report(report)


@app.command()
def translate(
    ctx: typer.Context,
    epub_paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="One or more input EPUB files.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output EPUB path. Defaults to <input-stem>-<target>.epub.",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Directory for translated EPUBs. Defaults to the translated books folder.",
        file_okay=False,
        dir_okay=True,
    ),
    review_output: Optional[Path] = typer.Option(
        None,
        "--review-output",
        help="Optional CSV path with original and translated segments for external review.",
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help="Source language. If omitted, Ayvu reads the language metadata from the EPUB.",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        help="Target language. Defaults to the profile target or pt.",
    ),
    translator_name: str = typer.Option("libretranslate", "--translator", help="Translator backend."),
    url: str = typer.Option(DEFAULT_TRANSLATOR_URL, "--url", help="Translator base URL."),
    cache_path: Path = typer.Option(DEFAULT_CACHE_PATH, "--cache", help="SQLite cache path."),
    glossary_path: Optional[Path] = typer.Option(None, "--glossary", help="Optional JSON glossary."),
    profile_name: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Translation profile from the Ayvu config.",
    ),
    performance_profile_name: str = typer.Option(
        DEFAULT_PERFORMANCE_PROFILE,
        "--performance-profile",
        help="Performance profile: low, standard, high, or custom.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Process without writing translated EPUB."),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop at the first chapter/text error."),
    chapters: Optional[str] = typer.Option(
        None,
        "--chapters",
        help="Translate only selected chapters, e.g. '1-3,5,*chapter2*'.",
    ),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help="Continue translating remaining EPUBs after a batch item fails.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing an existing output file."),
    timeout: float = typer.Option(30.0, "--timeout", help="Translator HTTP timeout in seconds."),
    retries: Optional[int] = typer.Option(
        None,
        "--retries",
        help="Override the performance profile HTTP retry count.",
    ),
    requests_per_second: Optional[float] = typer.Option(
        None,
        "--requests-per-second",
        help="Override the performance profile maximum translator HTTP requests per second.",
    ),
    retry_backoff: Optional[float] = typer.Option(
        None,
        "--retry-backoff",
        help="Override the performance profile initial retry backoff delay in seconds.",
    ),
    retry_backoff_max: Optional[float] = typer.Option(
        None,
        "--retry-backoff-max",
        help="Override the performance profile maximum retry backoff delay in seconds.",
    ),
    translate_metadata: bool = typer.Option(
        False,
        "--translate-metadata",
        help="Also translate the EPUB title metadata and navigation text.",
    ),
    translate_alt_text: bool = typer.Option(
        False,
        "--translate-alt-text",
        help="Also translate the alt text of images. Text inside images (OCR) is out of scope.",
    ),
    chunk_limit: Optional[int] = typer.Option(
        None,
        "--chunk-limit",
        help="Override the performance profile maximum characters sent per request.",
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        help="Override the performance profile number of parallel document workers.",
    ),
    cache_only: bool = typer.Option(
        False,
        "--cache-only",
        help="Rebuild the EPUB using only cached translations; never call the translator.",
    ),
    require_full_cache: bool = typer.Option(
        False,
        "--require-full-cache",
        help="With --cache-only, only write the EPUB when every text is in the cache.",
    ),
    missing_output: Optional[Path] = typer.Option(
        None,
        "--missing-output",
        help="With --cache-only, path to save the original texts missing from the cache.",
    ),
    translation_memory: bool = typer.Option(
        False,
        "--translation-memory/--no-translation-memory",
        help="Reuse translations of similar text (fuzzy translation memory). Off by default.",
    ),
    tm_apply_threshold: float = typer.Option(
        0.95,
        "--tm-apply-threshold",
        help="With --translation-memory, reuse a stored translation when similarity is at or above this (0-1).",
    ),
    tm_suggest_threshold: float = typer.Option(
        0.80,
        "--tm-suggest-threshold",
        help="With --translation-memory, record a review suggestion when similarity is in [suggest, apply).",
    ),
) -> None:
    """Translate EPUB visible text while preserving EPUB structure."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    config = _load_existing_config_or_default()
    epub_list = list(epub_paths)
    _validate_translate_command_options(
        epub_paths=epub_list,
        output=output,
        output_dir=output_dir,
        review_output=review_output,
        mode=mode,
    )
    _validate_cache_only_options(
        cache_only=cache_only,
        require_full_cache=require_full_cache,
        missing_output=missing_output,
        mode=mode,
    )
    resolved_output_dir = _resolve_translation_output_dir(output_dir, mode=mode)
    chapter_selection = _parse_chapter_selection(chapters, mode=mode)
    tm_options = _build_translation_memory_options(
        enabled=translation_memory,
        apply_threshold=tm_apply_threshold,
        suggest_threshold=tm_suggest_threshold,
        mode=mode,
    )
    execution_controls = _resolve_execution_controls(
        performance_profile_name=performance_profile_name,
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
        chunk_limit=chunk_limit,
        mode=mode,
    )
    _validate_worker_options(
        workers=execution_controls.workers,
        translation_memory=tm_options,
        mode=mode,
    )

    if len(epub_list) > 1:
        _run_batch_translation(
            epub_paths=epub_list,
            output_dir=resolved_output_dir,
            source=source,
            target=target,
            translator_name=translator_name,
            url=url,
            cache_path=cache_path,
            glossary_path=glossary_path,
            profile_name=profile_name,
            performance_profile_name=execution_controls.performance_profile_name,
            dry_run=dry_run,
            fail_fast=fail_fast,
            chapter_selection=chapter_selection,
            continue_on_error=continue_on_error,
            overwrite=overwrite,
            timeout=timeout,
            retries=execution_controls.retries,
            requests_per_second=execution_controls.requests_per_second,
            retry_backoff=execution_controls.retry_backoff,
            retry_backoff_max=execution_controls.retry_backoff_max,
            chunk_limit=execution_controls.chunk_limit,
            workers=execution_controls.workers,
            mode=mode,
            config=config,
            translate_metadata=translate_metadata,
            translate_alt_text=translate_alt_text,
            cache_only=cache_only,
            require_full_cache=require_full_cache,
            translation_memory=tm_options,
        )
        return

    _run_translation(
        epub_path=epub_list[0],
        output=output,
        output_dir=resolved_output_dir,
        review_output=review_output,
        source=source,
        target=target,
        translator_name=translator_name,
        url=url,
        cache_path=cache_path,
        glossary_path=glossary_path,
        profile_name=profile_name,
        performance_profile_name=execution_controls.performance_profile_name,
        dry_run=dry_run,
        fail_fast=fail_fast,
        chapter_selection=chapter_selection,
        overwrite=overwrite,
        timeout=timeout,
        retries=execution_controls.retries,
        requests_per_second=execution_controls.requests_per_second,
        retry_backoff=execution_controls.retry_backoff,
        retry_backoff_max=execution_controls.retry_backoff_max,
        chunk_limit=execution_controls.chunk_limit,
        workers=execution_controls.workers,
        mode=mode,
        config=config,
        translate_metadata=translate_metadata,
        translate_alt_text=translate_alt_text,
        cache_only=cache_only,
        require_full_cache=require_full_cache,
        missing_output=missing_output,
        translation_memory=tm_options,
        confirm_output_location=resolved_output_dir is None,
    )


@app.command()
def resume(
    ctx: typer.Context,
    epub_path: Optional[Path] = typer.Argument(
        None,
        help="EPUB to resume. If omitted, resumes the only translation in progress.",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        help="Target language, to choose when several translations are in progress.",
    ),
) -> None:
    """Resume a translation in progress from its saved checkpoint."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    config = _load_existing_config_or_default()
    store = ResumeStateStore(_processing_dir(config))
    scan = store.scan()

    state = _select_resume_state(scan.running, epub_path, target, mode=mode)
    if state is None:
        raise typer.Exit(code=1)

    console.print(
        f"[green]Resuming translation:[/green] {state.input_path.name} -> {state.output_path.name}"
    )
    _print_resume_checkpoint(state)
    _resume_translation(state, mode=mode)


def _print_expected_error(summary: str, next_step: str, mode: UserMode, detail: str = "") -> None:
    console.print(f"[red]{summary}[/red]")
    if mode == UserMode.DEVELOPER and detail:
        console.print(f"[dim]Detalhe técnico: {detail}[/dim]")
    console.print(next_step)


def _normalize_cache_before(value: str | None, mode: UserMode) -> str | None:
    if value is None:
        return None

    raw_value = value.strip()
    if not raw_value:
        _print_expected_error(
            "Data de cache inválida.",
            "Use --before no formato YYYY-MM-DD ou em formato ISO, como 2026-01-31T10:30:00.",
            mode,
        )
        raise typer.Exit(code=1)

    try:
        if len(raw_value) == 10:
            parsed = datetime.strptime(raw_value, "%Y-%m-%d")
        else:
            parsed = datetime.fromisoformat(raw_value.replace("T", " "))
    except ValueError as exc:
        _print_expected_error(
            "Data de cache inválida.",
            "Use --before no formato YYYY-MM-DD ou em formato ISO, como 2026-01-31T10:30:00.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _validate_cache_clean_options(
    *,
    source: str | None,
    target: str | None,
    before: str | None,
    all_entries: bool,
    dry_run: bool,
    yes: bool,
    mode: UserMode,
) -> None:
    has_filter = bool(source or target or before)
    if all_entries and has_filter:
        _print_expected_error(
            "Opções de limpeza de cache conflitantes.",
            "Use --all sozinho para limpar tudo, ou remova --all e use filtros como --source, --target ou --before.",
            mode,
        )
        raise typer.Exit(code=1)

    if not all_entries and not has_filter:
        _print_expected_error(
            "A limpeza de cache precisa de um escopo explícito.",
            "Informe --source, --target, --before ou use --all para limpar o cache inteiro.",
            mode,
        )
        raise typer.Exit(code=1)

    if dry_run:
        return

    if not yes:
        _print_expected_error(
            "Limpeza de cache não confirmada.",
            "Use --dry-run para pré-visualizar ou adicione --yes para confirmar a remoção.",
            mode,
        )
        raise typer.Exit(code=1)


def _count_cache_entries(
    cache: TranslationCache,
    *,
    source: str | None,
    target: str | None,
    before: str | None,
) -> int:
    rows = cache.summary(source_lang=source, target_lang=target, before=before)
    return sum(row.count for row in rows)


def _print_cache_summary(rows: tuple[CacheSummaryRow, ...], cache_path: Path) -> None:
    console.print(f"[dim]Cache path:[/dim] {cache_path}")
    if not rows:
        console.print("No cache entries found.")
        return

    table = Table(title="Cache summary")
    table.add_column("Source")
    table.add_column("Target")
    table.add_column("Entries")
    table.add_column("First entry")
    table.add_column("Last entry")
    for row in rows:
        table.add_row(
            row.source_lang,
            row.target_lang,
            str(row.count),
            row.first_created_at,
            row.last_created_at,
        )
    console.print(table)
    console.print(f"Total: {_plural_cache_entries(sum(row.count for row in rows))}")


def _print_cache_import_report(report: CacheImportReport) -> None:
    table = Table(title="Cache import", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("Entries read", str(report.total_read))
    table.add_row("Imported", str(report.imported))
    table.add_row("Skipped existing", str(report.skipped))
    table.add_row("Replaced", str(report.replaced))
    console.print(table)


def _plural_cache_entries(count: int) -> str:
    noun = "entry" if count == 1 else "entries"
    return f"{count} cache {noun}"


def _print_epub_read_error(detail: str, mode: UserMode) -> None:
    _print_expected_error(
        "Não foi possível ler o EPUB informado.",
        "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
        mode,
        detail=detail,
    )


def _resolve_source_language(
    explicit_source: str | None,
    epub_path: Path,
    mode: UserMode,
) -> tuple[str, bool]:
    if explicit_source is not None and explicit_source.strip():
        return explicit_source.strip(), False

    try:
        detected = detect_epub_language(epub_path)
    except Exception:
        return DEFAULT_SOURCE_LANGUAGE, True

    if detected is None:
        console.print(
            "[yellow]Idioma do EPUB ausente ou inválido nos metadados.[/yellow] "
            f"Usando '{DEFAULT_SOURCE_LANGUAGE}' como padrão. "
            "Use --source para escolher outro idioma de origem."
        )
        return DEFAULT_SOURCE_LANGUAGE, True

    return detected, True


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
        table.add_row(label, value)
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


def _resolve_execution_controls(
    *,
    performance_profile_name: str,
    workers: int | None,
    requests_per_second: float | None,
    retries: int | None,
    retry_backoff: float | None,
    retry_backoff_max: float | None,
    chunk_limit: int | None,
    mode: UserMode,
) -> ExecutionControls:
    profile = _resolve_performance_profile(performance_profile_name, mode=mode)
    return ExecutionControls(
        performance_profile_name=profile.name,
        workers=workers if workers is not None else profile.workers,
        requests_per_second=(
            requests_per_second
            if requests_per_second is not None
            else profile.requests_per_second
        ),
        retries=retries if retries is not None else profile.retries,
        retry_backoff=retry_backoff if retry_backoff is not None else profile.retry_backoff,
        retry_backoff_max=(
            retry_backoff_max if retry_backoff_max is not None else profile.retry_backoff_max
        ),
        chunk_limit=chunk_limit if chunk_limit is not None else profile.chunk_limit,
    )


def _resolve_performance_profile(name: str, mode: UserMode) -> PerformanceProfile:
    normalized = name.strip().lower() if name else DEFAULT_PERFORMANCE_PROFILE
    profile = PERFORMANCE_PROFILES.get(normalized)
    if profile is not None:
        return profile

    available = ", ".join(sorted(PERFORMANCE_PROFILES))
    _print_expected_error(
        "Perfil de performance não encontrado.",
        "Use --performance-profile com um dos perfis disponíveis, ou remova a opção para usar standard.",
        mode,
        detail=f"Perfil solicitado: {name}. Perfis disponíveis: {available}.",
    )
    raise typer.Exit(code=1)


def _print_translation_route(route: TranslationRoute | None, mode: UserMode) -> None:
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


def _resolve_requested_profile(
    config: AyvuConfig,
    profile_name: str | None,
    mode: UserMode,
) -> tuple[str | None, TranslationProfile | None]:
    if profile_name is None or not profile_name.strip():
        return None, None

    name = profile_name.strip()
    profile = config.profiles.get(name)
    if profile is not None:
        return name, profile

    available = ", ".join(sorted(config.profiles)) or "nenhum perfil configurado"
    _print_expected_error(
        "Perfil de tradução não encontrado.",
        "Use --profile com um perfil definido em config.json, ou remova --profile para rodar sem perfil.",
        mode,
        detail=f"Perfil solicitado: {name}. Perfis disponíveis: {available}.",
    )
    raise typer.Exit(code=1)


def _resolve_target_language(
    explicit_target: str | None,
    profile: TranslationProfile | None,
    default_target: str = DEFAULT_TARGET_LANGUAGE,
) -> str:
    if explicit_target is not None and explicit_target.strip():
        return explicit_target.strip()
    if profile is not None and profile.target_language is not None:
        return profile.target_language
    return default_target


def _resolve_glossary_path(
    explicit_glossary_path: Path | None,
    profile: TranslationProfile | None,
) -> Path | None:
    if explicit_glossary_path is not None:
        return explicit_glossary_path
    if profile is None or profile.glossary is None:
        return None

    profile_glossary = profile.glossary.expanduser()
    if profile_glossary.is_absolute():
        return profile_glossary
    return default_glossaries_dir() / profile_glossary


def _parse_chapter_selection(value: str | None, mode: UserMode) -> ChapterSelection | None:
    if value is None:
        return None
    try:
        return ChapterSelection.parse(value)
    except ChapterSelectionError as exc:
        _print_expected_error(
            "Seleção de capítulos inválida.",
            "Use índices 1-based, faixas e padrões separados por vírgula, por exemplo --chapters '1-3,5,*chapter2*'.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc


def _resolve_chapter_selection_for_display(
    epub_path: Path,
    selection: ChapterSelection,
    options: TranslationOptions,
    mode: UserMode,
) -> list[SelectedChapter]:
    try:
        return resolve_chapter_selection(
            epub_path,
            selection,
            translate_metadata=options.translate_metadata,
            max_documents=options.max_documents,
        )
    except ChapterSelectionError as exc:
        _print_expected_error(
            "Nenhum capítulo corresponde à seleção informada.",
            "Confira --chapters e use índices 1-based, faixas ou padrões que existam no EPUB.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        _print_epub_read_error(str(exc), mode)
        raise typer.Exit(code=1) from exc


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


def _validate_translate_command_options(
    epub_paths: list[Path],
    output: Path | None,
    output_dir: Path | None,
    review_output: Path | None,
    mode: UserMode,
) -> None:
    if not epub_paths:
        _print_expected_error(
            "Nenhum EPUB informado para tradução.",
            "Informe pelo menos um arquivo .epub no comando translate.",
            mode,
        )
        raise typer.Exit(code=1)

    if output is not None and output_dir is not None:
        _print_expected_error(
            "Opções de saída incompatíveis.",
            "Use --output para um EPUB específico ou --output-dir para uma pasta de saída, não ambos.",
            mode,
        )
        raise typer.Exit(code=1)

    if len(epub_paths) == 1:
        return

    if output is not None:
        _print_expected_error(
            "Não é possível usar --output com múltiplos EPUBs.",
            "Use --output-dir para escolher a pasta do batch, ou traduza um EPUB por vez com --output.",
            mode,
        )
        raise typer.Exit(code=1)

    if review_output is not None:
        _print_expected_error(
            "Não é possível usar --review-output com múltiplos EPUBs.",
            "Gere arquivos de revisão traduzindo um EPUB por vez.",
            mode,
        )
        raise typer.Exit(code=1)


def _validate_cache_only_options(
    cache_only: bool,
    require_full_cache: bool,
    missing_output: Path | None,
    mode: UserMode,
) -> None:
    if cache_only:
        return

    if require_full_cache:
        _print_expected_error(
            "--require-full-cache exige --cache-only.",
            "Adicione --cache-only para reconstruir apenas com o cache, ou remova --require-full-cache.",
            mode,
        )
        raise typer.Exit(code=1)

    if missing_output is not None:
        _print_expected_error(
            "--missing-output exige --cache-only.",
            "Adicione --cache-only para listar os textos ausentes do cache, ou remova --missing-output.",
            mode,
        )
        raise typer.Exit(code=1)


def _build_translation_memory_options(
    enabled: bool,
    apply_threshold: float,
    suggest_threshold: float,
    mode: UserMode,
) -> TranslationMemoryOptions | None:
    if not enabled:
        return None

    options = TranslationMemoryOptions(
        apply_threshold=apply_threshold,
        suggest_threshold=suggest_threshold,
    )
    try:
        options.validate()
    except TranslationMemoryError as exc:
        _print_expected_error(
            "Configuração inválida da memória de tradução.",
            "Use limiares entre 0 e 1, com a sugestão menor ou igual à aplicação.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc
    return options


def _validate_worker_options(
    workers: int,
    translation_memory: TranslationMemoryOptions | None,
    mode: UserMode,
) -> None:
    if workers < 1:
        _print_expected_error(
            "Quantidade de workers inválida.",
            "Use --workers com valor 1 ou maior.",
            mode,
            detail=f"--workers {workers}",
        )
        raise typer.Exit(code=1)

    if workers > 1 and translation_memory is not None:
        _print_expected_error(
            "--workers é incompatível com --translation-memory nesta versão.",
            "Use --workers 1 com --translation-memory, ou remova --translation-memory para executar em paralelo.",
            mode,
        )
        raise typer.Exit(code=1)


def _resolve_translation_output_dir(output_dir: Path | None, mode: UserMode) -> Path | None:
    if output_dir is None:
        return None

    path = output_dir.expanduser()
    if path.exists() and not path.is_dir():
        _print_expected_error(
            "A pasta de saída informada não é um diretório.",
            "Escolha um diretório em --output-dir ou remova a opção para usar o padrão.",
            mode,
            detail=f"Output directory: {path}",
        )
        raise typer.Exit(code=1)
    return path


def _run_translation(
    epub_path: Path,
    output: Path | None,
    review_output: Path | None,
    source: str | None,
    target: str | None,
    translator_name: str,
    url: str,
    cache_path: Path,
    glossary_path: Path | None,
    profile_name: str | None,
    performance_profile_name: str | None,
    dry_run: bool,
    fail_fast: bool,
    chapter_selection: ChapterSelection | None,
    overwrite: bool,
    timeout: float,
    retries: int,
    requests_per_second: float | None,
    retry_backoff: float,
    retry_backoff_max: float,
    chunk_limit: int,
    workers: int,
    mode: UserMode,
    config: AyvuConfig | None = None,
    translate_metadata: bool = False,
    translate_alt_text: bool = False,
    cache_only: bool = False,
    require_full_cache: bool = False,
    missing_output: Path | None = None,
    translation_memory: TranslationMemoryOptions | None = None,
    output_dir: Path | None = None,
    auto_save_markdown_report: bool = False,
    report_dir: Path | None = None,
    confirm_output_location: bool = True,
    resolve_managed_project: bool = True,
) -> TranslationRunResult:
    config = config or _load_existing_config_or_default()
    resolved_profile_name, profile = _resolve_requested_profile(config, profile_name, mode=mode)
    resolved_target = _resolve_target_language(target, profile)
    resolved_glossary_path = _resolve_glossary_path(glossary_path, profile)
    resolved_source, source_inferred = _resolve_source_language(source, epub_path, mode=mode)
    language_pair = LanguagePair(source=resolved_source, target=resolved_target)
    _print_translation_plan(
        language_pair,
        source_inferred=source_inferred,
        mode=mode,
        profile_name=resolved_profile_name,
        profile_style=profile.style if profile else None,
        performance_profile_name=performance_profile_name,
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    )
    translation_options = TranslationOptions(
        language_pair=language_pair,
        dry_run=dry_run,
        fail_fast=fail_fast,
        chunk_limit=chunk_limit,
        workers=workers,
        translate_metadata=translate_metadata,
        translate_alt_text=translate_alt_text,
        chapter_selection=chapter_selection,
        cache_only=cache_only,
        require_full_cache=require_full_cache,
        translation_memory=translation_memory,
    )
    if chapter_selection is not None:
        selected_chapters = _resolve_chapter_selection_for_display(
            epub_path=epub_path,
            selection=chapter_selection,
            options=translation_options,
            mode=mode,
        )
        _print_selected_chapters(selected_chapters)
    output_plan = OutputPlan.for_translation(
        epub_path,
        output,
        language_pair,
        dry_run=dry_run,
        default_dir=output_dir or _translated_books_dir(config),
    )
    if confirm_output_location:
        output_plan = _confirm_default_output_location(output_plan, epub_path, mode=mode)
    output_plan = _resolve_existing_output_conflict(output_plan, overwrite=overwrite, mode=mode)
    output_path = output_plan.path
    review_output_path = _resolve_review_output_path(
        review_output,
        dry_run=dry_run,
        overwrite=overwrite,
        mode=mode,
    )

    project_id = None
    if resolve_managed_project and not dry_run:
        try:
            project_request = build_project_request(
                epub_path,
                source=language_pair.source,
                target=language_pair.target,
                glossary_path=resolved_glossary_path,
                profile_name=resolved_profile_name,
                selections={
                    "translator": translator_name,
                    "url": url,
                    "chunk_limit": chunk_limit,
                    "translate_metadata": translate_metadata,
                    "translate_alt_text": translate_alt_text,
                    "chapters": chapter_selection.source if chapter_selection else None,
                    "translation_memory": (
                        [translation_memory.apply_threshold, translation_memory.suggest_threshold,
                         translation_memory.max_candidates]
                        if translation_memory is not None else None
                    ),
                },
            )
            resolution = resolve_project(ProjectJsonRepository(_processing_dir(config)), project_request)
            project_id = resolution.project.project_id
        except ProjectError as exc:
            next_step = {
                "source_missing": "Informe uma fonte existente; o projeto anterior foi preservado.",
                "source_changed": "A fonte foi alterada. Restaure a revisão original ou escolha explicitamente outra fonte para um novo projeto.",
                "ambiguous_project": "Há projetos compatíveis duplicados. Preserve os manifests e resolva a duplicação antes de repetir.",
                "incomplete_project_creation": "Com todas as execuções encerradas, preserve a pasta .staging-* movendo-a para fora de projects e repita o comando.",
                "project_commit_uncertain": "O manifesto pode já estar publicado. Recarregue o estado ou repita a tradução para reutilizar o projeto.",
                "credential_bearing_endpoint": "Use uma URL do tradutor sem usuário, senha, parâmetros de consulta ou fragmento.",
            }.get(str(exc),
                "Verifique a fonte, as permissões da pasta de processamento e o estado do projeto; "
                "preserve os arquivos existentes antes de corrigir conflitos.")
            _print_expected_error(
                "Não foi possível resolver o projeto local.",
                next_step,
                mode,
                detail=str(exc),
            )
            raise typer.Exit(code=1) from None

    try:
        preflight = run_translation_preflight(
            epub_path=epub_path,
            cache_path=cache_path,
            glossary_path=resolved_glossary_path,
            translator_name=translator_name,
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
            language_pair=language_pair,
            dry_run=dry_run,
            cache_only=cache_only,
        )
    except PreflightError as exc:
        _print_expected_error(exc.summary, exc.next_step, mode, detail=exc.detail)
        raise typer.Exit(code=1) from exc

    _print_translation_route(preflight.route, mode=mode)

    resume_store: ResumeStateStore | None = None
    resume_state: TranslationResumeState | None = None
    if not dry_run and not cache_only:
        resume_store, resume_state = _save_running_resume_state(
            epub_path=epub_path,
            output_path=output_path,
            cache_path=cache_path,
            translator_name=translator_name,
            url=url,
            glossary_path=resolved_glossary_path,
            options=translation_options,
            overwrite=overwrite,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
            processing_dir=_processing_dir(config),
        )

    progress_view: TranslationProgress | None = None
    review_segments: list[ReviewSegment] | None = [] if review_output_path is not None else None
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            progress_view = TranslationProgress(progress, dry_run=dry_run, workers=workers)

            def on_chapter_start(index: int, total: int, name: str) -> None:
                nonlocal resume_state
                progress_view.chapter_started(index, total, name)
                if resume_store and resume_state:
                    resume_state = resume_state.start_chapter(name, total)
                    _save_resume_state(resume_store, resume_state)

            def on_chapter_done(index: int, total: int, name: str, stats: HtmlTranslationStats) -> None:
                nonlocal resume_state
                progress_view.chapter_done(index, total, name, stats)
                if resume_store and resume_state:
                    ok = not stats.errors and stats.missing == 0
                    failed = len(stats.errors) + stats.missing
                    resume_state = resume_state.record_chapter(
                        name=name,
                        total=total,
                        ok=ok,
                        failed_segment_count=failed,
                    )
                    _save_resume_state(resume_store, resume_state)

            with TranslationCache(cache_path) as cache:
                report = translate_epub(
                    epub_path,
                    output_path,
                    translator=preflight.translator,
                    cache=cache,
                    options=translation_options,
                    glossary=preflight.glossary,
                    on_chapter_start=on_chapter_start,
                    on_chapter_done=on_chapter_done,
                    on_text_processed=progress_view.text_processed,
                    review_segments=review_segments,
                    translator_factory=getattr(preflight, "translator_factory", None),
                    cache_factory=lambda: TranslationCache(cache_path),
                )
    except KeyboardInterrupt as exc:
        _print_interrupted_translation(
            snapshot=progress_view.snapshot() if progress_view else None,
            output_path=output_path,
            cache_path=cache_path,
            dry_run=dry_run,
            workers=workers,
            requests_per_second=requests_per_second,
            retries=retries,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
        )
        raise typer.Exit(code=1) from exc

    if review_output_path is not None and review_segments is not None:
        _write_review_output(review_output_path, review_segments, overwrite=overwrite, mode=mode)
        report.review_output_path = review_output_path

    if cache_only and report.missing_texts:
        _save_missing_texts(
            report,
            missing_output=missing_output,
            overwrite=overwrite,
            directory=report_dir,
            mode=mode,
        )

    validation = (
        _validate_with_progress(output_path) if report.output_written and not dry_run else None
    )
    validation_warnings = validation.warnings if validation else []

    _print_report(report, dry_run, validation_warnings)
    markdown_report_path = None
    if auto_save_markdown_report:
        markdown_report_path = _save_markdown_report(
            report,
            dry_run,
            validation_warnings,
            directory=report_dir,
        )
        console.print(f"[green]Report saved to:[/green] {markdown_report_path}")
    else:
        _offer_markdown_report(report, dry_run, validation_warnings, mode=mode)

    if cache_only and require_full_cache and not report.output_written:
        _print_cache_coverage_blocked(report, mode)
        raise typer.Exit(code=1)

    if validation is not None:
        if validation.ok:
            console.print(
                f"[green]Validação OK:[/green] {validation.document_count} documentos XHTML/HTML encontrados."
            )
            if resume_store and resume_state:
                _mark_resume_state_completed(resume_store, resume_state)
        else:
            raise typer.Exit(code=1)

    return TranslationRunResult(
        report=report,
        validation_warnings=validation_warnings,
        markdown_report_path=markdown_report_path,
        project_id=project_id,
    )


def _run_batch_translation(
    epub_paths: list[Path],
    output_dir: Path | None,
    source: str | None,
    target: str | None,
    translator_name: str,
    url: str,
    cache_path: Path,
    glossary_path: Path | None,
    profile_name: str | None,
    performance_profile_name: str | None,
    dry_run: bool,
    fail_fast: bool,
    chapter_selection: ChapterSelection | None,
    continue_on_error: bool,
    overwrite: bool,
    timeout: float,
    retries: int,
    requests_per_second: float | None,
    retry_backoff: float,
    retry_backoff_max: float,
    chunk_limit: int,
    workers: int,
    mode: UserMode,
    config: AyvuConfig,
    translate_metadata: bool = False,
    translate_alt_text: bool = False,
    cache_only: bool = False,
    require_full_cache: bool = False,
    translation_memory: TranslationMemoryOptions | None = None,
) -> None:
    resolved_output_dir = output_dir or _translated_books_dir(config)
    report_dir = _reports_dir(config)
    _print_batch_plan(
        epub_paths=epub_paths,
        output_dir=resolved_output_dir,
        report_dir=report_dir,
        continue_on_error=continue_on_error,
        performance_profile_name=performance_profile_name,
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    )

    results: list[BatchTranslationResult] = []
    total = len(epub_paths)
    for index, epub_path in enumerate(epub_paths, start=1):
        console.print(f"[cyan]Batch item {index}/{total}:[/cyan] {epub_path}")
        try:
            run_result = _run_translation(
                epub_path=epub_path,
                output=None,
                output_dir=resolved_output_dir,
                review_output=None,
                source=source,
                target=target,
                translator_name=translator_name,
                url=url,
                cache_path=cache_path,
                glossary_path=glossary_path,
                profile_name=profile_name,
                performance_profile_name=performance_profile_name,
                dry_run=dry_run,
                fail_fast=fail_fast,
                chapter_selection=chapter_selection,
                overwrite=overwrite,
                timeout=timeout,
                retries=retries,
                requests_per_second=requests_per_second,
                retry_backoff=retry_backoff,
                retry_backoff_max=retry_backoff_max,
                chunk_limit=chunk_limit,
                workers=workers,
                mode=mode,
                config=config,
                translate_metadata=translate_metadata,
                translate_alt_text=translate_alt_text,
                cache_only=cache_only,
                require_full_cache=require_full_cache,
                translation_memory=translation_memory,
                auto_save_markdown_report=True,
                report_dir=report_dir,
                confirm_output_location=False,
            )
        except typer.Exit as exc:
            if isinstance(exc.__cause__, KeyboardInterrupt):
                raise
            results.append(
                BatchTranslationResult(
                    epub_path=epub_path,
                    succeeded=False,
                    detail=f"exit code {exc.exit_code or 1}",
                )
            )
            console.print(f"[red]Batch item failed:[/red] {epub_path}")
            if continue_on_error:
                continue
            _print_batch_summary(results)
            raise typer.Exit(code=exc.exit_code or 1) from exc
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            _print_expected_error(
                "Falha inesperada ao traduzir este EPUB.",
                "Verifique o EPUB, o tradutor local e as opções usadas antes de tentar novamente.",
                mode,
                detail=detail,
            )
            results.append(BatchTranslationResult(epub_path=epub_path, succeeded=False, detail=detail))
            if continue_on_error:
                continue
            _print_batch_summary(results)
            raise typer.Exit(code=1) from exc

        results.append(
            BatchTranslationResult(
                epub_path=epub_path,
                succeeded=True,
                output_path=run_result.report.output_path,
                report_path=run_result.markdown_report_path,
            )
        )

    _print_batch_summary(results)
    if any(not result.succeeded for result in results):
        raise typer.Exit(code=1)


def _print_batch_plan(
    epub_paths: list[Path],
    output_dir: Path,
    report_dir: Path,
    continue_on_error: bool,
    performance_profile_name: str | None,
    workers: int,
    requests_per_second: float | None,
    retries: int,
    retry_backoff: float,
    retry_backoff_max: float,
) -> None:
    table = Table(title="Batch translation plan")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("EPUB files", str(len(epub_paths)))
    table.add_row("Output folder", str(output_dir))
    table.add_row("Reports folder", str(report_dir))
    table.add_row("Continue on error", "yes" if continue_on_error else "no")
    if performance_profile_name:
        table.add_row("Performance profile", performance_profile_name)
    for label, value in _execution_control_rows(
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    ):
        table.add_row(label, value)
    console.print(table)


def _print_batch_summary(results: list[BatchTranslationResult]) -> None:
    table = Table(title="Batch translation summary")
    table.add_column("EPUB")
    table.add_column("Status")
    table.add_column("Output or detail")
    table.add_column("Report")
    for result in results:
        status = "[green]OK[/green]" if result.succeeded else "[red]Failed[/red]"
        detail = _display_optional_path(result.output_path) if result.succeeded else result.detail
        table.add_row(
            str(result.epub_path),
            status,
            detail or "-",
            _display_optional_path(result.report_path),
        )
    console.print(table)


def _run_preview(
    epub_path: Path,
    source: str = DEFAULT_SOURCE_LANGUAGE,
    target: str = DEFAULT_TARGET_LANGUAGE,
    translator_name: str = "libretranslate",
    url: str = DEFAULT_TRANSLATOR_URL,
    cache_path: Path = DEFAULT_CACHE_PATH,
    glossary_path: Path | None = None,
    timeout: float = 30.0,
    retries: int = 2,
    requests_per_second: float | None = None,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    retry_backoff_max: float = DEFAULT_RETRY_BACKOFF_MAX,
    chunk_limit: int = 3000,
    max_documents: int = DEFAULT_PREVIEW_DOCUMENT_LIMIT,
    mode: UserMode = UserMode.DEVELOPER,
    config: AyvuConfig | None = None,
) -> None:
    config = config or _load_existing_config_or_default()
    epub_path = epub_path.expanduser()
    _ensure_preview_input_exists(epub_path)

    language_pair = LanguagePair(source=source, target=target)
    translation_options = TranslationOptions(
        language_pair=language_pair,
        chunk_limit=chunk_limit,
        max_documents=max_documents,
    )
    output_plan = OutputPlan.for_preview(
        epub_path,
        default_dir=_preview_books_dir(config),
    )
    output_path = output_plan.path
    _print_preview_output_location(output_path, max_documents)

    if output_plan.blocks_existing_file(overwrite=False):
        if not _confirm_existing_preview_overwrite(output_path, mode=mode):
            console.print("[red]Canceled:[/red] existing preview was not changed.")
            raise typer.Exit(code=1)

    try:
        preflight = run_translation_preflight(
            epub_path=epub_path,
            cache_path=cache_path,
            glossary_path=glossary_path,
            translator_name=translator_name,
            url=url,
            timeout=timeout,
            retries=retries,
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
            language_pair=language_pair,
            dry_run=False,
        )
    except PreflightError as exc:
        _print_expected_error(exc.summary, exc.next_step, mode, detail=exc.detail)
        raise typer.Exit(code=1) from exc

    _print_translation_route(preflight.route, mode=mode)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress_view = TranslationProgress(progress, dry_run=False)
        with TranslationCache(cache_path) as cache:
            report = translate_epub(
                epub_path,
                output_path,
                translator=preflight.translator,
                cache=cache,
                options=translation_options,
                glossary=preflight.glossary,
                on_chapter_start=progress_view.chapter_started,
                on_chapter_done=progress_view.chapter_done,
                on_text_processed=progress_view.text_processed,
            )

    validation = _validate_with_progress(output_path)
    _print_report(report, dry_run=False, validation_warnings=validation.warnings)
    if validation.ok:
        console.print(f"[green]Preview salvo em:[/green] {output_path}")
        console.print(
            f"[green]Validação OK:[/green] {validation.document_count} documentos XHTML/HTML encontrados."
        )
        return

    raise typer.Exit(code=1)


@app.command()
def extract(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    output: Path = typer.Option(..., "--output", "-o", help="Directory where Markdown files will be written."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow writing into an existing non-empty directory."),
) -> None:
    """Extract visible text from EPUB documents to Markdown files without translating."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    if output.exists() and any(output.iterdir()) and not overwrite:
        console.print(f"[red]Output directory is not empty:[/red] {output}")
        console.print("Use --overwrite to write into it.")
        raise typer.Exit(code=1)
    try:
        written = extract_markdown(epub_path, output)
    except Exception as exc:
        _print_epub_read_error(str(exc), mode)
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Extracted {len(written)} Markdown files to[/green] {output}")


@app.command("apply-review")
def apply_review(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Original EPUB to rebuild from.",
    ),
    review_csv: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Reviewed CSV exported by Ayvu.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output EPUB path. Defaults to <input-stem>-<target>-reviewed.epub.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing an existing output file."),
) -> None:
    """Rebuild a translated EPUB from a reviewed CSV, preserving the original file."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        review = read_review_csv(review_csv)
    except ReviewImportError as exc:
        _print_expected_error(
            "Não foi possível ler o arquivo de revisão.",
            "Confirme que é um CSV de revisão gerado pelo Ayvu e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    if not review.rows:
        _print_expected_error(
            "O arquivo de revisão não tem segmentos.",
            "Gere o CSV com `ayvu translate --review-output` e revise antes de importar.",
            mode,
        )
        raise typer.Exit(code=1)

    output_path = _resolve_apply_review_output(epub_path, output, review.target_language)
    if output_path.exists() and not overwrite:
        _print_expected_error(
            "Arquivo de saída já existe.",
            "Use --overwrite para substituí-lo ou escolha outro caminho em --output.",
            mode,
            detail=f"Output: {output_path}",
        )
        raise typer.Exit(code=1)

    try:
        report = apply_reviewed_epub(epub_path, output_path, review)
    except Exception as exc:
        _print_epub_read_error(str(exc), mode)
        raise typer.Exit(code=1) from exc

    validation = _validate_with_progress(output_path)
    _print_apply_review_report(report, validation.warnings)


def _resolve_apply_review_output(epub_path: Path, output: Path | None, target_language: str) -> Path:
    if output is not None:
        return output.expanduser()
    suffix = f"-{target_language}" if target_language else ""
    return epub_path.with_name(f"{epub_path.stem}{suffix}-reviewed.epub")


def _print_apply_review_report(report: ReviewApplyReport, validation_warnings: list[str]) -> None:
    table = Table(title="Importação de revisão", show_header=False)
    table.add_column("Campo", style="bold")
    table.add_column("Valor")
    table.add_row("Input", str(report.input_path) if report.input_path else "-")
    table.add_row("Output", str(report.output_path) if report.output_path else "-")
    table.add_row("Languages", f"{report.source_language or '-'} → {report.target_language or '-'}")
    table.add_row("Segments applied", str(report.applied))
    table.add_row("Without review", str(report.untranslated))
    table.add_row("Missing in EPUB", str(len(report.missing_in_epub)))
    table.add_row("Inconsistent", str(len(report.inconsistent)))
    table.add_row("Duplicated ids", str(len(report.duplicated)))
    table.add_row("Unknown documents", str(len(report.unknown_documents)))
    table.add_row("Invalid ids", str(len(report.invalid_ids)))
    console.print(table)

    _print_apply_review_warnings(report)
    if validation_warnings:
        _print_validation_warnings(validation_warnings)
    console.print(f"[green]Reviewed EPUB saved to:[/green] {report.output_path}")


def _print_apply_review_warnings(report: ReviewApplyReport) -> None:
    sections = (
        ("Segments missing in the EPUB", report.missing_in_epub),
        ("Inconsistent segments (original changed)", report.inconsistent),
        ("Duplicated segment ids", report.duplicated),
        ("Unknown documents", report.unknown_documents),
        ("Invalid segment ids", report.invalid_ids),
    )
    for title, items in sections:
        if not items:
            continue
        console.print(f"[yellow]{title}:[/yellow]")
        for item in items[:10]:
            console.print(f"  - {item}")
        if len(items) > 10:
            console.print(f"  - ... (+{len(items) - 10})")
    if report.untranslated:
        console.print(
            f"[yellow]{report.untranslated} segment(s) had no reviewed translation "
            "and stayed in the source language.[/yellow]"
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

        return validate_output_epub(output_path, on_progress=on_progress)


def _print_validation_warnings(warnings: list[str]) -> None:
    console.print("[yellow]Avisos de validação:[/yellow]")
    for warning in warnings:
        console.print(f"  - {warning}")


def _print_report(
    report: TranslationReport,
    dry_run: bool,
    validation_warnings: list[str] | None = None,
) -> None:
    validation_warnings = validation_warnings or []
    table = Table(title="Translation report")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Original EPUB", _display_optional_path(report.input_path))
    table.add_row("Detected language", report.detected_language or "-")
    table.add_row("Translated language", report.target_language or "-")
    table.add_row("Output", _report_output_value(report, dry_run))
    if report.review_output_path is not None:
        table.add_row("Review file", str(report.review_output_path))
    table.add_row("Chapters processed", str(report.chapters_processed))
    table.add_row("Texts translated", str(report.texts_translated))
    table.add_row("Texts from cache", str(report.texts_from_cache))
    if report.texts_from_memory:
        table.add_row("Texts from memory", str(report.texts_from_memory))
    if report.memory_suggestions:
        table.add_row("Memory suggestions", str(report.memory_suggestions))
    if report.texts_missing:
        table.add_row("Texts missing", str(report.texts_missing))
    if report.alt_texts_translated:
        table.add_row("Alt texts translated", str(report.alt_texts_translated))
    table.add_row("Errors", str(len(report.errors)))
    table.add_row("Validation warnings", str(len(validation_warnings)))
    if _has_glossary_summary(report):
        table.add_row("Glossary terms configured", str(report.glossary_terms_configured))
        table.add_row("Glossary terms applied", str(report.glossary_usage.total_applied))
        table.add_row(
            "Required glossary terms missing",
            str(len(report.glossary_usage.required_terms_missing)),
        )
        table.add_row(
            "Forbidden glossary terms found",
            str(report.glossary_usage.total_forbidden_found),
        )
    console.print(table)

    for error in report.errors:
        console.print(f"[yellow]Error:[/yellow] {error}")

    _print_glossary_warnings(report)

    if validation_warnings:
        _print_validation_warnings(validation_warnings)


def _has_glossary_summary(report: TranslationReport) -> bool:
    return report.glossary_terms_configured > 0 or report.glossary_usage.has_activity


def _print_glossary_warnings(report: TranslationReport) -> None:
    usage = report.glossary_usage
    if not usage.required_terms_missing and not usage.forbidden_terms_found:
        return

    console.print("[yellow]Glossary warnings:[/yellow]")
    if usage.required_terms_missing:
        console.print(f"  - Required terms missing: {_format_terms(usage.required_terms_missing)}")
    if usage.forbidden_terms_found:
        console.print(f"  - Forbidden terms found: {_format_counted_terms(usage.forbidden_terms_found)}")


def _resolve_review_output_path(
    review_output: Path | None,
    dry_run: bool,
    overwrite: bool,
    mode: UserMode,
) -> Path | None:
    if review_output is None:
        return None

    if dry_run:
        _print_expected_error(
            "Não é possível gerar arquivo de revisão em dry-run.",
            "Remova --dry-run para gerar traduções revisáveis, ou remova --review-output.",
            mode,
        )
        raise typer.Exit(code=1)

    path = review_output.expanduser()
    if path.exists() and not overwrite:
        _print_expected_error(
            "Arquivo de revisão já existe.",
            "Use --overwrite para substituí-lo ou escolha outro caminho em --review-output.",
            mode,
            detail=f"Review output: {path}",
        )
        raise typer.Exit(code=1)

    return path


def _write_review_output(
    path: Path,
    segments: list[ReviewSegment],
    overwrite: bool,
    mode: UserMode,
) -> None:
    try:
        write_review_csv(path, segments, overwrite=overwrite)
    except OSError as exc:
        _print_expected_error(
            "Não foi possível salvar o arquivo de revisão.",
            "Escolha um caminho gravável para --review-output e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Review file saved to:[/green] {path}")


def _print_interrupted_translation(
    snapshot: TranslationProgressSnapshot | None,
    output_path: Path,
    cache_path: Path,
    dry_run: bool,
    workers: int,
    requests_per_second: float | None,
    retries: int,
    retry_backoff: float,
    retry_backoff_max: float,
) -> None:
    console.print("[yellow]Translation interrupted by user.[/yellow]")
    console.print("Cached translations saved before the interruption can be reused with the same --cache path.")
    console.print(f"Cache path: {cache_path}")
    console.print(_interrupted_output_message(output_path, dry_run))
    if not dry_run:
        console.print("Run 'ayvu resume' to continue this translation from the saved checkpoint.")

    if snapshot is None:
        return

    table = Table(title="Partial translation progress")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Chapters processed", _partial_chapter_value(snapshot))
    table.add_row("Texts processed", str(snapshot.texts_processed))
    table.add_row("Texts translated", str(snapshot.texts_translated))
    table.add_row("Texts from cache", str(snapshot.texts_from_cache))
    if snapshot.texts_from_memory:
        table.add_row("Texts from memory", str(snapshot.texts_from_memory))
    table.add_row("Texts dry-run", str(snapshot.texts_dry_run))
    table.add_row("Texts missing", str(snapshot.texts_missing))
    table.add_row("Text errors", str(snapshot.text_errors))
    table.add_row("Current chapter", snapshot.current_chapter or "-")
    for label, value in _execution_control_rows(
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    ):
        table.add_row(label, value)
    console.print(table)


def _partial_chapter_value(snapshot: TranslationProgressSnapshot) -> str:
    if snapshot.total_chapters is None:
        return str(snapshot.chapters_processed)
    return f"{snapshot.chapters_processed}/{snapshot.total_chapters}"


def _interrupted_output_message(output_path: Path, dry_run: bool) -> str:
    if dry_run:
        return "Dry run interrupted; no translated EPUB was expected."
    if output_path.exists():
        return f"Translated EPUB may be incomplete: {output_path}"
    return f"Translated EPUB was not written: {output_path}"


def _print_processing_translation_states(store: ResumeStateStore) -> ResumeStateScan:
    scan = store.scan()
    if not scan.has_findings:
        return scan

    if scan.running:
        _print_running_resume_states(scan.running)
    if scan.invalid:
        _print_invalid_resume_states(scan.invalid)
    return scan


def _run_guided_main_flow(ctx: typer.Context, mode: UserMode) -> bool:
    if mode == UserMode.DEVELOPER:
        return False

    config = _load_or_init_config()
    _print_guided_main_menu()
    choice = typer.prompt("Choose an option", default=GUIDED_PREVIEW_OPTION).strip()
    return _handle_guided_main_choice(choice, ctx, config)


def _load_or_init_config(store: ConfigStore | None = None) -> AyvuConfig:
    store = store or ConfigStore.default()
    if not store.path.exists():
        return _init_default_language_config(store)

    try:
        return store.load()
    except ConfigError as exc:
        console.print(f"[yellow]Configuração inválida ignorada:[/yellow] {exc}")
        console.print("Usando configuração padrão. Ajuste o arquivo de configuração se quiser alterar o idioma.")
        return AyvuConfig.default()


def _load_existing_config_or_default(store: ConfigStore | None = None) -> AyvuConfig:
    store = store or ConfigStore.default()
    try:
        return store.load()
    except ConfigError as exc:
        console.print(f"[yellow]Configuração inválida ignorada:[/yellow] {exc}")
        console.print("Usando configuração padrão. Ajuste o arquivo de configuração quando possível.")
        return AyvuConfig.default()


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
    )
    if _save_config(store, config):
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


def _save_config(store: ConfigStore, config: AyvuConfig) -> bool:
    try:
        store.save(config)
        return True
    except ConfigError as exc:
        console.print(f"[yellow]Não foi possível salvar a configuração:[/yellow] {exc}")
        console.print("A mudança será usada nesta execução, mas não foi persistida.")
        return False


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
        _resolve_target_language(None, profile, default_target=config.default_target_language)
    )
    glossary_path = _choose_guided_profile_glossary(profile) or _choose_guided_translation_glossary()
    _run_translation(
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
    _run_preview(epub_path, target=target, mode=UserMode.COMMON, config=config)


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
    glossary_path = _resolve_glossary_path(None, profile)
    if glossary_path is None:
        return None
    console.print(f"[green]Profile glossary:[/green] {glossary_path}")
    return glossary_path


def _run_guided_glossaries() -> None:
    directory = default_glossaries_dir()
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
    path = _prompt_new_glossary_path(directory)
    if path is None:
        return None

    glossary = _prompt_guided_glossary_terms(Glossary())
    return _save_guided_glossary(path, glossary)


def _edit_guided_glossary(directory: Path) -> Path | None:
    files = _list_glossary_files(directory)
    if not files:
        console.print("[yellow]No saved glossaries found.[/yellow]")
        return None

    path = _prompt_saved_glossary_path(files)
    if path is None:
        return None

    glossary = _load_guided_glossary(path)
    if glossary is None:
        return None

    updated = _prompt_guided_glossary_terms(glossary)
    return _save_guided_glossary(path, updated)


def _preview_guided_glossary(directory: Path) -> None:
    files = _list_glossary_files(directory)
    if not files:
        console.print("[yellow]No saved glossaries found.[/yellow]")
        return

    path = _prompt_saved_glossary_path(files)
    if path is None:
        return

    glossary = _load_guided_glossary(path)
    if glossary is None:
        return
    _print_guided_glossary_preview(glossary)


def _choose_guided_translation_glossary() -> Path | None:
    files = _list_glossary_files(default_glossaries_dir())
    if not files:
        return None

    console.print("[yellow]Saved glossaries are available.[/yellow]")
    while True:
        path = _prompt_saved_glossary_path(files, back_label="Run without glossary")
        if path is None:
            return None
        if _load_guided_glossary(path) is not None:
            console.print(f"[green]Glossary selected:[/green] {path}")
            return path
        console.print("Choose another glossary or run without glossary.")


def _prompt_new_glossary_path(directory: Path) -> Path | None:
    while True:
        raw_name = typer.prompt("Glossary name", default="glossary").strip()
        stem = _safe_filename_part(Path(raw_name).stem or raw_name).lower()
        path = directory / f"{stem}.json"
        if not path.exists():
            return path

        console.print(f"[yellow]Glossary already exists:[/yellow] {path}")
        if not typer.confirm("Choose another glossary name?", default=True):
            console.print("Glossary was not created.")
            return None


def _prompt_guided_glossary_terms(glossary: Glossary) -> Glossary:
    entries = list(glossary.entries)
    if entries:
        _print_guided_glossary_preview(Glossary(entries))

    while True:
        term = typer.prompt("Original term (leave empty to finish)", default="").strip()
        if not term:
            if entries:
                return Glossary(entries)
            console.print("[yellow]No glossary terms were added.[/yellow]")
            return Glossary()

        entry = _prompt_guided_glossary_entry(term)
        replaced = _upsert_glossary_entry(entries, entry)
        if replaced:
            console.print(f"[green]Term updated:[/green] {entry.term}")
        else:
            console.print(f"[green]Term added:[/green] {entry.term}")
        _print_guided_glossary_preview(Glossary(entries))

        if not typer.confirm("Add another term?", default=True):
            return Glossary(entries)


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


def _save_guided_glossary(path: Path, glossary: Glossary) -> Path | None:
    if not glossary.entries:
        console.print("[yellow]Glossary was not saved because it has no terms.[/yellow]")
        return None

    _print_guided_glossary_preview(glossary)
    if not typer.confirm("Save this glossary?", default=True):
        console.print("Glossary was not saved.")
        return None

    try:
        saved_path = save_glossary(path, glossary)
    except GlossaryError as exc:
        console.print(f"[red]Glossary validation failed:[/red] {exc}")
        return None

    console.print(f"[green]Glossary saved:[/green] {saved_path}")
    return saved_path


def _load_guided_glossary(path: Path) -> Glossary | None:
    try:
        return load_glossary(path)
    except GlossaryError as exc:
        console.print(f"[red]Could not load glossary:[/red] {exc}")
        return None


def _prompt_saved_glossary_path(files: tuple[Path, ...], back_label: str = "Back") -> Path | None:
    _print_saved_glossaries(files)
    console.print(f"{GLOSSARY_BACK_OPTION}. {back_label}")

    while True:
        choice = typer.prompt("Choose glossary", default=GLOSSARY_BACK_OPTION).strip()
        if choice == GLOSSARY_BACK_OPTION:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(files):
            return files[int(choice) - 1]
        console.print(f"[red]Invalid glossary.[/red] Choose 1-{len(files)} or 0.")


def _print_saved_glossaries(files: tuple[Path, ...]) -> None:
    table = Table(title="Saved glossaries")
    table.add_column("Option")
    table.add_column("Name")
    table.add_column("Terms")
    for index, path in enumerate(files, start=1):
        table.add_row(str(index), path.stem, _glossary_term_count(path))
    console.print(table)


def _glossary_term_count(path: Path) -> str:
    glossary = _load_guided_glossary(path)
    if glossary is None:
        return "invalid"
    return str(len(glossary.entries))


def _print_guided_glossary_preview(glossary: Glossary) -> None:
    if not glossary.entries:
        console.print("[yellow]Glossary has no terms yet.[/yellow]")
        return

    table = Table(title="Glossary preview")
    table.add_column("Term")
    table.add_column("Rule")
    table.add_column("Translation")
    for entry in glossary.entries:
        table.add_row(entry.term, entry.rule, entry.translation or "-")
    console.print(table)


def _list_glossary_files(directory: Path) -> tuple[Path, ...]:
    if not directory.exists():
        return ()
    try:
        return tuple(sorted(path for path in directory.glob("*.json") if path.is_file()))
    except OSError as exc:
        console.print(f"[yellow]Could not list glossaries:[/yellow] {exc}")
        return ()


def _run_guided_library(config: AyvuConfig) -> None:
    books = scan_library(config)
    if not books:
        console.print("[yellow]Library has no EPUB books yet.[/yellow]")
        console.print(f"Original folder: {config.original_dir}")
        console.print(f"Translated folder: {config.translated_dir}")
        return

    _print_library_books(books)
    book = _prompt_library_book(books)
    if book is None:
        return
    _run_guided_library_book(config, book)


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


def _run_guided_library_book(config: AyvuConfig, book: LibraryBook) -> None:
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

        label, path = actions[int(choice) - 1]
        if path is None:
            _print_library_book_info(book)
            continue
        _open_library_book_path(path, config, label)
        return


def _library_book_actions(book: LibraryBook) -> list[tuple[str, Path | None]]:
    actions: list[tuple[str, Path | None]] = []
    if book.original_path:
        actions.append(("Open original", book.original_path))
    for translation in book.translations:
        actions.append((f"Open {translation.label}", translation.path))
    actions.append(("Show information", None))
    return actions


def _print_library_book_actions(book: LibraryBook, actions: list[tuple[str, Path | None]]) -> None:
    table = Table(title=book.name)
    table.add_column("Option")
    table.add_column("Action")
    for index, action in enumerate(actions, start=1):
        table.add_row(str(index), action[0])
    console.print(table)
    console.print(f"{LIBRARY_BACK_OPTION}. Back")


def _print_library_book_info(book: LibraryBook) -> None:
    table = Table(title="Book information")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Book", book.name)
    table.add_row("Original EPUB", str(book.original_path) if book.original_path else "-")
    table.add_row("Translations", _display_book_translations(book))
    table.add_row("Translation files", _display_translation_paths(book))
    console.print(table)


def _open_library_book_path(path: Path, config: AyvuConfig, label: str) -> None:
    try:
        open_library_epub(path, reader_app=config.reader_app)
    except LibraryOpenError as exc:
        console.print(f"[red]Could not open EPUB:[/red] {exc}")
        console.print("Configure Reader app in Settings or install a default EPUB reader.")
        return

    console.print(f"[green]{label}:[/green] {path}")


def _display_book_translations(book: LibraryBook) -> str:
    if not book.translations:
        return "-"
    return ", ".join(translation.label for translation in book.translations)


def _display_translation_paths(book: LibraryBook) -> str:
    if not book.translations:
        return "-"
    return "\n".join(str(translation.path) for translation in book.translations)


def _run_guided_settings(config: AyvuConfig) -> None:
    store = ConfigStore.default()
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
    if _save_config(store, updated):
        console.print("[green]Settings saved.[/green]")
    return updated


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


def _print_guided_language_choices(languages: tuple[TranslatorLanguage, ...]) -> None:
    table = Table(title="LibreTranslate languages")
    table.add_column("Option")
    table.add_column("Language")
    table.add_column("Code")
    table.add_column("State")

    for index, language in enumerate(languages, start=1):
        table.add_row(str(index), language.name, language.code, language.state)
    console.print(table)


def _language_code_from_guided_choice(choice: str, languages: tuple[TranslatorLanguage, ...]) -> str | None:
    if not choice.isdigit():
        return None

    index = int(choice)
    if 1 <= index <= len(languages):
        return languages[index - 1].code
    return None


def _load_languages_for_guided_selection() -> tuple[TranslatorLanguage, ...]:
    translator = LibreTranslateTranslator(url=DEFAULT_TRANSLATOR_URL, timeout=10.0, retries=1)
    try:
        return translator.list_languages()
    except TranslatorError as exc:
        console.print(f"[yellow]Could not list LibreTranslate languages:[/yellow] {exc}")
        return ()


def _print_languages(languages: tuple[TranslatorLanguage, ...]) -> None:
    table = Table(title="LibreTranslate languages")
    table.add_column("Language")
    table.add_column("Code")
    table.add_column("State")
    table.add_column("Targets")

    for language in languages:
        table.add_row(
            language.name,
            language.code,
            language.state,
            _display_language_targets(language.targets),
        )
    console.print(table)


def _display_language_targets(targets: tuple[str, ...]) -> str:
    if not targets:
        return "-"
    if len(targets) <= 8:
        return ", ".join(targets)
    first_targets = ", ".join(targets[:8])
    return f"{first_targets} (+{len(targets) - 8})"


def _offer_detected_translation_resume(states: tuple[TranslationResumeState, ...], mode: UserMode) -> bool:
    if not states:
        return False
    # In DEVELOPER mode, we don't resume automatically to avoid unexpected behavior.
    # The user should probably use a resume-specific command if we had one,
    # or just let the cache handle it.
    if mode == UserMode.DEVELOPER:
        return False

    if len(states) > 1:
        console.print("Multiple translations are in progress; automatic selection is not available yet.")
        return False

    state = states[0]
    _print_resume_checkpoint(state)
    if not typer.confirm("Continue detected translation?", default=False):
        console.print("Detected translation was not resumed. Processing files were left unchanged.")
        return False

    console.print(f"[green]Resuming translation:[/green] {state.input_path.name} -> {state.output_path.name}")
    _resume_translation(state, mode=mode)
    return True


def _resume_translation(state: TranslationResumeState, mode: UserMode) -> None:
    try:
        _run_translation(
            epub_path=state.input_path,
            output=state.output_path,
            review_output=None,
            source=state.source,
            target=state.target,
            translator_name=state.translator_name,
            url=state.url,
            cache_path=state.cache_path,
            glossary_path=state.glossary_path,
            profile_name=None,
            performance_profile_name=None,
            dry_run=False,
            fail_fast=state.fail_fast,
            chapter_selection=_parse_chapter_selection(state.chapter_selection, mode=mode),
            overwrite=True,
            timeout=state.timeout,
            retries=state.retries,
            requests_per_second=state.requests_per_second,
            retry_backoff=state.retry_backoff,
            retry_backoff_max=state.retry_backoff_max,
            chunk_limit=state.chunk_limit,
            workers=state.workers,
            mode=mode,
            translate_metadata=state.translate_metadata,
            translate_alt_text=state.translate_alt_text,
            translation_memory=state.translation_memory_options(),
            resolve_managed_project=False,
        )
    except typer.Exit:
        console.print(
            "Não foi possível retomar a tradução detectada. Verifique a mensagem acima e reinicie a tradução se necessário."
        )
        raise


def _select_resume_state(
    running: tuple[TranslationResumeState, ...],
    epub_path: Path | None,
    target: str | None,
    mode: UserMode,
) -> TranslationResumeState | None:
    candidates = list(running)
    if epub_path is not None:
        resolved = epub_path.expanduser().resolve()
        candidates = [state for state in candidates if state.input_path == resolved]
    if target is not None:
        candidates = [state for state in candidates if state.target == target]

    if not candidates:
        _print_expected_error(
            "No translation in progress was found to resume.",
            "Start a translation with 'ayvu translate <epub>' or check the processing folder.",
            mode,
        )
        return None

    if len(candidates) == 1:
        return candidates[0]

    _print_running_resume_states(tuple(candidates))
    _print_expected_error(
        "Several translations in progress match the request.",
        "Specify the EPUB and --target to choose which one to resume.",
        mode,
    )
    return None


def _print_resume_checkpoint(state: TranslationResumeState) -> None:
    table = Table(title="Resume checkpoint")
    table.add_column("Metric")
    table.add_column("Value")
    if _has_checkpoint_progress(state):
        table.add_row("Chapters completed", _checkpoint_chapter_value(state))
        if state.current_chapter:
            table.add_row("Current chapter", state.current_chapter)
        if state.failed_chapters:
            table.add_row("Chapters with failures", str(len(state.failed_chapters)))
            table.add_row("Failed segments", str(state.failed_segment_count))
    for label, value in _execution_control_rows(
        workers=state.workers,
        requests_per_second=state.requests_per_second,
        retries=state.retries,
        retry_backoff=state.retry_backoff,
        retry_backoff_max=state.retry_backoff_max,
    ):
        table.add_row(label, value)
    table.add_row("Last update", state.updated_at)
    console.print(table)
    console.print(
        "Saved execution settings and cached translations are reused; only segments still missing from the cache are retried."
    )


def _has_checkpoint_progress(state: TranslationResumeState) -> bool:
    return bool(
        state.completed_chapters
        or state.failed_chapters
        or state.current_chapter
        or state.total_chapters
    )


def _checkpoint_chapter_value(state: TranslationResumeState) -> str:
    done = len(state.completed_chapters)
    if state.total_chapters is None:
        return str(done)
    return f"{done}/{state.total_chapters}"


def _print_running_resume_states(states: tuple[TranslationResumeState, ...]) -> None:
    console.print("[yellow]Translations in progress were found.[/yellow]")
    table = Table(title="Processing translations")
    table.add_column("Original EPUB")
    table.add_column("Output")
    table.add_column("Target")
    table.add_column("Cache")
    table.add_column("Execution")
    for state in states:
        table.add_row(
            state.input_path.name,
            state.output_path.name,
            state.target,
            state.cache_path.name,
            _format_execution_summary(state),
        )
    console.print(table)


def _print_invalid_resume_states(states: tuple[InvalidResumeState, ...]) -> None:
    console.print("[yellow]Invalid processing state files were found.[/yellow]")
    table = Table(title="Invalid processing states")
    table.add_column("State file")
    table.add_column("Problem")
    for state in states:
        table.add_row(state.path.name, _single_line(state.message))
    console.print(table)
    console.print("Restart the translation if the state file cannot be fixed.")


def _save_running_resume_state(
    epub_path: Path,
    output_path: Path,
    cache_path: Path,
    translator_name: str,
    url: str,
    glossary_path: Path | None,
    options: TranslationOptions,
    overwrite: bool,
    timeout: float,
    retries: int,
    requests_per_second: float | None,
    retry_backoff: float,
    retry_backoff_max: float,
    processing_dir: Path,
) -> tuple[ResumeStateStore, TranslationResumeState]:
    store = ResumeStateStore(processing_dir)
    state = TranslationResumeState.create(
        input_path=epub_path,
        output_path=output_path,
        cache_path=cache_path,
        translator_name=translator_name,
        url=url,
        glossary_path=glossary_path,
        options=options,
        overwrite=overwrite,
        timeout=timeout,
        retries=retries,
        requests_per_second=requests_per_second,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    )
    _save_resume_state(store, state)
    return store, state


def _mark_resume_state_completed(store: ResumeStateStore, state: TranslationResumeState) -> None:
    _save_resume_state(store, state.mark_completed())


def _save_resume_state(store: ResumeStateStore, state: TranslationResumeState) -> None:
    try:
        store.save(state)
    except (OSError, ResumeStateError) as exc:
        console.print(f"[red]Resume state check failed:[/red] {exc}")
        console.print(
            "Choose a writable processing directory or fix permissions for Documentos/Livros/Processando."
        )
        raise typer.Exit(code=1) from exc


def _offer_markdown_report(
    report: TranslationReport,
    dry_run: bool,
    validation_warnings: list[str] | None = None,
    mode: UserMode = UserMode.DEVELOPER,
) -> None:
    if mode == UserMode.DEVELOPER:
        return

    if not typer.confirm("Save translation report as Markdown?", default=False):
        return

    path = _save_markdown_report(report, dry_run, validation_warnings)
    console.print(f"[green]Report saved to:[/green] {path}")


def _save_markdown_report(
    report: TranslationReport,
    dry_run: bool,
    validation_warnings: list[str] | None = None,
    directory: Path | None = None,
) -> Path:
    directory = directory or _default_reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _next_available_report_path(directory, _report_filename_stem(report))
    path.write_text(_render_markdown_report(report, dry_run, validation_warnings), encoding="utf-8")
    return path


def _save_missing_texts(
    report: TranslationReport,
    missing_output: Path | None,
    overwrite: bool,
    directory: Path | None,
    mode: UserMode,
) -> None:
    if missing_output is not None:
        path = missing_output.expanduser()
        if path.exists() and not overwrite:
            _print_expected_error(
                "Arquivo de textos ausentes já existe.",
                "Use --overwrite para substituí-lo ou escolha outro caminho em --missing-output.",
                mode,
                detail=f"Missing output: {path}",
            )
            raise typer.Exit(code=1)
    else:
        directory = directory or _default_reports_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _next_available_report_path(directory, _missing_filename_stem(report), suffix=".txt")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_missing_texts(report), encoding="utf-8")
    except OSError as exc:
        _print_expected_error(
            "Não foi possível salvar o arquivo de textos ausentes.",
            "Escolha um caminho gravável para --missing-output e tente novamente.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Missing texts saved to:[/green] {path}")


def _render_missing_texts(report: TranslationReport) -> str:
    lines = [
        "# Ayvu cache-only missing texts",
        f"# Original EPUB: {_display_optional_path(report.input_path)}",
        f"# Source language: {report.detected_language or '-'}",
        f"# Target language: {report.target_language or '-'}",
        f"# Missing texts: {len(report.missing_texts)}",
        "",
    ]
    for index, text in enumerate(report.missing_texts, start=1):
        lines.append(f"[{index}]")
        lines.append(text)
        lines.append("")
    return "\n".join(lines) + "\n"


def _missing_filename_stem(report: TranslationReport) -> str:
    source = _safe_filename_part(report.input_path.stem if report.input_path else "translation")
    target = _safe_filename_part(report.target_language or "translated")
    return f"{source}-{target}-missing"


def _print_cache_coverage_blocked(report: TranslationReport, mode: UserMode) -> None:
    _print_expected_error(
        f"Cache incompleto: {report.texts_missing} texto(s) sem tradução no cache.",
        (
            "Remova --require-full-cache para gerar mesmo assim, traduza o EPUB normalmente "
            "para preencher o cache, ou consulte o arquivo de textos ausentes."
        ),
        mode,
    )


def _render_markdown_report(
    report: TranslationReport,
    dry_run: bool,
    validation_warnings: list[str] | None = None,
) -> str:
    validation_warnings = validation_warnings or []
    lines = [
        "# Translation report",
        "",
        f"- Original EPUB: {_display_optional_path(report.input_path)}",
        f"- Detected language: {report.detected_language or '-'}",
        f"- Translated language: {report.target_language or '-'}",
        f"- Output: {_report_output_value(report, dry_run)}",
        f"- Chapters processed: {report.chapters_processed}",
        f"- Texts translated: {report.texts_translated}",
        f"- Texts from cache: {report.texts_from_cache}",
    ]
    if report.review_output_path is not None:
        lines.insert(6, f"- Review file: {report.review_output_path}")
    if report.texts_from_memory:
        lines.append(f"- Texts from memory: {report.texts_from_memory}")
    if report.memory_suggestions:
        lines.append(f"- Memory suggestions: {report.memory_suggestions}")
    if report.texts_missing:
        lines.append(f"- Texts missing: {report.texts_missing}")
    if report.alt_texts_translated:
        lines.append(f"- Alt texts translated: {report.alt_texts_translated}")
    lines.extend([
        f"- Errors: {len(report.errors)}",
        f"- Validation warnings: {len(validation_warnings)}",
    ])
    if _has_glossary_summary(report):
        lines.extend(
            [
                f"- Glossary terms configured: {report.glossary_terms_configured}",
                f"- Glossary terms applied: {report.glossary_usage.total_applied}",
                f"- Required glossary terms missing: {len(report.glossary_usage.required_terms_missing)}",
                f"- Forbidden glossary terms found: {report.glossary_usage.total_forbidden_found}",
            ]
        )

    if report.errors:
        lines.extend(["", "## Errors"])
        lines.extend(f"- {_single_line(error)}" for error in report.errors)

    if report.glossary_usage.applied_terms:
        lines.extend(["", "## Glossary usage"])
        lines.extend(
            f"- {_single_line(term)}: {count}"
            for term, count in sorted(report.glossary_usage.applied_terms.items())
        )

    if report.glossary_usage.required_terms_missing or report.glossary_usage.forbidden_terms_found:
        lines.extend(["", "## Glossary warnings"])
        if report.glossary_usage.required_terms_missing:
            lines.append(
                f"- Required terms missing: {_format_terms(report.glossary_usage.required_terms_missing)}"
            )
        if report.glossary_usage.forbidden_terms_found:
            lines.append(
                f"- Forbidden terms found: {_format_counted_terms(report.glossary_usage.forbidden_terms_found)}"
            )

    if validation_warnings:
        lines.extend(["", "## Validation warnings"])
        lines.extend(f"- {_single_line(warning)}" for warning in validation_warnings)

    return "\n".join(lines) + "\n"


def _default_reports_dir() -> Path:
    return _reports_dir(_load_existing_config_or_default())


def _next_available_report_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    path = directory / f"{stem}{suffix}"
    index = 2
    while path.exists():
        path = directory / f"{stem}-{index}{suffix}"
        index += 1
    return path


def _report_filename_stem(report: TranslationReport) -> str:
    source = _safe_filename_part(report.input_path.stem if report.input_path else "translation")
    target = _safe_filename_part(report.target_language or "translated")
    return f"{source}-{target}-report"


def _safe_filename_part(value: str) -> str:
    clean = []
    for char in value.strip():
        if char.isalnum() or char in ("-", "_"):
            clean.append(char)
            continue
        if char in (" ", "."):
            clean.append("-")

    filename = "".join(clean).strip("-_")
    return filename or "translation"


def _display_optional_path(path: Path | None) -> str:
    if path is None:
        return "-"
    return str(path)


def _report_output_value(report: TranslationReport, dry_run: bool) -> str:
    if dry_run:
        return "(dry run, no file written)"
    if not report.output_written:
        return "(cache incompleto, nenhum arquivo gerado)"
    return _display_optional_path(report.output_path)


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


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _format_terms(terms: list[str]) -> str:
    return ", ".join(_single_line(term) for term in terms) if terms else "-"


def _format_counted_terms(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{_single_line(term)} ({count})" for term, count in sorted(counts.items()))


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


def _reports_dir(config: AyvuConfig) -> Path:
    if _uses_internal_storage_defaults(config):
        return Path.home() / "Documentos" / "Livros" / "Relatorios"
    return config.reports_dir


def _processing_dir(config: AyvuConfig) -> Path:
    if _uses_internal_storage_defaults(config):
        return default_processing_dir()
    return config.processing_dir


def _uses_internal_storage_defaults(config: AyvuConfig) -> bool:
    return config.books_dir == Path(DEFAULT_BOOKS_DIR) and config.folders == FolderNames()


if __name__ == "__main__":
    app()
