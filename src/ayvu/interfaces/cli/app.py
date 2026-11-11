from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, Optional, Protocol
import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from ...translation import ItemOutcome, ItemStatus
from .render import _TRANSLATION_FAILURES, _print_epub_read_error, _print_expected_error, _print_network_selection, console
from .options import DEFAULT_CACHE_PATH, DEFAULT_PREVIEW_DOCUMENT_LIMIT, DEFAULT_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF_MAX, DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE, DEFAULT_TRANSLATOR_URL, NETWORK_POLICY_HELP, _invocation_network_policy, _selected_network_policy
from . import settings
from .settings import _display_optional_path, _load_existing_config_or_default, _refuse_invalid_translator_models, _uses_internal_storage_defaults
from .commands import cache, content, review, translator
from .commands.translator import _languages_were_listed
from .progress import TranslationProgress, TranslationProgressSnapshot
from ...preferences import DEFAULT_BOOKS_DIR, AyvuConfig, ConfigError, FolderNames, TranslationProfile, config_from_preferences
from .contracts import AcceptedItem, ConfigStore, TranslationReport, ValidationResult, TranslationResumeState, InvalidResumeState, InterruptedTranslations, JobRecovery, SelectedChapter
from ...composition.cli import _BookHooks, _BookRequest, _BookOutcome, configuration_store, library_service, default_glossaries_dir, managed_glossary_repository, cli_provider_session, open_review_use_cases, destination_fingerprint, run_translation_preflight, resolve_translation_project, batch_output_intent, translate_batch, batch_item_outcome, book_refusal, check_book, detect_epub_language, interrupted_translations, open_job_recovery, resolve_chapter_selection, validate_output, prepare_translation, publish_cli_report, report_destination, translation_checkpoint, _translate_without_project as execute_translate_without_project, _translate_in_project as execute_translate_in_project
from ...knowledge.glossary import Entry as GlossaryEntry, GlossaryReference, list_glossaries, read_glossary, create_glossary, update_glossary
from ...kernel import ChapterSelection, ChapterSelectionError, DEFAULT_PERFORMANCE_PROFILE, LanguagePair, OutputPlan, PERFORMANCE_PROFILES, PerformanceProfile, TranslationMemoryError, TranslationMemoryOptions, TranslationOptions, UserMode, default_preview_books_dir, default_translated_books_dir
from ...translation import BatchOutputError, BookRefused, PreflightError, ProjectTranslationUnavailable, ResumeStateError, TranslationDryRunCancelled
from ...library import Book as LibraryBook, LibraryService, ScanRequest, ListRequest, DescribeRequest, OpenRequest
from ...projects import ProjectError
from ...recovery import RequiredDecision, ResumeRequest, ResumeResult, ResumeStatus
from ...review import ReviewExportCommand, ReviewOutcome
from ...providers import DiscoveredLanguage
from ...network import NetworkPolicy, network_next_step
from ...translation import ContinuationTicket, TranslateProjectResult


GLOSSARY_RULE_TRANSLATE = "translate"


GLOSSARY_RULE_PRESERVE = "preserve"


app = typer.Typer(help="Translate local EPUB files with a local HTTP translator.")


cache_app = typer.Typer(help="Inspect, clean, export, and import the translation cache.")


app.add_typer(cache_app, name="cache")


class _RouteView(Protocol):
    @property
    def is_direct(self) -> bool: ...

    def describe(self) -> str: ...


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
    job_result: TranslateProjectResult | None = None


_PROJECT_NEXT_STEPS = {
    "source_missing": "Informe uma fonte existente; o projeto anterior foi preservado.",
    "source_changed": "A fonte foi alterada. Restaure a revisão original ou escolha explicitamente outra fonte para um novo projeto.",
    "ambiguous_project": "Há projetos compatíveis duplicados. Preserve os manifests e resolva a duplicação antes de repetir.",
    "incomplete_project_creation": "Com todas as execuções encerradas, preserve a pasta .staging-* movendo-a para fora de projects e repita o comando.",
    "project_commit_uncertain": "O manifesto pode já estar publicado. Recarregue o estado ou repita a tradução para reutilizar o projeto.",
    "credential_bearing_endpoint": "Use uma URL do tradutor sem usuário, senha, parâmetros de consulta ou fragmento.",
}


_PROJECT_NEXT_STEP = (
    "Verifique a fonte, as permissões da pasta de processamento e o estado do projeto; "
    "preserve os arquivos existentes antes de corrigir conflitos."
)


@dataclass(frozen=True, repr=False)
class BatchTranslationResult:
    epub_path: Path
    succeeded: bool
    output_path: Path | None = None
    report_path: Path | None = None
    detail: str = ""


