from __future__ import annotations
import ast
import os
import subprocess
import sys
from pathlib import Path
from tests.support.paths import PROJECT_ROOT

SOURCE = PROJECT_ROOT / "src" / "ayvu"


# Violations that exist today, each with the issue that removes it. An entry that no longer matches fails,
# so this list can only shrink.
# Empty at the start: the only package today, desktop/, is not reached into.
ALLOWED: dict[str, str] = {}


# Plain modules that are a public API by themselves: the composition roots (D5); the v1 modules, quarantined
# until REF-18 retires them (D7); and the adapters, which do I/O, so that importing a package's model and use
# cases never loads SQLite, HTTP or the filesystem code behind them (D2, like ayvu.kernel.sqlite).
ADAPTER_MODULES = frozenset({
    "network.http", "preferences.store", "preferences.config_facade", "provenance.store", "jobs.store",
    "artifacts.store", "artifacts.destination", "projects.store", "projects.schema", "projects.profiles.store",
    "projects.profiles.glossary_store", "library.adapters", "recovery.store", "recovery.checkpoint", "recovery.checkpoint_scan", "knowledge.cache.schema",
    "knowledge.cache.repository", "knowledge.cache.interchange", "knowledge.cache.admin_store",
    "knowledge.cache.admin_v2_queries", "knowledge.cache.admin_v2_files", "knowledge.cache.admin_v2_export",
    "knowledge.cache.admin_v2_records", "knowledge.cache.admin_v2_import",
    "knowledge.cache.legacy_admin_store", "knowledge.cache.admin_v2_store", "knowledge.memory.approved.store", "knowledge.glossary.catalog_store",
    "formats.local", "formats.epub.archive", "formats.epub.io", "formats.epub.adapter", "formats.epub.validation",
    "formats.markdown.writer", "providers.libretranslate.client", "providers.libretranslate.adapter",
    "providers.session", "translation.preflight", "translation.batch_output", "review.exchange_files",
    "review.local_files", "review.local_context", "review.local_packages", "review.local_start",
    "review.local_operations"})


PUBLIC_MODULES = frozenset({"composition.cli", "composition.desktop"} | {f"v1.{name}" for name in (
    "translation", "html_translate", "cache", "cache_check",
    "epub")} | ADAPTER_MODULES)


def packages(root: Path = SOURCE) -> set[str]:
    return {path.name for path in root.iterdir() if (path / "__init__.py").is_file()}


def exported(root: Path, package: str) -> set[str]:
    return exported_names(ast.parse((root / package / "__init__.py").read_text(encoding="utf-8")))


def exported_names(tree: ast.Module) -> set[str]:
    """The names a package's __init__.py exports: its __all__, or else every top-level binding."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "__all__" for t in node.targets):
            return {element.value for element in node.value.elts}
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def violations(root: Path = SOURCE, public_modules: frozenset[str] = PUBLIC_MODULES) -> dict[str, str]:
    """Maps "importer -> target" to a short reason, for every import that skips a package's public API."""
    roots = packages(root)
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(root).with_suffix("").parts
        module = parts[:-1] if parts[-1] == "__init__" else parts
        base = module if parts[-1] == "__init__" else module[:-1]
        importer = ".".join(module)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets: list[tuple[tuple[str, ...], list[str]]] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = base[:len(base) - node.level + 1] if node.level > 1 else base
                    target = anchor + (tuple(node.module.split(".")) if node.module else ())
                elif node.module and node.module.split(".")[0] == root.name:
                    target = tuple(node.module.split(".")[1:])
                else:
                    continue
                targets.append((target, [alias.name for alias in node.names]))
            elif isinstance(node, ast.Import):
                targets.extend((tuple(a.name.split(".")[1:]), []) for a in node.names if a.name.split(".")[0] == root.name)
            for target, names in targets:
                if not target or target[0] not in roots or (module and module[0] == target[0]):
                    continue
                key = f"{importer} -> {'.'.join(target)}"
                api = Path(root, *target)
                if ".".join(target) in public_modules:
                    tree = ast.parse(api.with_suffix(".py").read_text(encoding="utf-8"))
                    missing = sorted(set(names) - exported_names(tree) - {"*"})
                    if missing:
                        found[key] = f"imports names the module does not define: {', '.join(missing)}"
                elif not (api / "__init__.py").is_file():
                    found[key] = "imports a submodule of another package"
                else:
                    modules = {name for name in names if ".".join((*target, name)) in public_modules}
                    missing = sorted(set(names) - exported(root, "/".join(target)) - {"*"} - modules)
                    if missing:
                        found[key] = f"imports names the package does not export: {', '.join(missing)}"
    return found


def write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
