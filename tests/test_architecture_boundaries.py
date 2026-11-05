"""Static checks of the accepted ADR; never import the inspected modules."""
from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "architecture_boundaries"


class BoundaryError(AssertionError):
    pass


@dataclass(frozen=True)
class Owner:
    layer: str
    capability: str


@dataclass(frozen=True)
class Exemption:
    owner: str
    removal_issue: int
    expiry: str


OWNERS = {
    "ayvu": Owner("surface", "Package"),
    "ayvu.domain": Owner("domain", "Domain"),
    "ayvu.project": Owner("domain", "Projects"),
    "ayvu.project_service": Owner("application", "Projects"),
    "ayvu.project_store": Owner("infrastructure", "Projects"),
    "ayvu.chunking": Owner("domain", "Translation"),
    "ayvu.cli": Owner("interface", "CLI"),
    "ayvu.cli_progress": Owner("interface", "CLI"),
    "ayvu.preflight": Owner("composition", "Translation"),
    "ayvu.epub_io": Owner("format", "EPUB"),
    "ayvu.legacy_translation": Owner("format", "EPUB"),
    "ayvu.translation_engine": Owner("application", "Translation"),
    "ayvu.translate_project": Owner("application", "Translation"),
    "ayvu.translate_batch": Owner("application", "Translation"),
    "ayvu.batch_output_policy": Owner("infrastructure", "Translation"),
    "ayvu.translate_project_composition": Owner("composition", "Translation"),
    "ayvu.translate_batch_composition": Owner("composition", "Translation"),
    "ayvu.artifact_destination": Owner("infrastructure", "Artifacts"),
    "ayvu.epub_archive": Owner("format", "EPUB"),
    "ayvu.epub_format": Owner("format", "EPUB"),
    "ayvu.html_structure": Owner("format", "EPUB"),
    "ayvu.html_translate": Owner("format", "EPUB"),
    "ayvu.validation": Owner("format", "EPUB/Quality"),
    "ayvu.translator": Owner("provider", "Providers"),
    "ayvu.cache": Owner("knowledge", "Exact cache"),
    "ayvu.cache_admin": Owner("application", "Cache administration"),
    "ayvu.cache_admin_store": Owner("knowledge", "Cache administration"),
    "ayvu.cache_admin_composition": Owner("composition", "Cache administration"),
    "ayvu.glossary": Owner("knowledge", "Glossary"),
    "ayvu.translation_memory": Owner("domain", "Knowledge/TM"),
    "ayvu.config": Owner("infrastructure", "Preferences"),
    "ayvu.preferences": Owner("domain", "Preferences"),
    "ayvu.preferences_service": Owner("application", "Preferences"),
    "ayvu.preferences_store": Owner("infrastructure", "Preferences"),
    "ayvu.migration_contracts": Owner("domain", "Storage migrations"),
    "ayvu.sqlite_migrations": Owner("infrastructure", "Storage migrations"),
    "ayvu.project_schema": Owner("infrastructure", "Storage migrations"),
    "ayvu.job": Owner("domain", "Jobs"),
    "ayvu.job_service": Owner("application", "Jobs"),
    "ayvu.job_store": Owner("infrastructure", "Jobs"),
    "ayvu.job_events": Owner("domain", "Jobs"),
    "ayvu.job_execution": Owner("application", "Jobs"),
    "ayvu.legacy_resume": Owner("infrastructure", "Jobs"),
    "ayvu.job_recovery": Owner("application", "Jobs"),
    "ayvu.job_recovery_store": Owner("infrastructure", "Jobs"),
    "ayvu.artifact": Owner("domain", "Artifacts"),
    "ayvu.artifact_service": Owner("application", "Artifacts"),
    "ayvu.artifact_store": Owner("infrastructure", "Artifacts"),
    "ayvu.cache_identity": Owner("domain", "Translation cache identity"),
    "ayvu.content": Owner("domain", "Content"),
    "ayvu.translation_protection": Owner("domain", "Translation"),
    "ayvu.translation_pipeline": Owner("application", "Translation"),
    "ayvu.translation_plan": Owner("domain", "Translation"),
    "ayvu.translation_document": Owner("application", "Translation"),
    "ayvu.provider": Owner("domain", "Translation"),
    "ayvu.provider_capabilities": Owner("domain", "Translation"),
    "ayvu.provider_network": Owner("domain", "Translation"),
    "ayvu.format_adapter": Owner("domain", "Formats"),
    "ayvu.format_service": Owner("application", "Formats"),
    "ayvu.format_local": Owner("infrastructure", "Formats"),
    "ayvu.provider_composition": Owner("composition", "Providers"),
    "ayvu.provider_discovery": Owner("application", "Translation"),
    "ayvu.provider_service": Owner("application", "Translation"),
    "ayvu.provider_libretranslate": Owner("provider", "Providers"),
    "ayvu.provenance": Owner("domain", "Provenance"),
    "ayvu.provenance_service": Owner("application", "Provenance"),
    "ayvu.provenance_store": Owner("infrastructure", "Provenance"),
    "ayvu.resume": Owner("infrastructure", "Jobs"),
    "ayvu.review_export": Owner("infrastructure", "Review"),
    "ayvu.review_import": Owner("infrastructure", "Review"),
    "ayvu.review_exchange": Owner("infrastructure", "Review"),
    "ayvu.review_application": Owner("application", "Review"),
    "ayvu.review_composition": Owner("composition", "Review"),
    "ayvu.library_service": Owner("application", "Library"),
    "ayvu.library_adapters": Owner("infrastructure", "Library"),
    "ayvu.content_use_cases": Owner("application", "Formats"),
    "ayvu.markdown_writer": Owner("infrastructure", "Formats"),
    "ayvu.content_composition": Owner("composition", "Formats"),
}
TEST_OWNERS = {**OWNERS, "ayvu.application": Owner("application", "Translation"),
               "ayvu.desktop": Owner("interface", "Desktop")}


