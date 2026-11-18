"""Execute a limited book preview and validate its output."""
from __future__ import annotations
from . import plan, run as translation_run, run_messages, run_support, settings

from pathlib import Path
import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from . import report as reports, output as output_paths
from .progress import TranslationProgress
from ...preferences import AyvuConfig
from ...composition.cli import _BookHooks, _BookRequest, _BookOutcome, check_book, prepare_preview
from ...kernel import LanguagePair, OutputPlan, TranslationOptions, UserMode
from ...translation import BookRefused, PreflightError
from ...network import NetworkPolicy

from .render import _print_expected_error, _print_network_selection, console
from .options import DEFAULT_CACHE_PATH, DEFAULT_PREVIEW_DOCUMENT_LIMIT, DEFAULT_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF_MAX, DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE, DEFAULT_TRANSLATOR_URL, _selected_network_policy


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
    network_policy: NetworkPolicy | None = None,
) -> None:
    config = config or settings._load_existing_config_or_default()
    selected = settings._select_translator(config, mode, translator=translator_name, url=url, preview=True)
    config, translator_name, url = selected.config, selected.translator, selected.url
    epub_path = epub_path.expanduser()
    output_paths._ensure_preview_input_exists(epub_path)

    language_pair = LanguagePair(source=source, target=target)
    translation_options = TranslationOptions(
        language_pair=language_pair,
        chunk_limit=chunk_limit,
        max_documents=max_documents,
    )
    output_plan = OutputPlan.for_preview(
        epub_path,
        default_dir=output_paths._preview_books_dir(config),
    )
    output_path = output_plan.path
    output_paths._print_preview_output_location(output_path, max_documents)

    if output_plan.blocks_existing_file(overwrite=False):
        if not output_paths._confirm_existing_preview_overwrite(output_path, mode=mode):
            console.print("[red]Canceled:[/red] existing preview was not changed.")
            raise typer.Exit(code=1)

    try:
        check_book(epub_path)
    except BookRefused as exc:
        run_messages._print_translation_failure(_BookOutcome(failure=exc.code), output_path, mode)
        raise typer.Exit(code=1) from exc

    try:
        prepare = _prepare_preview(config, mode, translation_options,
            network_policy=_selected_network_policy(network_policy), epub_path=epub_path,
            cache_path=cache_path, glossary_path=glossary_path, translator_name=translator_name,
            url=url, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            language_pair=language_pair, dry_run=False)
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console) as progress:
            progress_view = TranslationProgress(progress, dry_run=False)
            outcome = run_support._translate_without_project(
                _BookRequest(epub_path, output_plan, True, translation_options, cache_path, prepare,
                    translator_name=translator_name, translator_models=config.translator_models, preview=True),
                _BookHooks(progress_view.chapter_started, progress_view.chapter_done,
                           progress_view.text_processed, lambda *args: None), validate=False)
            report = outcome.report
    except PreflightError as exc:
        _print_expected_error(exc.summary, exc.next_step, mode, detail=exc.detail)
        raise typer.Exit(code=1) from exc

    validation = run_support._validate_with_progress(output_path)
    report.provider_name, report.model_name = translator_name, config.translator_models.get(translator_name)
    reports._print_report(report, dry_run=False, validation_warnings=validation.warnings)
    if validation.ok:
        console.print(f"[green]Preview salvo em:[/green] {output_path}")
        console.print(
            f"[green]Validação OK:[/green] {validation.document_count} documentos XHTML/HTML encontrados."
        )
        return

    raise typer.Exit(code=1)


def _prepare_preview(config, mode, options, **configuration):
    def on_route(prepared):
        plan._print_translation_route(prepared.route, mode=mode)
        _print_network_selection(getattr(prepared, "network_decision", None))

    return prepare_preview(translator_models=config.translator_models, options=options, defer=True,
        processing_dir=settings._processing_dir(config), preflight=translation_run.run_translation_preflight,
        on_route=on_route, **configuration)