class _BatchItemExit(typer.Exit):
    """Presentation exit with the content-free outcome of the active item."""

    def __init__(self, outcome: ItemOutcome) -> None:
        super().__init__(code=1)
        self.outcome = outcome


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
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
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
        if ctx.invoked_subcommand is None and not any(v for k, v in ctx.params.items() if k not in {"mode", "network_policy"}):
            mode = UserMode.COMMON
    ctx.ensure_object(dict)
    ctx.obj["mode"] = mode
    ctx.obj["network_policy"] = network_policy
    network_token = _invocation_network_policy.set(network_policy)
    ctx.call_on_close(lambda: _invocation_network_policy.reset(network_token))

    if ctx.invoked_subcommand is not None:
        return

    if preview is not None:
        _run_preview(preview, mode=mode)
        return

    config = _load_existing_config_or_default()
    recovery = open_job_recovery(settings._processing_dir(config))
    found = _print_processing_translation_states(recovery)
    if _offer_detected_translation_resume(found, recovery, mode=mode):
        return

    if _run_guided_main_flow(ctx, mode=mode):
        return

    console.print(ctx.get_help())


app.command()(content.inspect)


app.command('test-translator')(translator.test_translator)


app.command('languages')(translator.languages)


cache_app.command('inspect')(cache.cache_inspect)


cache_app.command('clean')(cache.cache_clean)


cache_app.command('export')(cache.cache_export)


cache_app.command('import')(cache.cache_import)