def check_tree(root: Path, owners: dict[str, Owner],
               exceptions: dict[tuple[str, str], Exemption] | None = None, *,
               allowed_edges: set[tuple[str, str]] | None = None) -> None:
    exceptions = exceptions or {}
    allowed_edges = allowed_edges or set()
    for edge, exemption in exceptions.items():
        if (len(edge) != 2 or not all(edge) or any("*" in part for part in edge)
                or not exemption.owner.strip() or exemption.removal_issue <= 0
                or not exemption.expiry.strip()):
            raise BoundaryError(f"invalid exception: {edge}")
    for edge in allowed_edges:
        if (len(edge) != 2 or not all(edge) or any("*" in part for part in edge)
                or edge[0] not in owners or edge[1] not in owners):
            raise BoundaryError(f"invalid allowed edge: {edge}")
    modules: dict[str, tuple[Path, bool, ast.Module]] = {}

    def unreadable(error: OSError) -> None:
        raise BoundaryError("unreadable production directory") from error

    if not root.is_dir() or root.is_symlink():
        raise BoundaryError("unreadable production root")
    for directory, directories, filenames in os.walk(root, onerror=unreadable):
        for name in directories + filenames:
            if (Path(directory) / name).is_symlink():
                raise BoundaryError("symlink in production tree")
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            parts = list(path.relative_to(root).with_suffix("").parts)
            package = parts[-1] == "__init__"
            if package:
                parts.pop()
            module = ".".join(["ayvu", *parts])
            if module not in owners:
                raise BoundaryError(f"unclassified production module: {module}")
            if owners[module].layer not in LAYERS or not owners[module].capability:
                raise BoundaryError(f"invalid owner: {module}")
            if module in modules:
                raise BoundaryError(f"ambiguous production module: {module}")
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=module)
            except (OSError, UnicodeError) as error:
                raise BoundaryError(f"unreadable module: {module}") from error
            except SyntaxError as error:
                raise BoundaryError(f"syntax error: {module}") from error
            modules[module] = (path, package, tree)
    if "ayvu" not in modules or set(owners) != set(modules):
        raise BoundaryError("missing classified production module")

    used: set[tuple[str, str]] = set()
    used_allowed: set[tuple[str, str]] = set()
    graph: dict[str, set[str]] = {}
    for source, (_, package, tree) in modules.items():
        graph[source] = set()
        for target in imports(source, package, tree, modules):
            if target in modules:
                graph[source].add(target)
            edge = (source, target)
            violation = forbidden(source, target, owners, allowed_edges)
            if violation:
                if edge not in exceptions:
                    raise BoundaryError(f"forbidden edge: {source} -> {target} ({violation})")
                used.add(edge)
            elif edge in allowed_edges:
                used_allowed.add(edge)
    for source in graph:
        pending = list(graph[source])
        visited: set[str] = set()
        while pending:
            target = pending.pop()
            if target == source:
                raise BoundaryError(f"cyclic production imports: {source}")
            if target not in visited:
                visited.add(target)
                pending.extend(graph[target])
    if set(exceptions) != used:
        raise BoundaryError(f"unused exception: {sorted(set(exceptions) - used)}")
    if allowed_edges != used_allowed:
        raise BoundaryError(f"unused allowed edge: {sorted(allowed_edges - used_allowed)}")


LAYERS = {"surface", "domain", "application", "interface", "format", "provider",
          "knowledge", "infrastructure", "composition"}
EXTERNAL = {"typer", "rich", "requests", "ebooklib", "bs4", "lxml", "rapidfuzz",
            "PySide6", "PySide2", "PyQt6", "PyQt5", "mcp"}
TECHNICAL_STDLIB = {"sqlite3", "subprocess", "http", "urllib", "xml", "zipfile", "csv",
                    "shelve", "dbm", "socket", "socketserver", "ssl", "ftplib",
                    "smtplib", "imaplib", "poplib", "multiprocessing", "os", "shutil"}
PRESENTATION_STDLIB = {"tkinter", "curses", "turtle", "idlelib", "webbrowser"}
ALLOWED_LAYER_DEPENDENCIES = {
    "surface": set(), "domain": {"domain"},
    "application": {"domain", "application"},
    "interface": {"domain", "application", "surface"},
    "format": {"domain", "application", "format"},
    "provider": {"domain", "application", "provider"},
    "knowledge": {"domain", "application", "knowledge"},
    "infrastructure": {"domain", "application", "infrastructure"},
    "composition": LAYERS - {"composition"},
}


def imports(source: str, package: bool, tree: ast.Module,
            modules: dict[str, tuple[Path, bool, ast.Module]]) -> list[str]:
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                raise BoundaryError(f"star import: {source}")
            base = node.module or ""
            if node.level:
                context = source.split(".") if package else source.split(".")[:-1]
                if node.level > len(context):
                    raise BoundaryError(f"relative import escapes package: {source}")
                context = context[:len(context) - node.level + 1]
                base = ".".join(context + ([base] if base else []))
            if base == "ayvu" or base.startswith("ayvu."):
                if base not in modules:
                    raise BoundaryError(f"unknown internal import: {base}")
                for alias in node.names:
                    child = base + "." + alias.name
                    if child in modules:
                        targets.append(child)
                    elif modules[base][1] and alias.name != "__version__":
                        raise BoundaryError(f"unknown internal import: {child}")
                    else:
                        targets.append(base)
            else:
                targets.append(base)
    for target in targets:
        root = target.split(".")[0]
        if root == "tests":
            raise BoundaryError("production imports tests")
        if root == "ayvu" and target not in modules:
            raise BoundaryError(f"unknown internal import: {target}")
        if root != "ayvu" and root not in sys.stdlib_module_names | EXTERNAL:
            raise BoundaryError(f"unknown external import: {target}")
    return targets


