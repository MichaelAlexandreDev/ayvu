from __future__ import annotations
import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
import pytest
from tests.support.public_api_support import exported_names as package_exports
from tests.support.architecture_owners import OWNERS, Owner, REVIEW_BUILDERS
from tests.support.architecture_edges import ALLOWED_INTERNAL_EDGES
from tests.support.paths import PROJECT_ROOT, TESTS_ROOT

ROOT = PROJECT_ROOT


FIXTURES = TESTS_ROOT / "fixtures" / "architecture_boundaries"


class BoundaryError(AssertionError):
    pass


@dataclass(frozen=True)
class Exemption:
    owner: str
    removal_issue: int
    expiry: str


CLI_ROOT = "ayvu.composition.cli"  # the CLI construction root; review siblings also declare internal edges


# The synthetic fixtures still name the flat layout of #133, so their modules keep their owners here.
TEST_OWNERS = {**OWNERS, "ayvu.application": Owner("application", "Translation"),
               "ayvu.domain": Owner("domain", "Domain"),
               "ayvu.desktop": Owner("interface", "Desktop"), "ayvu.cli": Owner("interface", "CLI")}


def check_tree(root: Path, owners: dict[str, Owner],
               exceptions: dict[tuple[str, str], Exemption] | None = None, *,
               allowed_edges: set[tuple[str, str]] | None = None) -> None:
    exceptions = exceptions or {}
    allowed_edges = allowed_edges or set()
    _check_declarations(owners, exceptions, allowed_edges)
    modules = _read_modules(root, owners)
    used, used_allowed, graph, effective_graph = _check_edges(modules, owners, exceptions, allowed_edges)
    _check_cli_reachability(modules, owners, allowed_edges, effective_graph)
    _check_cli_root(modules)
    _check_acyclic(graph)
    if set(exceptions) != used:
        raise BoundaryError(f"unused exception: {sorted(set(exceptions) - used)}")
    if allowed_edges != used_allowed:
        raise BoundaryError(f"unused allowed edge: {sorted(allowed_edges - used_allowed)}")


def _check_declarations(owners, exceptions, allowed_edges):
    for edge, exemption in exceptions.items():
        if (len(edge) != 2 or not all(edge) or any("*" in part for part in edge)
                or not exemption.owner.strip() or exemption.removal_issue <= 0
                or not exemption.expiry.strip()):
            raise BoundaryError(f"invalid exception: {edge}")
    for edge in allowed_edges:
        if (len(edge) != 2 or not all(edge) or any("*" in part for part in edge)
                or edge[0] not in owners or edge[1] not in owners):
            raise BoundaryError(f"invalid allowed edge: {edge}")


def _read_modules(root, owners):
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

    return modules


def _check_edges(modules, owners, exceptions, allowed_edges):
    used: set[tuple[str, str]] = set()
    used_allowed: set[tuple[str, str]] = set()
    graph: dict[str, set[str]] = {}
    effective_graph: dict[str, set[str]] = {}
    for source, (_, package, tree) in modules.items():
        graph[source] = set()
        effective_graph[source] = set()
        for target in imports(source, package, tree, modules):
            effective_graph[source].add(target)
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
    return used, used_allowed, graph, effective_graph


def _check_cli_reachability(modules, owners, allowed_edges, effective_graph):
    # Reuse the same owners, import resolver and external dependency rules over
    # effective reachability. A reviewed edge exception cannot launder an adapter
    # through an application module or a re-export into CLI presentation.
    for source, owner in owners.items():
        if owner.layer != "interface" or owner.capability != "CLI":
            continue
        pending = list(effective_graph[source])
        visited = set()
        while pending:
            target = pending.pop()
            if target in visited:
                continue
            visited.add(target)
            if target == CLI_ROOT:
                continue
            if target in owners:
                if owners[target].layer not in (ALLOWED_LAYER_DEPENDENCIES["interface"] - {"composition"}) | {"interface"}:
                    raise BoundaryError(f"CLI transitive infrastructure: {source} -> {target}")
                pending.extend(effective_graph[target])
            elif forbidden(source, target, owners, allowed_edges):
                raise BoundaryError(f"CLI transitive vendor: {source} -> {target}")
        check_cli_construction_imports(source, modules)


def _check_cli_root(modules):
    if CLI_ROOT in modules:
        tree = modules[CLI_ROOT][2]
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and any(isinstance(part, (ast.Call, ast.Dict, ast.List, ast.Set)) for part in ast.walk(node)):
                raise BoundaryError("eager CLI resource or hidden singleton")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                raise BoundaryError("mutable CLI service locator or singleton")
            if isinstance(node, ast.FunctionDef) and (node.decorator_list or any(
                isinstance(part, (ast.Call, ast.Dict, ast.List, ast.Set))
                for default in [*node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)]
                for part in ast.walk(default)
            )):
                raise BoundaryError("cached or eager CLI construction")


def _check_acyclic(graph):
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


