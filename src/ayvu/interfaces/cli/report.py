"""Translation summaries, Markdown reports and missing-text files."""
from __future__ import annotations
from . import settings
from pathlib import Path
import typer
from rich.table import Table
from .settings import _display_optional_path, _uses_internal_storage_defaults
from ...preferences import AyvuConfig
from .contracts import AcceptedItem, TranslationReport
from ...composition.cli import publish_cli_report, report_destination
from ...kernel import UserMode

from .render import _print_expected_error, console


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
    if report.execution_note:
        console.print(report.execution_note)

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
    if report.execution_note:
        lines.append(f"- Execution: {report.execution_note}")
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
    return _reports_dir(settings._load_existing_config_or_default())


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


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _format_terms(terms: list[str]) -> str:
    return ", ".join(_single_line(term) for term in terms) if terms else "-"


def _format_counted_terms(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{_single_line(term)} ({count})" for term, count in sorted(counts.items()))


def _reports_dir(config: AyvuConfig) -> Path:
    if _uses_internal_storage_defaults(config):
        return Path.home() / "Documentos" / "Livros" / "Relatorios"
    return config.reports_dir


def _report_stem_for(input_path, target_language):
    source = _safe_filename_part(input_path.stem if input_path else "translation")
    target = _safe_filename_part(target_language or "translated")
    return f"{source}-{target}-report"


def _missing_stem_for(input_path, target_language):
    source = _safe_filename_part(input_path.stem if input_path else "translation")
    target = _safe_filename_part(target_language or "translated")
    return f"{source}-{target}-missing"
