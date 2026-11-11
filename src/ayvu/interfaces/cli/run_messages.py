"""Translation failures, interruptions and recovery guidance."""
from __future__ import annotations
from . import plan

from pathlib import Path
from rich.table import Table
from .progress import TranslationProgressSnapshot
from .contracts import TranslationReport
from ...composition.cli import _BookOutcome
from ...kernel import UserMode

from .render import _TRANSLATION_FAILURES, _print_expected_error, console


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


def _print_project_failure(code: str, mode: UserMode) -> None:
    next_step = _PROJECT_NEXT_STEPS.get(code, _PROJECT_NEXT_STEP)
    _print_expected_error("Não foi possível resolver o projeto local.", next_step, mode, detail=code)


def _print_translation_failure(outcome: _BookOutcome, output_path: Path, mode: UserMode) -> None:
    code = outcome.failure or outcome.warning or "unexpected_error"
    template = _TRANSLATION_FAILURES.get(code) or _TRANSLATION_FAILURES["unexpected_error"]
    detail = str(outcome.raised) if outcome.raised is not None and str(outcome.raised) else code
    _print_expected_error(template[0], template[1].format(output=output_path), mode, detail=detail)


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
    for label, value in plan._execution_control_rows(
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


def _print_cache_coverage_blocked(report: TranslationReport, mode: UserMode) -> None:
    _print_expected_error(
        f"Cache incompleto: {report.texts_missing} texto(s) sem tradução no cache.",
        (
            "Remova --require-full-cache para gerar mesmo assim, traduza o EPUB normalmente "
            "para preencher o cache, ou consulte o arquivo de textos ausentes."
        ),
        mode,
    )