def forbidden(
    source: str,
    target: str,
    owners: dict[str, Owner],
    allowed_edges: set[tuple[str, str]] | None = None,
) -> str | None:
    allowed_edges = allowed_edges or set()
    owner = owners[source]
    if target in owners:
        destination = owners[target]
        if owner.layer == "interface" and destination.layer == "interface":
            if owner.capability != destination.capability:
                return "interfaces must not import one another"
            if (source, target) not in allowed_edges:
                return "undeclared internal edge"
            return None
        if destination.layer not in ALLOWED_LAYER_DEPENDENCIES[owner.layer]:
            return "dependency direction"
        if (source, target) not in allowed_edges:
            return "undeclared internal edge"
        return None
    root = target.split(".")[0]
    if root in sys.stdlib_module_names:
        if root in PRESENTATION_STDLIB and owner.layer not in {"interface", "composition"}:
            return "presentation dependency"
        if root in TECHNICAL_STDLIB and owner.layer in {"surface", "domain", "application", "interface"}:
            return "concrete infrastructure"
        return None
    allowed = {
        "interface": {"typer", "rich"} if owner.capability == "CLI" else
                     {"PySide6", "PySide2", "PyQt6", "PyQt5"},
        "format": {"bs4", "lxml", "ebooklib"}, "provider": {"requests"},
        "composition": EXTERNAL,
    }.get(owner.layer, set())
    return None if root in allowed else "framework/vendor dependency"