def check_cli_construction_imports(source, modules):
    """The presenter may import named factories, never an adapter re-export."""
    _, package, tree = modules[source]
    if CLI_ROOT not in modules:
        return
    root_tree = modules[CLI_ROOT][2]
    # Named factories, and the request and outcome types the root itself defines.
    factories = {node.name for node in root_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        targets = imports(source, package, ast.Module(body=[node], type_ignores=[]), modules)
        if CLI_ROOT not in targets:
            continue
        # Names imported from a root, never the root module itself under another name.
        if isinstance(node, ast.ImportFrom) and node.module and not any(
                target.endswith("." + alias.name) for target, alias in zip(targets, node.names)):
            if any(alias.name not in factories for alias in node.names):
                raise BoundaryError("CLI concrete re-export through composition")
        else:
            raise BoundaryError("CLI must inject named workflow factories")


LAYERS = {"surface", "domain", "application", "interface", "format", "provider",
          "knowledge", "infrastructure", "composition"}


EXTERNAL = {"typer", "rich", "requests", "urllib3", "ebooklib", "bs4", "lxml", "rapidfuzz",
            "PySide6", "PySide2", "PyQt6", "PyQt5", "mcp"}


TECHNICAL_STDLIB = {"sqlite3", "subprocess", "http", "urllib", "xml", "zipfile", "csv",
                    "shelve", "dbm", "socket", "socketserver", "ssl", "ftplib",
                    "smtplib", "imaplib", "poplib", "multiprocessing", "os", "shutil"}


PRESENTATION_STDLIB = {"tkinter", "curses", "turtle", "idlelib", "webbrowser"}


ALLOWED_LAYER_DEPENDENCIES = {
    "surface": set(), "domain": {"domain"},
    "application": {"domain", "application"},
    "interface": {"domain", "application", "surface", "composition"},
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
                    elif (modules[base][1] and alias.name != "__version__"
                          and alias.name not in package_exports(modules[base][2])):
                        # D2 of the package-structure decision: a package is used through what its
                        # __init__.py exports; any other name is an unknown member.
                        raise BoundaryError(f"unknown internal import: {child}")
                    else:
                        targets.append(reexported(base, alias.name, modules))
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


def reexported(package: str, name: str, modules: dict[str, tuple[Path, bool, ast.Module]]) -> str:
    """The submodule a package's __init__.py takes NAME from, so an edge keeps its real owner (D2)."""
    found = [f"{package}.{n.module}" for n in (modules[package][2].body if modules[package][1] else ())
             if isinstance(n, ast.ImportFrom) and n.level == 1 and any((a.asname or a.name) == name for a in n.names)]
    return found[0] if found and found[0] in modules else package


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
        if destination.layer == "composition" and (source == CLI_ROOT or {source, target} <= REVIEW_BUILDERS):
            return None if (source, target) in allowed_edges else "undeclared internal edge"
        if source != "ayvu" and target.startswith(source + "."):
            return None  # D2: a package's __init__.py re-exports its own submodules
        if destination.layer not in ALLOWED_LAYER_DEPENDENCIES[owner.layer]:
            return "dependency direction"
        if destination.capability == "Kernel":
            return None  # D4: every module may use the shared kernel
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
        "format": {"bs4", "lxml", "ebooklib"}, "provider": {"requests", "urllib3"},
        "composition": EXTERNAL,
    }.get(owner.layer, set())
    return None if root in allowed else "framework/vendor dependency"


EXCEPTIONS = {
    ("ayvu.v1.epub", "ayvu.v1.cache"): Exemption("Formats", 144, "EPUB adapter isolated from pipeline"),
    ("ayvu.v1.epub", "ayvu.knowledge.glossary.rules"): Exemption("Formats", 144, "glossary coordinated by pipeline"),
    ("ayvu.v1.epub", "ayvu.review.csv_export"): Exemption("Review", 158, "legacy EPUB review projection removed with the old facade"),
    ("ayvu.v1.epub", "ayvu.review.csv_import"): Exemption("Review", 158, "v1 refusal in apply_reviewed_epub removed with the old facade"),
    ("ayvu.v1.epub", "ayvu.providers.libretranslate.client"): Exemption("Formats", 144, "provider port separated by #141"),
    ("ayvu.v1.html_translate", "ayvu.v1.cache"): Exemption("Formats", 153, "compatibility facade retired with the versioned cache"),
    ("ayvu.v1.html_translate", "ayvu.knowledge.glossary.rules"): Exemption("Formats", 153, "compatibility facade retired with the versioned cache"),
    ("ayvu.v1.html_translate", "ayvu.providers.libretranslate.client"): Exemption("Formats", 153, "compatibility facade retired with the versioned cache"),
    ("ayvu.knowledge.memory.fuzzy", "rapidfuzz"): Exemption("Knowledge", 161, "TM matching behind repository boundary"),
}


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


# Pesquisa #167: registro separado, removível, nunca um módulo de produção.
LOCAL_WORKER_RESEARCH = frozenset({'probe.py', 'measure.py'})
