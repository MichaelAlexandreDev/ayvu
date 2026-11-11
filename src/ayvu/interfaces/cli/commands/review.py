"""apply-review: apply a reviewed package to a translated book."""
from __future__ import annotations

from pathlib import Path
from typing import Optional
import typer
from rich.table import Table
from ....composition.cli import open_review_use_cases, destination_fingerprint, read_review_input
from ....kernel import UserMode
from ....review import ReviewApplyCommand, ReviewValidationCommand

from ..render import _print_expected_error, console
from .. import settings
from ..settings import _display_optional_path


def apply_review(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Original EPUB path used to verify the source revision.",
    ),
    review_csv: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Edited v2 review package. Legacy v1 files are inspection-only.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output EPUB path. Defaults to <input-stem>-reviewed.epub.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing an existing output EPUB."),
    validate_only: bool = typer.Option(False, "--validate-only", help="Validate the package without reconstructing an EPUB."),
) -> None:
    """Validate and apply one identity-bound v2 review package."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        package_bytes = read_review_input(review_csv)
    except Exception as exc:
        code = getattr(exc, "code", "review_file_unreadable")
        _print_expected_error(
            "Não foi possível ler o arquivo de revisão.",
            "Confirme que o arquivo é um pacote de revisão local válido e tente novamente.",
            mode,
            detail=code,
        )
        raise typer.Exit(code=1) from None

    use_cases = open_review_use_cases(settings._processing_dir(settings._load_existing_config_or_default()), source_path=epub_path)
    if validate_only:
        cancellation = use_cases.cancellation()
        validated = use_cases.validate.execute(
            ReviewValidationCommand(package_bytes, cancellation=cancellation.token))
        if validated.valid:
            table = Table(title="Review package validation")
            table.add_column("Metric")
            table.add_column("Value")
            table.add_row("Documents", str(validated.counts.documents))
            table.add_row("Units", str(validated.counts.units))
            table.add_row("Reviewed", str(validated.counts.reviewed))
            table.add_row("Kept machine translation", str(validated.counts.pending))
            console.print(table)
            raise typer.Exit(code=0)
        _print_review_apply_failure(validated.code, mode)
        raise typer.Exit(code=1)

    output_path = output or epub_path.with_name(f"{epub_path.stem}-reviewed{epub_path.suffix or '.epub'}")
    cancellation = use_cases.cancellation()
    applied = use_cases.apply.execute(ReviewApplyCommand(
        package_bytes, None, output_path, destination_fingerprint(output_path), overwrite,
        cancellation=cancellation.token))
    if not applied.success:
        _print_review_apply_failure(applied.code, mode)
        raise typer.Exit(code=1)

    table = Table(title="Review application report")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Output", _display_optional_path(output_path))
    table.add_row("Documents", str(applied.counts.documents))
    table.add_row("Units applied", str(applied.counts.reviewed))
    table.add_row("Kept machine translation", str(applied.counts.pending))
    console.print(table)
    console.print(f"[green]Reviewed EPUB saved to:[/green] {output_path}")


def _print_review_apply_failure(code: str, mode: UserMode) -> None:
    if code == "legacy_reexport_required":
        _print_expected_error(
            "CSV v1 serve apenas para inspeção e não pode reconstruir o EPUB.",
            "Exporte novamente usando --review-output depois de gerar uma tradução da mesma fonte e aplique o novo pacote v2.",
            mode,
            detail="legacy_reexport_required",
        )
        return
    if code in {"invalid_preamble", "invalid_encoding", "malformed_csv", "invalid_newline", "invalid_column_count"}:
        _print_expected_error(
            "Não foi possível ler o arquivo de revisão.",
            "Confirme que o arquivo é um pacote de revisão local válido e tente novamente.",
            mode,
            detail=code,
        )
        return
    if code in {"output_exists", "review_file_exists"}:
        _print_expected_error("O arquivo de saída já existe.", "Use --overwrite ou escolha outro caminho com --output.",
                              mode, detail="output_exists")
        return
    if code == "review_not_approved":
        _print_expected_error(
            "Nenhuma linha do pacote foi marcada como revisada.",
            "Edite reviewed_text nas linhas que quiser e marque review_status como reviewed nelas. As demais mantêm a tradução automática.",
            mode,
            detail=code,
        )
        return
    if code == "pending_row_edited":
        _print_expected_error(
            "Uma linha foi editada, mas não foi marcada como revisada.",
            "Marque review_status como reviewed nessa linha, ou desfaça a edição em reviewed_text. Nada foi gravado.",
            mode,
            detail=code,
        )
        return
    if code == "review_rejected":
        _print_expected_error(
            "O pacote tem uma linha marcada como rejeitada.",
            "Corrija reviewed_text e marque a linha como reviewed, ou volte para pending para manter a tradução automática. Nada foi gravado.",
            mode,
            detail=code,
        )
        return
    _print_expected_error(
        "Não foi possível validar ou aplicar o pacote de revisão.",
        "Confirme que o pacote v2 pertence à mesma revisão do projeto, à fonte e ao artefato de tradução; corrija os conflitos e tente novamente.",
        mode,
        detail=code,
    )