ALLOWED_INTERNAL_EDGES = {
    ("ayvu.translate_batch_composition", "ayvu.batch_output_policy"),
    ("ayvu.translate_batch_composition", "ayvu.translate_batch"),
    ("ayvu.translate_batch_composition", "ayvu.translate_project"),
    ("ayvu.translate_batch_composition", "ayvu.job"),
    ("ayvu.translate_batch_composition", "ayvu.job_events"),
    ("ayvu.translate_batch_composition", "ayvu.job_execution"),
    ("ayvu.translate_project_composition", "ayvu.job_execution"),
    ("ayvu.batch_output_policy", "ayvu.artifact"),
    ("ayvu.batch_output_policy", "ayvu.domain"),
    ("ayvu.cli", "ayvu.translate_batch"),
    ("ayvu.cli", "ayvu.job"),
    ("ayvu.cache_admin", "ayvu.job_events"),
    ("ayvu.cache_admin_store", "ayvu.cache_admin"),
    ("ayvu.cache_admin_store", "ayvu.cache"),
    ("ayvu.cache_admin_composition", "ayvu.cache_admin"),
    ("ayvu.cache_admin_composition", "ayvu.cache_admin_store"),
    ("ayvu.cli", "ayvu.cache_admin"),
    ("ayvu.cache_identity", "ayvu.job"),
    ("ayvu.cache_identity", "ayvu.provenance"),
    ("ayvu.translation_document", "ayvu.cache_identity"),
    ("ayvu.translation_pipeline", "ayvu.cache_identity"),
    ("ayvu.translation_plan", "ayvu.cache_identity"),
    ("ayvu.sqlite_migrations", "ayvu.migration_contracts"),
    ("ayvu.project_schema", "ayvu.job"),
    ("ayvu.project_schema", "ayvu.artifact"),
    ("ayvu.project_schema", "ayvu.artifact_service"),
    ("ayvu.project_schema", "ayvu.artifact_store"),
    ("ayvu.artifact", "ayvu.job"),
    ("ayvu.artifact_service", "ayvu.artifact"),
    ("ayvu.artifact_service", "ayvu.job_execution"),
    ("ayvu.artifact_store", "ayvu.artifact"),
    ("ayvu.artifact_store", "ayvu.artifact_service"),
    ("ayvu.artifact_store", "ayvu.job"),
    ("ayvu.artifact_store", "ayvu.job_store"),
    ("ayvu.artifact_store", "ayvu.migration_contracts"),
    ("ayvu.artifact_store", "ayvu.sqlite_migrations"),
    ("ayvu.legacy_resume", "ayvu.resume"),
    ("ayvu.provider", "ayvu.content"),
    ("ayvu.provider", "ayvu.job"),
    ("ayvu.provider", "ayvu.job_events"),
    ("ayvu.provider", "ayvu.provenance"),
    ("ayvu.provider_capabilities", "ayvu.job"),
    ("ayvu.format_adapter", "ayvu.artifact"),
    ("ayvu.format_adapter", "ayvu.content"),
    ("ayvu.format_adapter", "ayvu.job"),
    ("ayvu.format_adapter", "ayvu.job_events"),
    ("ayvu.format_service", "ayvu.artifact"),
    ("ayvu.format_service", "ayvu.artifact_service"),
    ("ayvu.format_service", "ayvu.content"),
    ("ayvu.format_service", "ayvu.format_adapter"),
    ("ayvu.format_service", "ayvu.job"),
    ("ayvu.format_service", "ayvu.job_events"),
    ("ayvu.format_service", "ayvu.job_execution"),
    ("ayvu.format_local", "ayvu.format_adapter"),
    ("ayvu.format_local", "ayvu.format_service"),
    ("ayvu.content_use_cases", "ayvu.format_adapter"),
    ("ayvu.content_use_cases", "ayvu.format_service"),
    ("ayvu.content_use_cases", "ayvu.job"),
    ("ayvu.content_use_cases", "ayvu.job_events"),
    ("ayvu.content_use_cases", "ayvu.job_execution"),
    ("ayvu.markdown_writer", "ayvu.content_use_cases"),
    ("ayvu.markdown_writer", "ayvu.format_adapter"),
    ("ayvu.markdown_writer", "ayvu.format_local"),
    ("ayvu.markdown_writer", "ayvu.format_service"),
    ("ayvu.markdown_writer", "ayvu.job_events"),
    ("ayvu.content_composition", "ayvu.content_use_cases"),
    ("ayvu.content_composition", "ayvu.epub_format"),
    ("ayvu.content_composition", "ayvu.format_local"),
    ("ayvu.content_composition", "ayvu.format_service"),
    ("ayvu.content_composition", "ayvu.markdown_writer"),
    ("ayvu.provider_network", "ayvu.job"),
    ("ayvu.provider_network", "ayvu.provider"),
    ("ayvu.provider_discovery", "ayvu.job"),
    ("ayvu.provider_discovery", "ayvu.job_events"),
    ("ayvu.provider_discovery", "ayvu.job_execution"),
    ("ayvu.provider_discovery", "ayvu.provenance"),
    ("ayvu.provider_discovery", "ayvu.provider"),
    ("ayvu.provider_discovery", "ayvu.provider_capabilities"),
    ("ayvu.provider_discovery", "ayvu.provider_network"),
    ("ayvu.provider_discovery", "ayvu.provider_service"),
    ("ayvu.provider_libretranslate", "ayvu.provider_discovery"),
    ("ayvu.provider_capabilities", "ayvu.provider"),
    ("ayvu.provider_service", "ayvu.provider_capabilities"),
    ("ayvu.provider_libretranslate", "ayvu.provider_capabilities"),
    ("ayvu.provider_service", "ayvu.job"),
    ("ayvu.provider_service", "ayvu.job_events"),
    ("ayvu.provider_service", "ayvu.provenance"),
    ("ayvu.provider_service", "ayvu.provider"),
    ("ayvu.provider_libretranslate", "ayvu.job_events"),
    ("ayvu.provider_libretranslate", "ayvu.provenance"),
    ("ayvu.provider_libretranslate", "ayvu.provider"),
    ("ayvu.provider_libretranslate", "ayvu.provider_service"),
    ("ayvu.provider_libretranslate", "ayvu.translator"),
    ("ayvu.project_schema", "ayvu.provenance"),
    ("ayvu.project_schema", "ayvu.provenance_store"),
    ("ayvu.provenance", "ayvu.job"),
    ("ayvu.provenance_service", "ayvu.provenance"),
    ("ayvu.provenance_store", "ayvu.job"),
    ("ayvu.provenance_store", "ayvu.job_store"),
    ("ayvu.provenance_store", "ayvu.migration_contracts"),
    ("ayvu.provenance_store", "ayvu.provenance"),
    ("ayvu.provenance_store", "ayvu.provenance_service"),
    ("ayvu.provenance_store", "ayvu.sqlite_migrations"),
    ("ayvu.project_schema", "ayvu.job_store"),
    ("ayvu.project_schema", "ayvu.sqlite_migrations"),
    ("ayvu.review_application", "ayvu.artifact"),
    ("ayvu.review_application", "ayvu.job"),
    ("ayvu.review_application", "ayvu.job_events"),
    ("ayvu.review_application", "ayvu.job_execution"),
    ("ayvu.review_application", "ayvu.translate_project"),
    ("ayvu.review_composition", "ayvu.artifact"),
    ("ayvu.review_composition", "ayvu.artifact_destination"),
    ("ayvu.review_composition", "ayvu.artifact_service"),
    ("ayvu.review_composition", "ayvu.artifact_store"),
    ("ayvu.review_composition", "ayvu.content"),
    ("ayvu.review_composition", "ayvu.epub_format"),
    ("ayvu.review_composition", "ayvu.format_adapter"),
    ("ayvu.review_composition", "ayvu.format_local"),
    ("ayvu.review_composition", "ayvu.format_service"),
    ("ayvu.review_composition", "ayvu.html_structure"),
    ("ayvu.review_composition", "ayvu.job"),
    ("ayvu.review_composition", "ayvu.job_events"),
    ("ayvu.review_composition", "ayvu.job_execution"),
    ("ayvu.review_composition", "ayvu.job_service"),
    ("ayvu.review_composition", "ayvu.job_store"),
    ("ayvu.review_composition", "ayvu.project"),
    ("ayvu.review_composition", "ayvu.project_schema"),
    ("ayvu.review_composition", "ayvu.project_store"),
    ("ayvu.review_composition", "ayvu.provenance"),
    ("ayvu.review_composition", "ayvu.provenance_store"),
    ("ayvu.review_composition", "ayvu.review_application"),
    ("ayvu.review_composition", "ayvu.review_exchange"),
    ("ayvu.review_composition", "ayvu.review_import"),
    ("ayvu.review_composition", "ayvu.translation_engine"),
    ("ayvu.review_composition", "ayvu.translate_project"),
    ("ayvu.job_service", "ayvu.job"),
    ("ayvu.job_events", "ayvu.job"),
    ("ayvu.job_execution", "ayvu.job"),
    ("ayvu.job_execution", "ayvu.job_events"),
    ("ayvu.job_store", "ayvu.job"),
    ("ayvu.job_store", "ayvu.job_service"),
    ("ayvu.job_store", "ayvu.migration_contracts"),
    ("ayvu.job_store", "ayvu.sqlite_migrations"),
    ("ayvu.config", "ayvu.preferences"),
    ("ayvu.config", "ayvu.preferences_service"),
    ("ayvu.config", "ayvu.preferences_store"),
    ("ayvu.preferences_service", "ayvu.preferences"),
    ("ayvu.preferences_store", "ayvu.preferences"),
    ("ayvu.preferences_store", "ayvu.preferences_service"),
    ("ayvu.cli", "ayvu.project"),
    ("ayvu.cli", "ayvu.project_service"),
    ("ayvu.project_service", "ayvu.project"),
    ("ayvu.project_store", "ayvu.project"),
    ("ayvu.project_store", "ayvu.project_service"),
    ("ayvu.cache", "ayvu.domain"),
    ("ayvu.cli", "ayvu.cli_progress"),
    ("ayvu.cli", "ayvu.translate_project"),
    ("ayvu.cli", "ayvu.translation_engine"),
    ("ayvu.cli", "ayvu.review_application"),
    ("ayvu.cli", "ayvu.domain"),
    ("ayvu.cli", "ayvu.library_service"),
    ("ayvu.library_adapters", "ayvu.library_service"),
    ("ayvu.cli", "ayvu.provider_discovery"),
    ("ayvu.preflight", "ayvu.provider_discovery"),
    ("ayvu.provider_composition", "ayvu.provider_capabilities"),
    ("ayvu.provider_composition", "ayvu.provider_discovery"),
    ("ayvu.provider_composition", "ayvu.provider_libretranslate"),
    ("ayvu.provider_composition", "ayvu.provider_network"),
    ("ayvu.provider_composition", "ayvu.provider_service"),
    ("ayvu.provider_composition", "ayvu.translator"),
    ("ayvu.epub_io", "ayvu.domain"),
    ("ayvu.epub_io", "ayvu.epub_archive"),
    ("ayvu.legacy_translation", "ayvu.content"),
    ("ayvu.legacy_translation", "ayvu.domain"),
    ("ayvu.legacy_translation", "ayvu.epub_archive"),
    ("ayvu.legacy_translation", "ayvu.epub_io"),
    ("ayvu.legacy_translation", "ayvu.format_service"),
    ("ayvu.legacy_translation", "ayvu.html_structure"),
    ("ayvu.legacy_translation", "ayvu.html_translate"),
    ("ayvu.legacy_translation", "ayvu.translation_engine"),
    ("ayvu.legacy_translation", "ayvu.translation_memory"),
    ("ayvu.translation_engine", "ayvu.artifact"),
    ("ayvu.translation_engine", "ayvu.content"),
    ("ayvu.translation_engine", "ayvu.domain"),
    ("ayvu.translation_engine", "ayvu.format_adapter"),
    ("ayvu.translation_engine", "ayvu.format_service"),
    ("ayvu.translation_engine", "ayvu.job_events"),
    ("ayvu.translation_engine", "ayvu.job_execution"),
    ("ayvu.job_recovery", "ayvu.job"),
    ("ayvu.job_recovery", "ayvu.job_execution"),
    ("ayvu.job_recovery", "ayvu.job_service"),
    ("ayvu.job_recovery", "ayvu.translate_project"),
    ("ayvu.job_recovery_store", "ayvu.artifact"),
    ("ayvu.job_recovery_store", "ayvu.artifact_store"),
    ("ayvu.job_recovery_store", "ayvu.domain"),
    ("ayvu.job_recovery_store", "ayvu.job"),
    ("ayvu.job_recovery_store", "ayvu.job_execution"),
    ("ayvu.job_recovery_store", "ayvu.job_recovery"),
    ("ayvu.job_recovery_store", "ayvu.job_service"),
    ("ayvu.job_recovery_store", "ayvu.job_store"),
    ("ayvu.job_recovery_store", "ayvu.legacy_resume"),
    ("ayvu.job_recovery_store", "ayvu.project"),
    ("ayvu.job_recovery_store", "ayvu.project_schema"),
    ("ayvu.job_recovery_store", "ayvu.project_store"),
    ("ayvu.job_recovery_store", "ayvu.provenance"),
    ("ayvu.job_recovery_store", "ayvu.provenance_store"),
    ("ayvu.job_recovery_store", "ayvu.resume"),
    ("ayvu.job_recovery_store", "ayvu.translate_project"),
    ("ayvu.cli", "ayvu.job_recovery"),
    ("ayvu.translate_project_composition", "ayvu.resume"),
    ("ayvu.translate_project_composition", "ayvu.job_recovery"),
    ("ayvu.translate_project_composition", "ayvu.job_recovery_store"),
    ("ayvu.translate_project", "ayvu.artifact"),
    ("ayvu.translate_project", "ayvu.domain"),
    ("ayvu.translate_project", "ayvu.format_adapter"),
    ("ayvu.translate_project", "ayvu.format_service"),
    ("ayvu.translate_project", "ayvu.job"),
    ("ayvu.translate_project", "ayvu.job_events"),
    ("ayvu.translate_project", "ayvu.job_execution"),
    ("ayvu.translate_project", "ayvu.job_service"),
    ("ayvu.translate_project", "ayvu.project"),
    ("ayvu.translate_project", "ayvu.provenance"),
    ("ayvu.translate_project", "ayvu.translation_engine"),
    ("ayvu.artifact_destination", "ayvu.artifact"),
    ("ayvu.artifact_destination", "ayvu.artifact_service"),
    ("ayvu.artifact_destination", "ayvu.artifact_store"),
    ("ayvu.artifact_destination", "ayvu.format_local"),
    ("ayvu.artifact_destination", "ayvu.format_service"),
    ("ayvu.artifact_destination", "ayvu.job_execution"),
    ("ayvu.preflight", "ayvu.cache"),
    ("ayvu.preflight", "ayvu.domain"),
    ("ayvu.preflight", "ayvu.epub_io"),
    ("ayvu.preflight", "ayvu.glossary"),
    ("ayvu.preflight", "ayvu.translator"),
    ("ayvu.translate_project_composition", "ayvu.artifact"),
    ("ayvu.translate_project_composition", "ayvu.artifact_destination"),
    ("ayvu.translate_project_composition", "ayvu.job"),
    ("ayvu.translate_project_composition", "ayvu.project"),
    ("ayvu.translate_project_composition", "ayvu.provenance"),
    ("ayvu.translate_project_composition", "ayvu.domain"),
    ("ayvu.translate_project_composition", "ayvu.epub_format"),
    ("ayvu.translate_project_composition", "ayvu.epub_io"),
    ("ayvu.translate_project_composition", "ayvu.format_local"),
    ("ayvu.translate_project_composition", "ayvu.format_service"),
    ("ayvu.translate_project_composition", "ayvu.job_events"),
    ("ayvu.translate_project_composition", "ayvu.legacy_translation"),
    ("ayvu.translate_project_composition", "ayvu.project_schema"),
    ("ayvu.translate_project_composition", "ayvu.project_store"),
    ("ayvu.translate_project_composition", "ayvu.translate_project"),
    ("ayvu.translate_project_composition", "ayvu.translation_engine"),
    ("ayvu.translate_project_composition", "ayvu.translation_memory"),
    ("ayvu.translate_project_composition", "ayvu.translator"),
    ("ayvu.translate_project_composition", "ayvu.validation"),
    ("ayvu.epub_io", "ayvu.html_translate"),
    ("ayvu.epub_io", "ayvu.translation_memory"),
    ("ayvu.review_exchange", "ayvu.artifact"),
    ("ayvu.review_exchange", "ayvu.content"),
    ("ayvu.review_exchange", "ayvu.format_adapter"),
    ("ayvu.review_exchange", "ayvu.project"),
    ("ayvu.review_exchange", "ayvu.translation_protection"),
    ("ayvu.validation", "ayvu.epub_archive"),
    ("ayvu.epub_format", "ayvu.content"),
    ("ayvu.epub_format", "ayvu.epub_archive"),
    ("ayvu.epub_format", "ayvu.format_adapter"),
    ("ayvu.epub_format", "ayvu.format_service"),
    ("ayvu.epub_format", "ayvu.html_structure"),
    ("ayvu.epub_format", "ayvu.job_events"),
    ("ayvu.html_structure", "ayvu.content"),
    ("ayvu.html_structure", "ayvu.format_adapter"),
    ("ayvu.html_structure", "ayvu.translation_protection"),
    ("ayvu.html_translate", "ayvu.html_structure"),
    ("ayvu.html_translate", "ayvu.translation_pipeline"),
    ("ayvu.html_translate", "ayvu.translation_protection"),
    ("ayvu.translation_pipeline", "ayvu.chunking"),
    ("ayvu.translation_pipeline", "ayvu.translation_plan"),
    ("ayvu.translation_plan", "ayvu.provenance"),
    ("ayvu.translation_plan", "ayvu.provider"),
    ("ayvu.translation_plan", "ayvu.provider_capabilities"),
    ("ayvu.translation_document", "ayvu.artifact"),
    ("ayvu.translation_document", "ayvu.artifact_service"),
    ("ayvu.translation_document", "ayvu.content"),
    ("ayvu.translation_document", "ayvu.domain"),
    ("ayvu.translation_document", "ayvu.job"),
    ("ayvu.translation_document", "ayvu.job_events"),
    ("ayvu.translation_document", "ayvu.job_execution"),
    ("ayvu.translation_document", "ayvu.provenance"),
    ("ayvu.translation_document", "ayvu.provenance_service"),
    ("ayvu.translation_document", "ayvu.provider"),
    ("ayvu.translation_document", "ayvu.provider_service"),
    ("ayvu.translation_document", "ayvu.translation_memory"),
    ("ayvu.translation_document", "ayvu.translation_pipeline"),
    ("ayvu.translation_document", "ayvu.translation_plan"),
    ("ayvu.translation_document", "ayvu.translation_protection"),
    ("ayvu.translation_pipeline", "ayvu.domain"),
    ("ayvu.translation_pipeline", "ayvu.translation_memory"),
    ("ayvu.translation_pipeline", "ayvu.translation_protection"),
    ("ayvu.html_translate", "ayvu.domain"),
    ("ayvu.html_translate", "ayvu.translation_memory"),
    ("ayvu.preflight", "ayvu.domain"),
    ("ayvu.resume", "ayvu.domain"),
    ("ayvu.translation_memory", "ayvu.domain"),
    ("ayvu.validation", "ayvu.html_translate"),
}