@app.command()
def translate(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
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
        help="Optional v2 review package for the completed project translation.",
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
        help="Enable translation memory: approved repository in rich/v2; legacy cache fuzzy reuse without a model label. Off by default.",
    ),
    tm_conservative_auto_apply: bool = typer.Option(
        False, "--tm-conservative-auto-apply",
        help="Rich/v2 only: allow approved fuzzy reuse at a threshold of at least 0.98, with exact structural evidence.",
    ),
    tm_legacy_cache_compatibility: bool = typer.Option(
        False, "--tm-legacy-cache-compatibility",
        help="Rich/v2 only: opt into machine/unreviewed cache compatibility; v1 rows without stage metadata are excluded.",
    ),
    tm_apply_threshold: float = typer.Option(
        0.95,
        "--tm-apply-threshold",
        help="Similarity threshold (0-1). Rich/v2 fuzzy auto-apply also requires --tm-conservative-auto-apply and at least 0.98.",
    ),
    tm_suggest_threshold: float = typer.Option(
        0.80,
        "--tm-suggest-threshold",
        help="Minimum similarity for a review suggestion (0-1). Rich/v2 fuzzy matches are suggestions by default.",
    ),
) -> None:
    """Translate EPUB visible text while preserving EPUB structure."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    epub_list = list(epub_paths)
    if len(epub_list) > 100:
        _print_expected_error(
            "Um lote aceita no máximo 100 EPUBs.",
            "Divida os arquivos em lotes de até 100 EPUBs.",
            mode,
        )
        raise typer.Exit(code=1)
    if len(epub_list) > 1 and missing_output is not None:
        _print_expected_error(
            "Não é possível usar --missing-output com múltiplos EPUBs.",
            "Remova --missing-output para usar os arquivos automáticos de cada EPUB.",
            mode,
        )
        raise typer.Exit(code=1)
    _validate_translate_command_options(
        epub_paths=epub_list,
        output=output,
        output_dir=output_dir,
        review_output=review_output,
        mode=mode,
    )
    config = _load_existing_config_or_default()
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
        conservative_auto_apply=tm_conservative_auto_apply,
        legacy_cache_compatibility=tm_legacy_cache_compatibility,
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
            network_policy=_selected_network_policy(network_policy),
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
        network_policy=_selected_network_policy(network_policy),
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
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
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
    recovery = open_job_recovery(settings._processing_dir(config))
    found = interrupted_translations(recovery)

    state = _select_resume_state(found.running, epub_path, target, mode=mode)
    if state is None:
        raise typer.Exit(code=1)

    console.print(
        f"[green]Resuming translation:[/green] {state.input_path.name} -> {state.output_path.name}"
    )
    _print_resume_checkpoint(state)
    _resume_translation(state, found, recovery, mode=mode, network_policy=_selected_network_policy(network_policy))


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
    conservative_auto_apply: bool = False,
    legacy_cache_compatibility: bool = False,
) -> TranslationMemoryOptions | None:
    if not enabled:
        if conservative_auto_apply or legacy_cache_compatibility:
            _print_expected_error("As opções de política de memória requerem --translation-memory.",
                                  "Adicione --translation-memory.", mode)
            raise typer.Exit(code=1)
        return None

    options = TranslationMemoryOptions(
        apply_threshold=apply_threshold,
        suggest_threshold=suggest_threshold,
        conservative_auto_apply=conservative_auto_apply,
        legacy_cache_compatibility=legacy_cache_compatibility,
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
    network_policy: NetworkPolicy | None = None,
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
    project_profile_name: str | None = None,
    batch_plan: AcceptedItem | None = None,
    continuation: ContinuationTicket | None = None,
    operation_id: str | None = None,
    cancellation: object | None = None,
) -> TranslationRunResult:
    active_job = None

    def record_job(result: TranslateProjectResult) -> None:
        nonlocal active_job
        active_job = result

    def item_exit(code: str, *, interrupted: bool = False) -> NoReturn:
        if batch_plan is not None:
            normalized = (batch_item_outcome(active_job, interrupted=interrupted, fail_fast=fail_fast)
                          if active_job is not None else ItemOutcome(ItemStatus.FAILED, failure=code, interrupted=interrupted))
            raise _BatchItemExit(normalized)
        raise typer.Exit(code=1)

    config = config or _load_existing_config_or_default()
    resolved_profile_name, profile = _resolve_requested_profile(config, profile_name, mode=mode)
    resolved_target = _resolve_target_language(target, profile)
    resolved_glossary_path = _resolve_glossary_path(glossary_path, profile)
    resolved_source, source_inferred = _resolve_source_language(source, epub_path, mode=mode)
    language_pair = LanguagePair(source=resolved_source, target=resolved_target)
    # A resume holds the profile of the run it continues, and shows no profile, because it applies none of its settings.
    identity_profile_name = project_profile_name or resolved_profile_name
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
    if batch_plan is not None:
        output_plan = batch_plan.output
        batch_plan.revalidate()
    if confirm_output_location:
        output_plan = _confirm_default_output_location(output_plan, epub_path, mode=mode)
    if batch_plan is None:
        output_plan = _resolve_existing_output_conflict(output_plan, overwrite=overwrite, mode=mode)
    output_path = output_plan.path
    review_output_path = _resolve_review_output_path(
        review_output,
        dry_run=dry_run,
        overwrite=overwrite,
        mode=mode,
    )

    project_id = None
    source_sha256 = None
    if resolve_managed_project and not dry_run:
        try:
            resolution = resolve_translation_project(
                settings._processing_dir(config), epub_path, source=language_pair.source, target=language_pair.target,
                glossary_path=resolved_glossary_path, profile_name=identity_profile_name,
                translator=translator_name, url=url, chunk_limit=chunk_limit,
                translate_metadata=translate_metadata, translate_alt_text=translate_alt_text,
                chapters=chapter_selection.source if chapter_selection else None,
                translation_memory=translation_memory,
            )
            project_id = resolution.project.project_id
            source_sha256 = resolution.project.source_sha256
        except ProjectError as exc:
            _print_project_failure(str(exc), mode)
            item_exit("project_unavailable")

    checkpoint = translation_checkpoint()
    progress = _translation_progress_display()
    progress_view = TranslationProgress(progress, dry_run=dry_run, workers=workers)

    def prepare() -> object:
        prepared = prepare_translation(
            epub_path=epub_path, options=translation_options, cache_path=cache_path,
            glossary_path=resolved_glossary_path, translator_name=translator_name,
            url=url, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            output_path=output_path, overwrite=overwrite, processing_dir=settings._processing_dir(config),
            profile_name=identity_profile_name, preflight=run_translation_preflight,
            translator_models=config.translator_models, project_backed=project_id is not None,
            network_policy=_selected_network_policy(network_policy),
            save_checkpoint=lambda **values: _checkpoint_action(checkpoint.start, **values),
            on_route=lambda route: _print_translation_route(route, mode=mode), on_ready=progress.start,
        )
        _print_network_selection(getattr(prepared, "network_decision", None))
        return prepared

    def on_chapter_start(index: int, total: int, name: str) -> None:
        progress_view.chapter_started(index, total, name)
        _checkpoint_action(checkpoint.chapter_started, index, total, name)

    def on_chapter_done(index: int, total: int, name: str, stats: object) -> None:
        progress_view.chapter_done(index, total, name, stats)
        _checkpoint_action(checkpoint.chapter_done, index, total, name, stats)

    validation_task = None

    def on_validation(index: int, total: int, _name: str) -> None:
        nonlocal validation_task
        if validation_task is None:
            validation_task = progress.add_task("Validando EPUB", total=total)
        progress.update(validation_task, total=total, completed=index, description=f"Validando EPUB {index}/{total}")

    hooks = _BookHooks(on_chapter_start, on_chapter_done, progress_view.text_processed, on_validation)
    book = _BookRequest(
        epub_path=epub_path,
        output_plan=output_plan,
        # Whoever gets this far with a file already there accepted replacing it, by flag or at the prompt.
        replace_existing=batch_plan.overwrite if batch_plan is not None else (overwrite or output_path.exists()),
        options=translation_options,
        cache_path=cache_path,
        prepare=prepare,
        project_id=project_id,
        source_sha256=source_sha256,
        processing_dir=settings._processing_dir(config) if project_id is not None else None,
        continuation=continuation,
        operation_id=operation_id,
        batch_plan=batch_plan,
        cancellation=cancellation,
        on_project_result=record_job,
        translator_name=translator_name,
        translator_models=config.translator_models,
    )
    try:
        try:
            outcome = (
                _translate_without_project(book, hooks)
                if project_id is None
                else _translate_in_project(book, hooks)
            )
        finally:
            progress.stop()
    except PreflightError as exc:
        _print_expected_error(exc.summary, exc.next_step, mode, detail=exc.detail)
        item_exit("provider_failed")
    except BookRefused as exc:
        _print_translation_failure(_BookOutcome(failure=exc.code), output_path, mode)
        item_exit("input_failed")
    except ProjectTranslationUnavailable as exc:
        _print_expected_error(
            "Não foi possível abrir o estado do projeto local.",
            "Verifique as permissões da pasta de processamento e repita. Nada foi traduzido nem escrito.",
            mode,
            detail=str(exc),
        )
        item_exit("project_unavailable")
    except TranslationDryRunCancelled:
        raise _BatchItemExit(ItemOutcome(ItemStatus.CANCELLED)) from None
    except KeyboardInterrupt as exc:
        if hasattr(exc, "result"):
            record_job(exc.result)
        _print_interrupted_translation(
            snapshot=progress_view.snapshot(),
            output_path=output_path,
            cache_path=cache_path,
            dry_run=dry_run,
            workers=workers,
            requests_per_second=requests_per_second,
            retries=retries,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
        )
        if batch_plan is not None:
            item_exit("interrupted", interrupted=True)
        raise typer.Exit(code=1) from exc

    for notice in outcome.notices:
        console.print(f"[yellow]Aviso da tradução:[/yellow] {notice}")
    if outcome.report is None:
        _print_translation_failure(replace(outcome, raised=None) if batch_plan is not None else outcome, output_path, mode)
        item_exit(outcome.failure if outcome.failure in ("output_conflict", "provider_failed") else "translation_failed")
    report = outcome.report
    if batch_plan is not None and report.output_written and not dry_run:
        batch_plan.mark_published()
    if review_output_path is not None:
        if outcome.job_result is None:
            _print_review_export_failure("translation_identity_unavailable", mode)
            raise typer.Exit(code=1)
        use_cases = open_review_use_cases(settings._processing_dir(config))
        cancellation = use_cases.cancellation()
        exported = use_cases.export.execute(ReviewExportCommand(
            outcome.job_result, review_output_path, destination_fingerprint(review_output_path), overwrite,
            cancellation=cancellation.token))
        if exported.outcome is not ReviewOutcome.READY or exported.artifact is None:
            _print_review_export_failure(exported.code, mode)
            raise typer.Exit(code=1)
        console.print(f"[green]Review file saved to:[/green] {review_output_path}")
        report.review_output_path = review_output_path

    if cache_only and report.missing_texts:
        try:
            _save_missing_texts(
                report,
                missing_output=missing_output,
                overwrite=overwrite,
                directory=report_dir,
                mode=mode,
                batch_plan=batch_plan,
            )
        except (BatchOutputError, OSError) as exc:
            if batch_plan is None:
                raise
            code = "auxiliary_allocation_exhausted" if str(exc) == "auxiliary_allocation_exhausted" else "report_failed"
            raise _BatchItemExit(ItemOutcome(ItemStatus.FAILED, failure=code,
                job_ref=active_job.job_id if active_job else None,
                job_state=active_job.state.value if active_job and active_job.state else None,
                output_published=bool(active_job and active_job.output_ready))) from None

    validation = outcome.validation
    validation_warnings = list(validation.warnings) if validation else []

    _print_report(report, dry_run, validation_warnings)
    markdown_report_path = None
    if auto_save_markdown_report:
        try:
            markdown_report_path = _save_markdown_report(
                report,
                dry_run,
                validation_warnings,
                directory=report_dir,
                batch_plan=batch_plan,
            )
        except (BatchOutputError, OSError) as exc:
            if batch_plan is None:
                raise
            code = "auxiliary_allocation_exhausted" if str(exc) == "auxiliary_allocation_exhausted" else "report_failed"
            raise _BatchItemExit(ItemOutcome(ItemStatus.FAILED, failure=code,
                job_ref=active_job.job_id if active_job else None,
                job_state=active_job.state.value if active_job and active_job.state else None,
                output_published=bool(active_job and active_job.output_ready))) from None
        console.print(f"[green]Report saved to:[/green] {markdown_report_path}")
    else:
        _offer_markdown_report(report, dry_run, validation_warnings, mode=mode)

    if cache_only and require_full_cache and not report.output_written:
        _print_cache_coverage_blocked(report, mode)
        item_exit("cache_incomplete")

    if validation is not None:
        if validation.ok:
            console.print(
                f"[green]Validação OK:[/green] {validation.document_count} documentos XHTML/HTML encontrados."
            )
            _checkpoint_action(checkpoint.completed)
        else:
            item_exit("validation_failed")

    if outcome.warning is not None:
        _print_translation_failure(outcome, output_path, mode)
        if outcome.warning != "provenance_failed":
            item_exit("translation_failed")

    return TranslationRunResult(
        report=report,
        validation_warnings=validation_warnings,
        markdown_report_path=markdown_report_path,
        project_id=project_id,
        job_result=outcome.job_result,
    )


def _translation_progress_display() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def _print_project_failure(code: str, mode: UserMode) -> None:
    next_step = _PROJECT_NEXT_STEPS.get(code, _PROJECT_NEXT_STEP)
    _print_expected_error("Não foi possível resolver o projeto local.", next_step, mode, detail=code)


def _print_translation_failure(outcome: _BookOutcome, output_path: Path, mode: UserMode) -> None:
    code = outcome.failure or outcome.warning or "unexpected_error"
    template = _TRANSLATION_FAILURES.get(code) or _TRANSLATION_FAILURES["unexpected_error"]
    detail = str(outcome.raised) if outcome.raised is not None and str(outcome.raised) else code
    _print_expected_error(template[0], template[1].format(output=output_path), mode, detail=detail)


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
    network_policy: NetworkPolicy | None = None,
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

    _, profile = _resolve_requested_profile(config, profile_name, mode=mode)
    target_language = _resolve_target_language(target, profile)
    intents = []
    try:
        for epub_path in epub_paths:
            output_plan = OutputPlan.for_translation(
                epub_path, None, LanguagePair(source or "", target_language), dry_run=dry_run,
                default_dir=resolved_output_dir,
            )
            output_plan = _resolve_existing_output_conflict(output_plan, overwrite=overwrite, mode=mode)
            intents.append(batch_output_intent(
                epub_path, output_plan, report_dir, _report_stem_for(epub_path, target_language),
                _missing_stem_for(epub_path, target_language) if cache_only else None,
                overwrite or (not dry_run and output_plan.path.exists()),
            ))
    except BatchOutputError as exc:
        _print_expected_error(
            "Não foi possível planejar as saídas do lote.",
            "Escolha destinos distintos dos EPUBs originais e tente novamente.",
            mode, detail=str(exc),
        )
        raise typer.Exit(code=1) from None

    results: list[BatchTranslationResult] = []
    total = len(epub_paths)

    def execute_item(epub_path: Path):
        def execute(plan: AcceptedItem, correlation_id: str, ordinal: int, cancellation) -> ItemOutcome:
            console.print(f"[cyan]Batch item {ordinal}/{total}:[/cyan] {epub_path}")
            try:
                run_result = _run_translation(
                    epub_path=epub_path, output=None, output_dir=resolved_output_dir, review_output=None,
                    source=source, target=target, translator_name=translator_name, url=url,
                    network_policy=_selected_network_policy(network_policy),
                    cache_path=cache_path, glossary_path=glossary_path, profile_name=profile_name,
                    performance_profile_name=performance_profile_name, dry_run=dry_run, fail_fast=fail_fast,
                    chapter_selection=chapter_selection, overwrite=overwrite, timeout=timeout, retries=retries,
                    requests_per_second=requests_per_second, retry_backoff=retry_backoff,
                    retry_backoff_max=retry_backoff_max, chunk_limit=chunk_limit, workers=workers, mode=mode,
                    config=config, translate_metadata=translate_metadata, translate_alt_text=translate_alt_text,
                    cache_only=cache_only, require_full_cache=require_full_cache, translation_memory=translation_memory,
                    auto_save_markdown_report=True, report_dir=report_dir, confirm_output_location=False,
                    batch_plan=plan, operation_id=correlation_id,
                    cancellation=cancellation,
                )
            except _BatchItemExit as exc:
                if exc.outcome.output_published and exc.outcome.failure in ("report_failed", "auxiliary_allocation_exhausted"):
                    console.print("[yellow]O EPUB foi publicado, mas o relatório não pôde ser salvo.[/yellow]")
                if exc.outcome.output_published and exc.outcome.interrupted:
                    console.print("[yellow]O EPUB foi publicado antes da interrupção.[/yellow]")
                results.append(BatchTranslationResult(epub_path=epub_path, succeeded=False,
                                                      detail="exit code 1"))
                console.print(f"[red]Batch item failed:[/red] {epub_path}")
                return exc.outcome
            except typer.Exit as exc:
                results.append(BatchTranslationResult(epub_path=epub_path, succeeded=False,
                                                      detail=f"exit code {exc.exit_code or 1}"))
                console.print(f"[red]Batch item failed:[/red] {epub_path}")
                return ItemOutcome(ItemStatus.FAILED, failure="translation_failed")
            except KeyboardInterrupt:
                results.append(BatchTranslationResult(epub_path=epub_path, succeeded=False, detail="exit code 1"))
                return ItemOutcome(ItemStatus.FAILED, failure="interrupted", interrupted=True)
            except BatchOutputError as exc:
                code = "auxiliary_allocation_exhausted" if str(exc) == "auxiliary_allocation_exhausted" else "output_conflict"
                _print_expected_error("Não foi possível publicar as saídas deste EPUB.",
                                      "Verifique os destinos e tente novamente.", mode, detail=code)
                results.append(BatchTranslationResult(epub_path=epub_path, succeeded=False, detail="exit code 1"))
                return ItemOutcome(ItemStatus.FAILED, failure=code)
            except Exception:
                _print_expected_error("Falha inesperada ao traduzir este EPUB.",
                                      "Verifique o EPUB, o tradutor local e as opções usadas antes de tentar novamente.",
                                      mode, detail="unexpected_error")
                results.append(BatchTranslationResult(epub_path=epub_path, succeeded=False, detail="unexpected_error"))
                return ItemOutcome(ItemStatus.FAILED, failure="unexpected_error")
            report = run_result.report
            job = run_result.job_result
            if job is not None:
                outcome = batch_item_outcome(job, report_saved=run_result.markdown_report_path is not None,
                                               fail_fast=fail_fast)
            elif report.errors or report.missing_texts or run_result.validation_warnings:
                outcome = ItemOutcome(ItemStatus.PARTIAL, warnings=("translation_warnings",))
            else:
                outcome = ItemOutcome(ItemStatus.SUCCESS)
            succeeded = outcome.status in (ItemStatus.SUCCESS, ItemStatus.PARTIAL) and not outcome.interrupted
            results.append(BatchTranslationResult(epub_path=epub_path, succeeded=succeeded,
                                                  output_path=report.output_path if succeeded else None,
                                                  report_path=run_result.markdown_report_path if succeeded else None,
                                                  detail="" if succeeded else "exit code 1"))
            return outcome
        return execute

    def on_progress(item) -> None:
        failure = item.outcome.normalized_failure
        if failure is not None and failure.code == "auxiliary_allocation_exhausted" and len(results) < item.ordinal:
            path = epub_paths[item.ordinal - 1]
            results.append(BatchTranslationResult(path, False, detail="auxiliary_allocation_exhausted"))
            console.print(f"[red]Batch item failed:[/red] {path}")

    try:
        result = translate_batch(tuple(zip(intents, (execute_item(path) for path in epub_paths))),
                                 continue_on_error=continue_on_error, on_progress=on_progress)
    except BatchOutputError as exc:
        _print_expected_error(
            "Não foi possível planejar as saídas do lote.",
            "Escolha destinos distintos dos EPUBs originais e tente novamente.", mode, detail=str(exc))
        raise typer.Exit(code=1) from None
    _print_batch_summary(results)
    if not result.succeeded:
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
    if not results:
        return
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
    network_policy: NetworkPolicy | None = None,
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
        check_book(epub_path)
    except BookRefused as exc:
        _print_translation_failure(_BookOutcome(failure=exc.code), output_path, mode)
        raise typer.Exit(code=1) from exc

    try:
        preflight = run_translation_preflight(
            network_policy=_selected_network_policy(network_policy),
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
    _print_network_selection(getattr(preflight, "network_decision", None))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress_view = TranslationProgress(progress, dry_run=False)
        outcome = _translate_without_project(
            _BookRequest(epub_path, output_plan, True, translation_options, cache_path, lambda: preflight),
            _BookHooks(progress_view.chapter_started, progress_view.chapter_done, progress_view.text_processed, lambda *args: None),
            validate=False,
        )
        report = outcome.report

    validation = _validate_with_progress(output_path)
    _print_report(report, dry_run=False, validation_warnings=validation.warnings)
    if validation.ok:
        console.print(f"[green]Preview salvo em:[/green] {output_path}")
        console.print(
            f"[green]Validação OK:[/green] {validation.document_count} documentos XHTML/HTML encontrados."
        )
        return

    raise typer.Exit(code=1)


app.command()(content.extract)


app.command('apply-review')(review.apply_review)


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
            detail="review_output_exists",
        )
        raise typer.Exit(code=1)

    return path


def _print_review_export_failure(code: str, mode: UserMode) -> None:
    if code in {"output_exists", "review_file_exists"}:
        _print_expected_error(
            "Arquivo de revisão já existe.",
            "Use --overwrite para substituí-lo ou escolha outro caminho em --review-output.",
            mode,
            detail="review_output_exists",
        )
        return
    _print_expected_error(
        "Não foi possível salvar o pacote de revisão.",
        "Confirme que a tradução terminou com um artefato íntegro e escolha um caminho gravável para --review-output.",
        mode,
        detail=code,
    )


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


def _print_processing_translation_states(recovery: JobRecovery) -> InterruptedTranslations:
    found = interrupted_translations(recovery)
    if found.running:
        _print_running_resume_states(found.running)
    if found.invalid:
        _print_invalid_resume_states(found.invalid)
    return found


def _run_guided_main_flow(ctx: typer.Context, mode: UserMode) -> bool:
    if mode == UserMode.DEVELOPER:
        return False

    config = _load_or_init_config()
    _print_guided_main_menu()
    choice = typer.prompt("Choose an option", default=GUIDED_PREVIEW_OPTION).strip()
    return _handle_guided_main_choice(choice, ctx, config)


def _load_or_init_config(store: ConfigStore | None = None) -> AyvuConfig:
    store = store or configuration_store()
    snapshot = store.get_preferences()
    _refuse_invalid_translator_models(snapshot)
    if snapshot.error:
        console.print(f"[yellow]Configuração inválida ignorada:[/yellow] {snapshot.error}")
        console.print("Usando configuração padrão. Ajuste o arquivo de configuração se quiser alterar o idioma.")
        return AyvuConfig.default()
    if not snapshot.exists:
        return _init_default_language_config(store)
    if snapshot.read_only:
        console.print("[yellow]Configuração em somente leitura: campos desconhecidos impedem a gravação.[/yellow]")
    return config_from_preferences(snapshot.preferences, snapshot.revision)


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
        _preferences_revision='missing',
    )
    config, persisted = _save_config(store, config)
    if persisted:
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


def _choose_guided_translation_glossary() -> Path | None:
    directory = default_glossaries_dir()
    repository = managed_glossary_repository(directory)
    items = _list_glossary_files(repository)
    if not items:
        return None
    console.print("[yellow]Saved glossaries are available.[/yellow]")
    while True:
        item = _prompt_saved_glossary_path(items, back_label="Run without glossary")
        if item is None:
            return None
        if _load_guided_glossary(repository, item.reference) is not None:
            path = directory / item.reference.value
            console.print(f"[green]Glossary selected:[/green] {path}")
            return path
        console.print("Choose another glossary or run without glossary.")


def _prompt_new_glossary_path(directory: Path, items: tuple) -> Path | None:
    while True:
        raw_name = typer.prompt("Glossary name", default="glossary").strip()
        stem = _safe_filename_part(Path(raw_name).stem or raw_name).lower()
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


def _run_guided_settings(config: AyvuConfig) -> None:
    store = configuration_store()
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


def _print_guided_language_choices(languages: tuple[DiscoveredLanguage, ...]) -> None:
    table = Table(title="LibreTranslate languages")
    table.add_column("Option")
    table.add_column("Language")
    table.add_column("Code")
    table.add_column("State")

    for index, language in enumerate(languages, start=1):
        table.add_row(str(index), language.name, language.code, language.state)
    console.print(table)


def _language_code_from_guided_choice(choice: str, languages: tuple[DiscoveredLanguage, ...]) -> str | None:
    if not choice.isdigit():
        return None

    index = int(choice)
    if 1 <= index <= len(languages):
        return languages[index - 1].code
    return None


def _load_languages_for_guided_selection() -> tuple[DiscoveredLanguage, ...]:
    session = cli_provider_session(url=DEFAULT_TRANSLATOR_URL, timeout=10.0, retries=1,
                                   network_policy=_selected_network_policy())
    result = session.languages()
    _print_network_selection(session.network_decision)
    if _languages_were_listed(result):
        return result.languages
    console.print(f"[yellow]Could not list LibreTranslate languages:[/yellow] {result.code}")
    if network_next_step(result.code):
        console.print(session.network_decision.diagnostic("languages", result.code))
        console.print(network_next_step(result.code))
    return ()


def _offer_detected_translation_resume(found: InterruptedTranslations, recovery: JobRecovery, mode: UserMode) -> bool:
    states = found.running
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
    _resume_translation(state, found, recovery, mode=mode)
    return True


class _ResumeRunner:
    """Runs a resume with what its file recorded, on whichever path the use case chose."""

    def __init__(self, state: TranslationResumeState, mode: UserMode, network_policy: NetworkPolicy | None = None) -> None:
        self._state = state
        self._mode = mode
        self._network_policy = _selected_network_policy(network_policy)

    def run_legacy(self, reference: str) -> None:
        self._run(resolve_managed_project=False)

    def run_durable(self, reference: str, ticket: ContinuationTicket, operation_id: str) -> TranslateProjectResult:
        return self._run(continuation=ticket, operation_id=operation_id).job_result

    def _run(self, **identity: object) -> TranslationRunResult:
        state, mode = self._state, self._mode
        return _run_translation(
            epub_path=state.input_path,
            output=state.output_path,
            review_output=None,
            source=state.source,
            target=state.target,
            translator_name=state.translator_name,
            url=state.url,
            network_policy=self._network_policy,
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
            project_profile_name=state.profile_name,
            **identity,
        )


def _resume_translation(state: TranslationResumeState, found: InterruptedTranslations, recovery: JobRecovery, mode: UserMode,
                        network_policy: NetworkPolicy | None = None) -> None:
    entry = found.entry_of(state)
    # Asking for this resume is the decision: the command says what it does, and it has always replaced its output.
    request = ResumeRequest(entry.reference, entry.job_id, entry.project_id, entry.expected_revision, tuple(RequiredDecision))
    try:
        result = recovery.request_resume.execute(request, _ResumeRunner(state, mode, network_policy))
        if result.status in (ResumeStatus.REFUSED, ResumeStatus.ALREADY_REQUESTED):
            _print_resume_refusal(result, state, mode)
            raise typer.Exit(code=1)
    except typer.Exit:
        console.print(
            "Não foi possível retomar a tradução detectada. Verifique a mensagem acima e reinicie a tradução se necessário."
        )
        raise


def _print_resume_refusal(result: ResumeResult, state: TranslationResumeState, mode: UserMode) -> None:
    code = "continuation_exists" if result.status is ResumeStatus.ALREADY_REQUESTED else result.code
    if code in ("source_changed", "source_missing", "source_unreadable"):
        _print_project_failure(code, mode)
    elif code == "not_found":
        _print_expected_error(
            "No translation in progress was found to resume.",
            "Start a translation with 'ayvu translate <epub>' or check the processing folder.",
            mode,
        )
    else:
        failure = (book_refusal(state.input_path) or "unexpected_error") if code == "policy_denied" else code
        _print_translation_failure(_BookOutcome(failure=failure), state.output_path, mode)


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
    batch_plan: AcceptedItem | None = None,
) -> Path:
    if batch_plan is not None:
        return batch_plan.write_auxiliary("report", _render_markdown_report(report, dry_run, validation_warnings))
    directory = directory or _default_reports_dir()
    path = _next_available_report_path(directory, _report_filename_stem(report))
    publish_cli_report(path, _render_markdown_report(report, dry_run, validation_warnings))
    return path


def _save_missing_texts(
    report: TranslationReport,
    missing_output: Path | None,
    overwrite: bool,
    directory: Path | None,
    mode: UserMode,
    batch_plan: AcceptedItem | None = None,
) -> None:
    if batch_plan is not None:
        path = batch_plan.write_auxiliary("missing", _render_missing_texts(report))
        console.print(f"[green]Missing texts saved to:[/green] {path}")
        return
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
        path = _next_available_report_path(directory, _missing_filename_stem(report), suffix=".txt")

    try:
        publish_cli_report(path, _render_missing_texts(report))
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
    return _missing_stem_for(report.input_path, report.target_language)


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
    return report_destination(directory, stem, suffix)


def _report_filename_stem(report: TranslationReport) -> str:
    return _report_stem_for(report.input_path, report.target_language)


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


def _report_stem_for(input_path, target_language):
    source = _safe_filename_part(input_path.stem if input_path else "translation")
    target = _safe_filename_part(target_language or "translated")
    return f"{source}-{target}-report"


def _missing_stem_for(input_path, target_language):
    source = _safe_filename_part(input_path.stem if input_path else "translation")
    target = _safe_filename_part(target_language or "translated")
    return f"{source}-{target}-missing"


if __name__ == "__main__":
    app()
