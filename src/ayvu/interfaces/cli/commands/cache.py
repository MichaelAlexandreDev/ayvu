"""cache inspect, clean, export and import: administer the translation cache."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Optional
import typer
from ....knowledge.cache import CacheAdminError, CacheFilter, CacheRef, FileRef, InspectRequest, PreviewCleanRequest, ExecuteCleanRequest, ExportRequest, ImportRequest, CacheNamespace, CacheScopeMapping, ImportScopeMap, ExecuteImportRequest
from ....knowledge.cache import CacheAuthorizationPolicy, CacheScope, CacheScopeKind, CacheIdentityError
from ....composition.cli import cache_admin_services, read_scope_map_payload
from ....kernel import UserMode
from ..render import _print_expected_error, console
from ..options import DEFAULT_CACHE_PATH
from .cache_output import _cache_failure_hint, _cache_warnings, _plural_cache_entries, _print_cache_import_report, _print_cache_summary


def _cache_namespace(namespace: str | None, scope_kind: str | None, scope_id: str | None,
                     project_id: str | None, all_namespaces: bool = False):
    if all_namespaces:
        if namespace is not None or any((scope_kind, scope_id, project_id)):
            raise CacheAdminError("invalid_scope")
        return CacheNamespace.all()
    if namespace is None:
        if any((scope_kind, scope_id, project_id)):
            raise CacheAdminError("invalid_scope")
        return None
    if namespace == "legacy-v1":
        if any((scope_kind, scope_id, project_id)):
            raise CacheAdminError("invalid_scope")
        return CacheNamespace.legacy()
    if namespace != "v2" or not all((scope_kind, scope_id, project_id)):
        raise CacheAdminError("invalid_scope")
    try:
        kind = CacheScopeKind(scope_kind)
        if kind not in (CacheScopeKind.PROJECT, CacheScopeKind.LOCAL_USER, CacheScopeKind.WORK):
            raise ValueError()
        scope = CacheScope(kind, scope_id)
        policy = CacheAuthorizationPolicy(project_id, () if kind is CacheScopeKind.PROJECT else (scope,))
        return CacheNamespace.v2(policy.resolve(scope))
    except (ValueError, CacheIdentityError):
        raise CacheAdminError("invalid_scope") from None


def _cache_scope_map(path: Path):
    try:
        raw = read_scope_map_payload(FileRef(str(path)))
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(data, list) or len(data) > 10000:
            raise ValueError()
        def namespace(value, source=False):
            if not isinstance(value, dict):
                raise ValueError()
            if value == {"kind": "legacy-v1"}:
                return CacheNamespace.legacy()
            expected = {"kind", "scope_kind", "scope_id"} | (set() if source else {"project_id"})
            if set(value) != expected or value["kind"] != "v2":
                raise ValueError()
            source_project = (value["scope_id"] if value["scope_kind"] == "project" else
                              "00000000-0000-0000-0000-000000000001")
            return _cache_namespace("v2", value["scope_kind"], value["scope_id"],
                                    source_project if source else value["project_id"])
        if any(not isinstance(row, dict) or set(row) != {"source", "destination"} for row in data):
            raise ValueError()
        return ImportScopeMap(tuple(CacheScopeMapping(namespace(row["source"], True),
                                                       namespace(row["destination"])) for row in data))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, CacheAdminError):
        raise CacheAdminError("invalid_scope_map") from None


def _normalize_cache_before(value: str | None, mode: UserMode, *, preserve_fraction: bool = False) -> str | None:
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
            detail="invalid_before",
        )
        raise typer.Exit(code=1) from None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    if preserve_fraction and parsed.microsecond:
        return parsed.isoformat(sep=" ", timespec="microseconds")
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
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Select legacy-v1 or v2."),
    scope_kind: Optional[str] = typer.Option(None, "--scope-kind", help="V2 scope: project, local_user or work."),
    scope_id: Optional[str] = typer.Option(None, "--scope-id"),
    project_id: Optional[str] = typer.Option(None, "--project-id"),
    all_namespaces: bool = typer.Option(False, "--all", help="Select all cache namespaces."),
) -> None:
    """Show cached translations grouped by language pair."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    before_value = _normalize_cache_before(before, mode, preserve_fraction=namespace == "v2" or all_namespaces)
    try:
        services = cache_admin_services()
        result = services.inspect.execute(InspectRequest(CacheRef(str(cache_path)), CacheFilter(source, target, before_value), namespace=_cache_namespace(namespace, scope_kind, scope_id, project_id, all_namespaces)))
        _cache_warnings(result)
        if result.failure:
            raise CacheAdminError(result.failure.code, result.failure.family)
        summary = result.value
    except CacheAdminError as exc:
        _print_expected_error(
            "Não foi possível inspecionar o cache.",
            _cache_failure_hint(exc.code, "Verifique o caminho informado em --cache e tente novamente."),
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    if summary.adapter == "semantic_v2":
        console.print(f"[dim]Cache path:[/dim] {cache_path}")
        counts = Counter()
        for item in summary.namespaces:
            counts[(item.kind, item.scope_kind)] += item.count
        for (kind, scope_kind), count in sorted(counts.items(), key=lambda row: (row[0][0], row[0][1] or "")):
            label = kind if scope_kind is None else f"{kind} / {scope_kind}"
            console.print(f"{label}: {_plural_cache_entries(count)}")
        console.print(f"Total: {_plural_cache_entries(summary.count)}")
    else:
        _print_cache_summary(summary.pairs, cache_path)


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
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Select legacy-v1 or v2."),
    scope_kind: Optional[str] = typer.Option(None, "--scope-kind", help="V2 scope: project, local_user or work."),
    scope_id: Optional[str] = typer.Option(None, "--scope-id"),
    project_id: Optional[str] = typer.Option(None, "--project-id"),
) -> None:
    """Remove cache entries by filter or clear the whole cache."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    before_value = _normalize_cache_before(before, mode, preserve_fraction=namespace in ("v2", "all"))
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
        if namespace == "all" and not all_entries:
            raise CacheAdminError("invalid_scope")
        services = cache_admin_services()
        selection = services.preview.execute(PreviewCleanRequest(
            CacheRef(str(cache_path)), CacheFilter(source, target, before_value, all_entries), namespace=_cache_namespace(None if namespace == "all" else namespace, scope_kind, scope_id, project_id, namespace == "all" and all_entries)))
        _cache_warnings(selection)
        if selection.failure:
            raise CacheAdminError(selection.failure.code, selection.failure.family)
        if dry_run:
            console.print(f"[yellow]Dry run:[/yellow] {_plural_cache_entries(selection.value.count)} would be removed.")
            return
        report = services.clean.execute(ExecuteCleanRequest(selection.value))
        _cache_warnings(report)
        if report.failure:
            raise CacheAdminError(report.failure.code, report.failure.family)
    except CacheAdminError as exc:
        _print_expected_error(
            "Não foi possível limpar o cache.",
            _cache_failure_hint(exc.code, "Verifique o caminho informado em --cache e tente novamente."),
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Deleted {_plural_cache_entries(report.value)}.[/green]")


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
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Select legacy-v1 or v2."),
    scope_kind: Optional[str] = typer.Option(None, "--scope-kind", help="V2 scope: project, local_user or work."),
    scope_id: Optional[str] = typer.Option(None, "--scope-id"),
    project_id: Optional[str] = typer.Option(None, "--project-id"),
    all_namespaces: bool = typer.Option(False, "--all", help="Select all cache namespaces."),
) -> None:
    """Export cached translations to readable JSON."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    before_value = _normalize_cache_before(before, mode, preserve_fraction=namespace == "v2" or all_namespaces)
    try:
        services = cache_admin_services()
        report = services.export.execute(ExportRequest(
            CacheRef(str(cache_path)), FileRef(str(output)), CacheFilter(source, target, before_value), overwrite, namespace=_cache_namespace(namespace, scope_kind, scope_id, project_id, all_namespaces)))
        _cache_warnings(report)
        if report.failure:
            raise CacheAdminError(report.failure.code, report.failure.family)
    except CacheAdminError as exc:
        _print_expected_error(
            "Não foi possível exportar o cache.",
            _cache_failure_hint(exc.code, "Verifique --cache, o caminho de saída e tente novamente.",
                                "Exporte um namespace explícito menor ou reduza --before." if namespace == "v2" or all_namespaces else
                                "Exporte em partes com --source, --target ou --before.", v2=namespace == "v2" or all_namespaces),
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Cache export saved to:[/green] {output}")
    console.print(f"Entries exported: {report.value}")
    if namespace is not None or all_namespaces:
        console.print(f"JSON format: v{report.format_version}")


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
    scope_map: Optional[Path] = typer.Option(None, "--scope-map", help="External JSON namespace mapping."),
    all_namespaces: bool = typer.Option(False, "--all", help="Not allowed for import."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview a mapped import."),
    yes: bool = typer.Option(False, "--yes", help="Confirm a mapped import."),
) -> None:
    """Import cached translations from an Ayvu JSON export."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    try:
        if all_namespaces:
            raise CacheAdminError("invalid_scope")
        if scope_map is None and (dry_run or yes):
            raise CacheAdminError("invalid_scope_map")
        mapping = _cache_scope_map(scope_map) if scope_map is not None else None
        services = cache_admin_services()
        request = ImportRequest(CacheRef(str(cache_path)), FileRef(str(input_path)), replace, scope_map=mapping)
        if mapping is None:
            report = services.import_cache.execute(request)
        else:
            preview = services.preview_import.execute(request)
            _cache_warnings(preview)
            if preview.failure:
                raise CacheAdminError(preview.failure.code, preview.failure.family)
            completed = False
            try:
                console.print(f"Import preview: {preview.value.count} entries; {preview.value.imported} imported; "
                              f"{preview.value.skipped} skipped; {preview.value.replaced} replacements; "
                              f"{preview.value.conflicts} conflicts; {preview.value.duplicates} duplicates.")
                if dry_run:
                    return
                if not yes and not typer.confirm("Import these cache entries?"):
                    raise typer.Exit(code=1)
                report = services.execute_import.execute(ExecuteImportRequest(preview.value))
                completed = report.failure is None
            finally:
                if not completed:
                    discarded = services.discard_import.execute(preview.value)
                    _cache_warnings(discarded)
                    if discarded.failure:
                        console.print(f"[yellow]Cache warning:[/yellow] {discarded.failure.code}")
        _cache_warnings(report)
        if report.failure:
            raise CacheAdminError(report.failure.code, report.failure.family)
    except CacheAdminError as exc:
        _print_expected_error(
            "Não foi possível importar o cache.",
            _cache_failure_hint(exc.code, "Confirme que o arquivo foi exportado pelo Ayvu e tente novamente.",
                                "Exporte em partes menores com --source, --target ou --before e importe cada uma.", v2=scope_map is not None),
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc

    _print_cache_import_report(report.value)