EXCEPTIONS = {
    ("ayvu.cli", "ayvu.translate_batch_composition"): Exemption("Translation", 158, "batch service composed at the CLI root"),
    ("ayvu.cli", "ayvu.batch_output_policy"): Exemption("Translation", 158, "batch output policy composed at CLI root"),
    ("ayvu.cli", "ayvu.cache_admin_composition"): Exemption("Cache administration", 158, "cache administration composed at the root"),
    ("ayvu.cli", "ayvu.project_store"): Exemption(
        "Projects", 158, "project repository constructed in the shared composition root"),
    ("ayvu.cli", "sqlite3"): Exemption("CLI", 158, "CLI uses application outcomes"),
    ("ayvu.cli", "ayvu.cache"): Exemption("Knowledge", 150, "cache administration extracted"),
    ("ayvu.cli", "ayvu.config"): Exemption("Preferences", 158, "compatibility config converted at shared composition root"),
    ("ayvu.cli", "ayvu.content_composition"): Exemption("Formats", 147, "inspect and export use cases composed at the root"),
    ("ayvu.cli", "ayvu.review_composition"): Exemption("Review", 158, "review use cases composed at the shared application root"),
    ("ayvu.cli", "ayvu.glossary"): Exemption("Knowledge", 157, "glossary use cases extracted"),
    ("ayvu.cli", "ayvu.html_translate"): Exemption("CLI", 158, "neutral statistics outcome"),
    ("ayvu.cli", "ayvu.preflight"): Exemption("Translation", 158, "built in the shared composition root"),
    ("ayvu.preflight", "ayvu.provider_composition"): Exemption("Translation", 158, "built in the shared composition root"),
    ("ayvu.cli", "ayvu.translate_project_composition"): Exemption("Translation", 158, "built in the shared composition root"),
    ("ayvu.cli", "ayvu.provider_composition"): Exemption("Providers", 158, "provider built in the shared composition root"),
    ("ayvu.epub_io", "ayvu.cache"): Exemption("Formats", 144, "EPUB adapter isolated from pipeline"),
    ("ayvu.epub_io", "ayvu.glossary"): Exemption("Formats", 144, "glossary coordinated by pipeline"),
    ("ayvu.epub_io", "ayvu.review_export"): Exemption("Review", 158, "legacy EPUB review projection removed with the old facade"),
    ("ayvu.epub_io", "ayvu.review_import"): Exemption("Review", 158, "v1 refusal in apply_reviewed_epub removed with the old facade"),
    ("ayvu.epub_io", "ayvu.translator"): Exemption("Formats", 144, "provider port separated by #141"),
    ("ayvu.html_translate", "ayvu.cache"): Exemption("Formats", 153, "compatibility facade retired with the versioned cache"),
    ("ayvu.html_translate", "ayvu.glossary"): Exemption("Formats", 153, "compatibility facade retired with the versioned cache"),
    ("ayvu.html_translate", "ayvu.translator"): Exemption("Formats", 153, "compatibility facade retired with the versioned cache"),
    ("ayvu.translation_memory", "rapidfuzz"): Exemption("Knowledge", 161, "TM matching behind repository boundary"),
    ("ayvu.translation_memory", "ayvu.cache"): Exemption("Knowledge", 161, "approved TM separated from exact cache"),
    ("ayvu.cli", "ayvu.library_adapters"): Exemption("Library", 158, "library adapters composed at the shared application root"),
}


