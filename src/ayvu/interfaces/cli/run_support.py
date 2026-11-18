"""Book execution adapters, progress displays and checkpoint actions."""
from __future__ import annotations

from pathlib import Path
import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from ...translation import ItemOutcome, ItemStatus
from .contracts import ValidationResult
from ...composition.cli import _BookHooks, _BookRequest, _BookOutcome, validate_output, _translate_without_project as execute_translate_without_project, _translate_in_project as execute_translate_in_project
from ...translation import ResumeStateError
from ...composition.cli import require_translation_model, translation_modality
from . import settings
from ...translation import PreflightError
from .progress import TranslationProgress

from .render import _print_expected_error, console


class _BatchItemExit(typer.Exit):
    """Presentation exit with the content-free outcome of the active item."""

    def __init__(self, outcome: ItemOutcome) -> None:
        super().__init__(code=1)
        self.outcome = outcome


def _auxiliary_output_failed(exc: Exception, batch_plan: object | None, active_job: object | None) -> None:
    """A report or missing-text file of a batch item failed: end the item, keeping its job's outcome."""
    if batch_plan is None:
        raise exc
    code = "auxiliary_allocation_exhausted" if str(exc) == "auxiliary_allocation_exhausted" else "report_failed"
    raise _BatchItemExit(ItemOutcome(ItemStatus.FAILED, failure=code,
        job_ref=active_job.job_id if active_job else None,
        job_state=active_job.state.value if active_job and active_job.state else None,
        output_published=bool(active_job and active_job.output_ready))) from None


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


def _translate_without_project(book: _BookRequest, hooks: _BookHooks, *, validate: bool = True, legacy_resume: bool = False) -> _BookOutcome:
    return _with_selection(book, execute_translate_without_project(book, hooks, validate=validate, legacy_resume=legacy_resume))


def _translate_in_project(book: _BookRequest, hooks: _BookHooks) -> _BookOutcome:
    return _with_selection(book, execute_translate_in_project(book, hooks))


def _checkpoint_action(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except (OSError, ResumeStateError) as exc:
        console.print(f"[red]Resume state check failed:[/red] {exc}")
        console.print("Choose a writable processing directory or fix permissions for Documentos/Livros/Processando.")
        raise typer.Exit(code=1) from exc


def cache_coverage_blocked(book, report):
    """Only labeled dry runs measure full coverage independently of publication."""
    options = book.options
    incomplete = (report.texts_missing > 0 if translation_modality(
        options.dry_run, book.translator_models, book.translator_name) == "transient_dry_run" else not report.output_written)
    return options.cache_only and options.require_full_cache and incomplete


def translation_progress(progress, options, config, translator_name):
    modality = translation_modality(options.dry_run, config.translator_models, translator_name)
    return TranslationProgress(progress, dry_run=options.dry_run, workers=options.workers,
                               local_sequential=modality == "transient_dry_run")


def translation_configuration(config, translator_name, mode, continuation, resolve_managed_project):
    """Refuse before project resolution; only the resume runner disables managed resolution."""
    config = config or settings._load_existing_config_or_default()
    try:
        require_translation_model(config.translator_models, translator_name,
            continuation=continuation, legacy_resume=not resolve_managed_project)
    except PreflightError as error:
        _print_expected_error(error.summary, error.next_step, mode, detail=error.detail)
        raise typer.Exit(code=1) from None
    return config


def _with_selection(book, outcome):
    """Attach only the already selected public names, for both terminal and Markdown rendering."""
    if outcome.report is not None:
        outcome.report.provider_name = book.translator_name
        outcome.report.model_name = book.translator_models.get(book.translator_name)
    return outcome
