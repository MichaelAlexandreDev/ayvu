"""What the cache commands print: summaries, import reports, warnings and failure hints."""
from __future__ import annotations

from pathlib import Path
from rich.table import Table
from ..contracts import CacheSummaryRow, CacheImportReport
from ..render import console


def _cache_warnings(result):
    for warning in result.warnings:
        console.print(f"[yellow]Cache warning:[/yellow] {warning.code}")


_CACHE_LIMIT_CODES = frozenset({"limit_exceeded", "file_too_large", "too_many_entries", "text_too_large",
                                "structure_too_deep", "structure_too_large"})


def _cache_failure_hint(code: str, default: str, too_large: str | None = None, *, v2: bool = False) -> str:
    if code == "v2-admin-required":
        return "Este cache requer administração compatível com v2."
    if code == "stale_preview":
        return "A previsão ficou desatualizada. Execute o comando novamente para gerar uma nova previsão."
    if code == "import_conflict":
        return "Revise os conflitos na previsão. Para substituir essas entradas, execute novamente com --replace e confirme a nova previsão."
    if code == "duplicate_conflict":
        return "O arquivo contém entradas divergentes para a mesma chave de destino. Corrija o arquivo ou o mapa; --replace não resolve essa colisão."
    if code in {"invalid_scope_map", "unmapped_scope", "scope_map_limit", "scope_reference_limit"}:
        return "Confira --scope-map: ele deve cobrir exatamente as origens do arquivo e autorizar cada destino, dentro dos limites documentados."
    if code == "unsupported_audit_mode":
        return "A importação mapeada exige journal delete, truncate ou persist. Prepare outro cache suportado; não altere o modo automaticamente."
    if code == "too_many_namespaces":
        return "Selecione menos namespaces por arquivo; o limite é de 10.000 seções."
    if code == "cache_busy":
        return "Outro processo está usando o cache. Espere ele terminar e tente novamente."
    if too_large is not None and code in _CACHE_LIMIT_CODES:
        limits = "1 GiB e 5.000.000 de entradas para v2; seleções somente legadas mantêm 512 MiB e 1.000.000 de entradas" if v2 else "512 MiB por arquivo e 1.000.000 de entradas"
        return f"O limite é de {limits}. {too_large}"
    return default


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