def test_real_production_tree() -> None:
    check_tree(
        ROOT / "src" / "ayvu",
        OWNERS,
        EXCEPTIONS,
        allowed_edges=ALLOWED_INTERNAL_EDGES,
    )


@pytest.mark.parametrize("case", json.loads((FIXTURES / "negative.json").read_text()))
def test_forbidden_edges(case: dict[str, str], tmp_path: Path) -> None:
    root = make_tree(tmp_path, case["source"], case["code"])
    with pytest.raises(BoundaryError, match=case["error"]):
        check_tree(root, TEST_OWNERS)


@pytest.mark.parametrize("code", [
    "import ayvu.domain", "import ayvu.domain as values",
    "from ayvu.domain import LanguagePair as Pair", "from .domain import LanguagePair",
    "from ayvu import domain as values", "from . import domain",
])
def test_equivalent_allowed_imports(code: str, tmp_path: Path) -> None:
    check_tree(
        make_tree(tmp_path, "ayvu.application", code),
        TEST_OWNERS,
        allowed_edges={("ayvu.application", "ayvu.domain")},
    )


@pytest.mark.parametrize(("source", "code"), [
    ("ayvu.epub_io", "from ayvu.application import FormatPort"),
    ("ayvu.cache", "from ayvu.application import CacheRepositoryPort"),
])
def test_adapters_may_import_application_ports(
    source: str, code: str, tmp_path: Path,
) -> None:
    check_tree(
        make_tree(tmp_path, source, code),
        TEST_OWNERS,
        allowed_edges={(source, "ayvu.application")},
    )


