"""Plan, execute and summarize a batch of book translations."""
from __future__ import annotations
from . import plan, run as translation_run, run_support, translate_options

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
import typer
from rich.table import Table
from ...translation import ItemOutcome, ItemStatus
from . import report as reports, output as output_paths
from .settings import _display_optional_path
from ...preferences import AyvuConfig
from .contracts import AcceptedItem
from ...composition.cli import batch_output_intent, translate_batch, batch_item_outcome
from ...kernel import ChapterSelection, LanguagePair, OutputPlan, TranslationMemoryOptions, UserMode
from ...translation import BatchOutputError
from ...network import NetworkPolicy

from .render import _print_expected_error, console
from .options import _selected_network_policy


@dataclass(frozen=True, repr=False)
class BatchTranslationResult:
    epub_path: Path
    succeeded: bool
    output_path: Path | None = None
    report_path: Path | None = None
    detail: str = ""


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
    resolved_output_dir = output_dir or output_paths._translated_books_dir(config)
    report_dir = reports._reports_dir(config)
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

    _, profile = translate_options._resolve_requested_profile(config, profile_name, mode=mode)
    target_language = translate_options._resolve_target_language(target, profile)
    intents = []
    try:
        for epub_path in epub_paths:
            output_plan = OutputPlan.for_translation(
                epub_path, None, LanguagePair(source or "", target_language), dry_run=dry_run,
                default_dir=resolved_output_dir,
            )
            output_plan = output_paths._resolve_existing_output_conflict(output_plan, overwrite=overwrite, mode=mode)
            intents.append(batch_output_intent(
                epub_path, output_plan, report_dir, reports._report_stem_for(epub_path, target_language),
                reports._missing_stem_for(epub_path, target_language) if cache_only else None,
                overwrite or (not dry_run and output_plan.path.exists()),
            ))
    except BatchOutputError as exc:
        _batch_planning_failed(exc, mode)

    results: list[BatchTranslationResult] = []
    total = len(epub_paths)

    def execute_item(epub_path: Path):
        def execute(plan: AcceptedItem, correlation_id: str, ordinal: int, cancellation) -> ItemOutcome:
            console.print(f"[cyan]Batch item {ordinal}/{total}:[/cyan] {epub_path}")
            try:
                run_result = translation_run._run_translation(
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
            except run_support._BatchItemExit as exc:
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
        _batch_planning_failed(exc, mode)
    _print_batch_summary(results)
    if not result.succeeded:
        raise typer.Exit(code=1)


def _batch_planning_failed(exc: BatchOutputError, mode: UserMode) -> NoReturn:
    _print_expected_error(
        "Não foi possível planejar as saídas do lote.",
        "Escolha destinos distintos dos EPUBs originais e tente novamente.",
        mode, detail=str(exc),
    )
    raise typer.Exit(code=1) from None


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
    for label, value in plan._execution_control_rows(
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
