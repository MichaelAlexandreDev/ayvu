"""Execute one book translation and coordinate its outputs."""
from __future__ import annotations
from . import plan, run_messages, run_support, settings, translate_options

from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn
import typer
from ...translation import ItemOutcome, ItemStatus
from . import report as reports, output as output_paths
from ...preferences import AyvuConfig
from .contracts import AcceptedItem, TranslationReport
from ...composition.cli import _BookHooks, _BookRequest, _BookOutcome, open_review_use_cases, destination_fingerprint, run_translation_preflight, resolve_translation_project, batch_item_outcome, prepare_translation, translation_checkpoint
from ...kernel import ChapterSelection, LanguagePair, OutputPlan, TranslationMemoryOptions, TranslationOptions, UserMode
from ...translation import BatchOutputError, BookRefused, PreflightError, ProjectTranslationUnavailable, TranslationDryRunCancelled
from ...projects import ProjectError
from ...review import ReviewExportCommand, ReviewOutcome
from ...network import NetworkPolicy
from ...translation import ContinuationTicket, TranslateProjectResult

from .render import _print_expected_error, _print_network_selection, console
from .options import _selected_network_policy


@dataclass(frozen=True)
class TranslationRunResult:
    report: TranslationReport
    validation_warnings: list[str]
    markdown_report_path: Path | None = None
    project_id: str | None = None
    job_result: TranslateProjectResult | None = None


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
            raise run_support._BatchItemExit(normalized)
        raise typer.Exit(code=1)

    config = config or settings._load_existing_config_or_default()
    resolved_profile_name, profile = translate_options._resolve_requested_profile(config, profile_name, mode=mode)
    resolved_target = translate_options._resolve_target_language(target, profile)
    resolved_glossary_path = translate_options._resolve_glossary_path(glossary_path, profile)
    resolved_source, source_inferred = translate_options._resolve_source_language(source, epub_path, mode=mode)
    language_pair = LanguagePair(source=resolved_source, target=resolved_target)
    # A resume holds the profile of the run it continues, and shows no profile, because it applies none of its settings.
    identity_profile_name = project_profile_name or resolved_profile_name
    plan._print_translation_plan(
        language_pair,
        source_inferred=source_inferred,
        mode=mode,
        profile_name=resolved_profile_name,
        profile_style=profile.style if profile else None,
        performance_profile_name=performance_profile_name,
        workers=workers, local_sequential=run_support.translation_modality(dry_run, config.translator_models, translator_name) == "transient_dry_run",
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
        selected_chapters = translate_options._resolve_chapter_selection_for_display(
            epub_path=epub_path,
            selection=chapter_selection,
            options=translation_options,
            mode=mode,
        )
        plan._print_selected_chapters(selected_chapters)
    output_plan = OutputPlan.for_translation(
        epub_path,
        output,
        language_pair,
        dry_run=dry_run,
        default_dir=output_dir or output_paths._translated_books_dir(config),
    )
    if batch_plan is not None:
        output_plan = batch_plan.output
        batch_plan.revalidate()
    if confirm_output_location:
        output_plan = output_paths._confirm_default_output_location(output_plan, epub_path, mode=mode)
    if batch_plan is None:
        output_plan = output_paths._resolve_existing_output_conflict(output_plan, overwrite=overwrite, mode=mode)
    output_path = output_plan.path
    review_output_path = translate_options._resolve_review_output_path(
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
            run_messages._print_project_failure(str(exc), mode)
            item_exit("project_unavailable")

    checkpoint = translation_checkpoint()
    progress = run_support._translation_progress_display()
    progress_view = run_support.translation_progress(progress, translation_options, config, translator_name)

    def prepare() -> object:
        prepared = prepare_translation(
            epub_path=epub_path, options=translation_options, cache_path=cache_path,
            glossary_path=resolved_glossary_path, translator_name=translator_name,
            url=url, timeout=timeout, retries=retries, requests_per_second=requests_per_second,
            retry_backoff=retry_backoff, retry_backoff_max=retry_backoff_max,
            output_path=output_path, overwrite=overwrite, processing_dir=settings._processing_dir(config),
            profile_name=identity_profile_name, preflight=run_translation_preflight,
            translator_models=config.translator_models, project_backed=project_id is not None,
            network_policy=_selected_network_policy(network_policy), modality=run_support.translation_modality(dry_run, config.translator_models, translator_name),
            save_checkpoint=lambda **values: run_support._checkpoint_action(checkpoint.start, **values),
            on_route=lambda route: plan._print_translation_route(route, mode=mode), on_ready=progress.start,
        )
        _print_network_selection(getattr(prepared, "network_decision", None))
        return prepared

    def on_chapter_start(index: int, total: int, name: str) -> None:
        progress_view.chapter_started(index, total, name)
        run_support._checkpoint_action(checkpoint.chapter_started, index, total, name)

    def on_chapter_done(index: int, total: int, name: str, stats: object) -> None:
        progress_view.chapter_done(index, total, name, stats)
        run_support._checkpoint_action(checkpoint.chapter_done, index, total, name, stats)

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
                run_support._translate_without_project(book, hooks)
                if project_id is None
                else run_support._translate_in_project(book, hooks)
            )
        finally:
            progress.stop()
    except PreflightError as exc:
        _print_expected_error(exc.summary, exc.next_step, mode, detail=exc.detail)
        item_exit("provider_failed")
    except BookRefused as exc:
        run_messages._print_translation_failure(_BookOutcome(failure=exc.code), output_path, mode)
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
        raise run_support._BatchItemExit(ItemOutcome(ItemStatus.CANCELLED)) from None
    except KeyboardInterrupt as exc:
        if hasattr(exc, "result"):
            record_job(exc.result)
        run_messages._print_interrupted_translation(
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
        run_messages._print_translation_failure(replace(outcome, raised=None) if batch_plan is not None else outcome, output_path, mode)
        item_exit(outcome.failure if outcome.failure in ("output_conflict", "provider_failed") else "translation_failed")
    report = outcome.report
    if batch_plan is not None and report.output_written and not dry_run:
        batch_plan.mark_published()
    if review_output_path is not None:
        if outcome.job_result is None:
            run_messages._print_review_export_failure("translation_identity_unavailable", mode)
            raise typer.Exit(code=1)
        use_cases = open_review_use_cases(settings._processing_dir(config))
        cancellation = use_cases.cancellation()
        exported = use_cases.export.execute(ReviewExportCommand(
            outcome.job_result, review_output_path, destination_fingerprint(review_output_path), overwrite,
            cancellation=cancellation.token))
        if exported.outcome is not ReviewOutcome.READY or exported.artifact is None:
            run_messages._print_review_export_failure(exported.code, mode)
            raise typer.Exit(code=1)
        console.print(f"[green]Review file saved to:[/green] {review_output_path}")
        report.review_output_path = review_output_path

    if cache_only and report.missing_texts:
        try:
            reports._save_missing_texts(
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
            raise run_support._BatchItemExit(ItemOutcome(ItemStatus.FAILED, failure=code,
                job_ref=active_job.job_id if active_job else None,
                job_state=active_job.state.value if active_job and active_job.state else None,
                output_published=bool(active_job and active_job.output_ready))) from None

    validation = outcome.validation
    validation_warnings = list(validation.warnings) if validation else []

    reports._print_report(report, dry_run, validation_warnings)
    markdown_report_path = None
    if auto_save_markdown_report:
        try:
            markdown_report_path = reports._save_markdown_report(
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
            raise run_support._BatchItemExit(ItemOutcome(ItemStatus.FAILED, failure=code,
                job_ref=active_job.job_id if active_job else None,
                job_state=active_job.state.value if active_job and active_job.state else None,
                output_published=bool(active_job and active_job.output_ready))) from None
        console.print(f"[green]Report saved to:[/green] {markdown_report_path}")
    else:
        reports._offer_markdown_report(report, dry_run, validation_warnings, mode=mode)

    if run_support.cache_coverage_blocked(book, report):
        run_messages._print_cache_coverage_blocked(report, mode)
        item_exit("cache_incomplete")

    if validation is not None:
        if validation.ok:
            console.print(
                f"[green]Validação OK:[/green] {validation.document_count} documentos XHTML/HTML encontrados."
            )
            run_support._checkpoint_action(checkpoint.completed)
        else:
            item_exit("validation_failed")

    if outcome.warning is not None:
        run_messages._print_translation_failure(outcome, output_path, mode)
        if outcome.warning != "provenance_failed":
            item_exit("translation_failed")

    return TranslationRunResult(
        report=report,
        validation_warnings=validation_warnings,
        markdown_report_path=markdown_report_path,
        project_id=project_id,
        job_result=outcome.job_result,
    )