@pytest.mark.parametrize(("source", "code"), [
    ("ayvu.domain", "import ayvu.chunking"),
    ("ayvu.epub_io", "import ayvu.application"),
])
def test_undeclared_internal_edges_fail_closed(
    source: str, code: str, tmp_path: Path,
) -> None:
    with pytest.raises(BoundaryError, match="undeclared internal edge"):
        check_tree(make_tree(tmp_path, source, code), TEST_OWNERS)


def test_same_interface_capability_requires_declared_edge(tmp_path: Path) -> None:
    owners = {**TEST_OWNERS, "ayvu.cli_helpers": Owner("interface", "CLI")}
    edge = ("ayvu.cli_helpers", "ayvu.cli")
    root = make_tree(tmp_path, edge[0], "import ayvu.cli", owners)
    with pytest.raises(BoundaryError, match="undeclared internal edge"):
        check_tree(root, owners)
    check_tree(root, owners, allowed_edges={edge})


@pytest.mark.parametrize(("source", "package", "code", "expected"), [
    ("ayvu.application", False, "import ayvu.domain", ["ayvu.domain"]),
    ("ayvu.application", False, "import ayvu.domain as values", ["ayvu.domain"]),
    ("ayvu.application", False,
     "from ayvu.domain import LanguagePair as Pair", ["ayvu.domain"]),
    ("ayvu.application", False,
     "from .domain import LanguagePair", ["ayvu.domain"]),
    ("ayvu.application", False, "from ayvu import domain as values", ["ayvu.domain"]),
    ("ayvu.application", False, "from . import domain", ["ayvu.domain"]),
    ("ayvu.application", False,
     "import ayvu.domain, ayvu.chunking", ["ayvu.domain", "ayvu.chunking"]),
    ("ayvu.nested.service", False,
     "from ..domain import LanguagePair", ["ayvu.domain"]),
])
def test_import_resolver_returns_exact_targets(
    source: str, package: bool, code: str, expected: list[str],
) -> None:
    empty = ast.parse("")
    modules = {
        "ayvu": (Path(), True, empty),
        "ayvu.domain": (Path(), False, empty),
        "ayvu.chunking": (Path(), False, empty),
        source: (Path(), package, ast.parse(code)),
    }
    assert imports(source, package, modules[source][2], modules) == expected


def test_multilevel_relative_import(tmp_path: Path) -> None:
    owners = {**TEST_OWNERS, "ayvu.nested": Owner("surface", "Translation"),
              "ayvu.nested.service": Owner("application", "Translation")}
    root = make_tree(tmp_path, "ayvu.nested.service", "from ..domain import LanguagePair", owners)
    check_tree(
        root,
        owners,
        allowed_edges={("ayvu.nested.service", "ayvu.domain")},
    )


def test_unknown_module_fails_closed(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")
    (root / "surprise.py").write_text("", encoding="utf-8")
    with pytest.raises(BoundaryError, match="unclassified"):
        check_tree(root, TEST_OWNERS)


def test_cyclic_domain_imports_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "import ayvu.chunking")
    (root / "chunking.py").write_text("import ayvu.domain", encoding="utf-8")
    with pytest.raises(BoundaryError, match="cyclic"):
        check_tree(
            root,
            TEST_OWNERS,
            allowed_edges={
                ("ayvu.domain", "ayvu.chunking"),
                ("ayvu.chunking", "ayvu.domain"),
            },
        )


def test_unknown_package_member_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.application", "from ayvu import missing")
    with pytest.raises(BoundaryError, match="unknown internal"):
        check_tree(root, TEST_OWNERS)


def test_public_surface_cannot_hide_adapter(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.cli", "from ayvu import __version__")
    allowed_edges = {("ayvu.cli", "ayvu")}
    check_tree(root, TEST_OWNERS, allowed_edges=allowed_edges)
    (root / "__init__.py").write_text("from .translator import Translator", encoding="utf-8")
    with pytest.raises(BoundaryError, match="ayvu -> ayvu.translator"):
        check_tree(root, TEST_OWNERS, allowed_edges=allowed_edges)


def test_new_subpackage_needs_explicit_owner(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")
    package = root / "new_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(BoundaryError, match="unclassified"):
        check_tree(root, TEST_OWNERS)


def test_missing_classified_module_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")
    (root / "domain.py").unlink()
    with pytest.raises(BoundaryError, match="missing classified"):
        check_tree(root, TEST_OWNERS)


@pytest.mark.parametrize("kind", ["file", "directory", "root"])
def test_symlinks_rejected(tmp_path: Path, kind: str) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")
    if kind == "root":
        link = tmp_path / "linked"
        link.symlink_to(root, target_is_directory=True)
        root = link
        message = "unreadable production root"
    else:
        target = root / ("domain.py" if kind == "file" else "__pycache__")
        if kind == "directory":
            target.mkdir()
        (root / "linked").symlink_to(target, target_is_directory=kind == "directory")
        message = "symlink"
    with pytest.raises(BoundaryError, match=message):
        check_tree(root, TEST_OWNERS)


def test_module_package_collision_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")
    (root / "domain").mkdir()
    (root / "domain" / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(BoundaryError, match="ambiguous"):
        check_tree(root, TEST_OWNERS)


def test_directory_read_failure_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")

    def walk(path: Path, *, onerror: object) -> list[object]:
        onerror(OSError("synthetic directory failure"))
        return []

    monkeypatch.setattr(os, "walk", walk)
    with pytest.raises(BoundaryError, match="unreadable production directory"):
        check_tree(root, TEST_OWNERS)


def test_unreadable_module_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_tree(tmp_path, "ayvu.domain", "")
    original = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if path == root / "domain.py":
            raise OSError("synthetic unreadable module")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(BoundaryError, match="unreadable"):
        check_tree(root, TEST_OWNERS)


def test_exception_is_exact_and_expires(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.application", "import ayvu.cache")
    exceptions = {("ayvu.application", "ayvu.cache"): Exemption("Translation", 145, "use port")}
    check_tree(root, TEST_OWNERS, exceptions)
    (root / "application.py").write_text("import ayvu.cache\nimport ayvu.translator", encoding="utf-8")
    with pytest.raises(BoundaryError, match="forbidden"):
        check_tree(root, TEST_OWNERS, exceptions)
    (root / "application.py").write_text("", encoding="utf-8")
    with pytest.raises(BoundaryError, match="unused exception"):
        check_tree(root, TEST_OWNERS, exceptions)


@pytest.mark.parametrize("exemption", [Exemption("", 145, "use port"),
    Exemption("Translation", 0, "use port"), Exemption("Translation", 145, "")])
def test_incomplete_exception_rejected(exemption: Exemption, tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.application", "import ayvu.cache")
    with pytest.raises(BoundaryError, match="invalid exception"):
        check_tree(root, TEST_OWNERS, {("ayvu.application", "ayvu.cache"): exemption})


@pytest.mark.parametrize("edge", [
    ("", "ayvu.domain"),
    ("ayvu.*", "ayvu.domain"),
    ("ayvu.application", "ayvu.missing"),
])
def test_invalid_allowed_edge_rejected(
    edge: tuple[str, str], tmp_path: Path,
) -> None:
    root = make_tree(tmp_path, "ayvu.application", "")
    with pytest.raises(BoundaryError, match="invalid allowed edge"):
        check_tree(root, TEST_OWNERS, allowed_edges={edge})


def test_unused_allowed_edge_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.application", "import ayvu.domain")
    with pytest.raises(BoundaryError, match="unused allowed edge"):
        check_tree(
            root,
            TEST_OWNERS,
            allowed_edges={
                ("ayvu.application", "ayvu.domain"),
                ("ayvu.epub_io", "ayvu.domain"),
            },
        )


def test_no_desktop_dependencies_required() -> None:
    project = __import__("tomllib").loads((ROOT / "pyproject.toml").read_text())
    standard_dependencies = [
        *project["project"]["dependencies"],
        *project["project"].get("optional-dependencies", {}).get("dev", []),
        *project.get("dependency-groups", {}).get("dev", []),
    ]
    assert not any("pyside" in dependency.lower() or "pyqt" in dependency.lower()
                   for dependency in standard_dependencies if isinstance(dependency, str))


def make_tree(tmp_path: Path, source: str, code: str,
              owners: dict[str, Owner] | None = None) -> Path:
    root = tmp_path / "ayvu"
    for module in owners or TEST_OWNERS:
        parts = module.split(".")[1:]
        is_package = not parts or any(other.startswith(module + ".") for other in owners or TEST_OWNERS)
        path = root.joinpath(*parts, "__init__.py") if is_package else root.joinpath(*parts).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code if module == source else "", encoding="utf-8")
    return root
