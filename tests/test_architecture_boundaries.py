"""Static checks of the accepted ADR; never import the inspected modules."""
from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_public_api import exported_names as package_exports
from support.architecture_owners import OWNERS, Owner
from support.architecture_edges import ALLOWED_INTERNAL_EDGES


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "architecture_boundaries"


class BoundaryError(AssertionError):
    pass


@dataclass(frozen=True)
class Exemption:
    owner: str
    removal_issue: int
    expiry: str


CLI_ROOT = "ayvu.composition.cli"  # the only composition module that may import another, by a declared edge

# The synthetic fixtures still name the flat layout of #133, so their modules keep their owners here.
TEST_OWNERS = {**OWNERS, "ayvu.application": Owner("application", "Translation"),
               "ayvu.domain": Owner("domain", "Domain"),
               "ayvu.desktop": Owner("interface", "Desktop"), "ayvu.cli": Owner("interface", "CLI")}


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
        if source == CLI_ROOT and destination.layer == "composition":
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


def test_real_production_tree() -> None:
    check_tree(ROOT / "src" / "ayvu", OWNERS, EXCEPTIONS, allowed_edges=ALLOWED_INTERNAL_EDGES)


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
    ("ayvu.formats.epub.io", "from ayvu.application import FormatPort"),
    ("ayvu.v1.cache", "from ayvu.application import CacheRepositoryPort"),
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
    ("ayvu.domain", "import ayvu.translation.chunking"),
    ("ayvu.formats.epub.io", "import ayvu.application"),
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
     "import ayvu.domain, ayvu.translation.chunking", ["ayvu.domain", "ayvu.translation.chunking"]),
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
        "ayvu.translation.chunking": (Path(), False, empty),
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
    root = make_tree(tmp_path, "ayvu.domain", "import ayvu.translation.chunking")
    (root / "translation" / "chunking.py").write_text("import ayvu.domain", encoding="utf-8")
    with pytest.raises(BoundaryError, match="cyclic"):
        check_tree(
            root,
            TEST_OWNERS,
            allowed_edges={
                ("ayvu.domain", "ayvu.translation.chunking"),
                ("ayvu.translation.chunking", "ayvu.domain"),
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
    (root / "__init__.py").write_text("from .providers.libretranslate.client import Translator", encoding="utf-8")
    with pytest.raises(BoundaryError, match="ayvu -> ayvu.providers.libretranslate.client"):
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
    root = make_tree(tmp_path, "ayvu.application", "import ayvu.v1.cache")
    exceptions = {("ayvu.application", "ayvu.v1.cache"): Exemption("Translation", 145, "use port")}
    check_tree(root, TEST_OWNERS, exceptions)
    (root / "application.py").write_text("import ayvu.v1.cache\nimport ayvu.providers.libretranslate.client", encoding="utf-8")
    with pytest.raises(BoundaryError, match="forbidden"):
        check_tree(root, TEST_OWNERS, exceptions)
    (root / "application.py").write_text("", encoding="utf-8")
    with pytest.raises(BoundaryError, match="unused exception"):
        check_tree(root, TEST_OWNERS, exceptions)


@pytest.mark.parametrize("exemption", [Exemption("", 145, "use port"),
    Exemption("Translation", 0, "use port"), Exemption("Translation", 145, "")])
def test_incomplete_exception_rejected(exemption: Exemption, tmp_path: Path) -> None:
    root = make_tree(tmp_path, "ayvu.application", "import ayvu.v1.cache")
    with pytest.raises(BoundaryError, match="invalid exception"):
        check_tree(root, TEST_OWNERS, {("ayvu.application", "ayvu.v1.cache"): exemption})


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
                ("ayvu.formats.epub.io", "ayvu.domain"),
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


@pytest.mark.parametrize('hidden', ['from ..review.csv_export import ReviewSegment', 'import requests', 'import sqlite3'])
def test_cli_transitive_dependencies_cannot_be_laundered_by_an_exemption(tmp_path, hidden):
    root = make_tree(tmp_path, 'ayvu.cli', 'from .providers.discovery import DiscoveryOutcome')
    (root / 'providers' / 'discovery.py').write_text(hidden)
    modules = {}
    for path in root.rglob('*.py'):
        parts = path.relative_to(root).with_suffix('').parts
        modules['.'.join(('ayvu', *parts[:-1 if parts[-1] == '__init__' else None]))] = (path, path.stem == '__init__', ast.parse(path.read_text()))
    targets = imports('ayvu.providers.discovery', False, ast.parse(hidden), modules)
    target = targets[0]
    exceptions = {('ayvu.providers.discovery', target): Exemption('test', 158, 'never valid for presentation')}
    with pytest.raises(BoundaryError, match='CLI transitive'):
        check_tree(root, TEST_OWNERS, exceptions, allowed_edges={('ayvu.cli', 'ayvu.providers.discovery')})


def test_cli_cannot_import_concrete_reexport_from_composition(tmp_path):
    root = make_tree(tmp_path, 'ayvu.cli', 'from .composition.cli import TranslationCache')
    (root / 'composition' / 'cli.py').write_text('from ..v1.cache import TranslationCache')
    with pytest.raises(BoundaryError, match='concrete re-export'):
        check_tree(root, TEST_OWNERS, allowed_edges={('ayvu.cli', CLI_ROOT), (CLI_ROOT, 'ayvu.v1.cache')})


@pytest.mark.parametrize("code", ["services = dict()", "services = {}", "services = []"])
def test_cli_composition_cannot_cache_a_mutable_or_eager_graph(tmp_path, code):
    root = make_tree(tmp_path, CLI_ROOT, code)
    with pytest.raises(BoundaryError, match='singleton'):
        check_tree(root, TEST_OWNERS)


@pytest.mark.parametrize('code', [
    'from .composition import cli as root',
    'from ayvu.composition import cli',
    'import ayvu.composition.cli as root',
])
def test_cli_cannot_hide_composition_construction_behind_a_module_alias(tmp_path, code):
    root = make_tree(tmp_path, 'ayvu.cli', code)
    with pytest.raises(BoundaryError, match='named workflow factories'):
        check_tree(root, TEST_OWNERS, allowed_edges={('ayvu.cli', CLI_ROOT)})


@pytest.mark.parametrize('code', [
    'def services():\n global registry\n registry = {}',
    'def service(resources={}):\n return resources',
    '@cache\ndef service():\n return adapter()',
])
def test_cli_composition_cannot_hide_a_cached_graph_in_a_factory(tmp_path, code):
    root = make_tree(tmp_path, CLI_ROOT, code)
    with pytest.raises(BoundaryError, match='singleton|eager CLI construction'):
        check_tree(root, TEST_OWNERS)


# Pesquisa #167: registro separado, removível, nunca um módulo de produção.
LOCAL_WORKER_RESEARCH = frozenset({'probe.py', 'measure.py'})


def test_local_worker_research_is_registered_stdlib_only_and_unreachable_from_production():
    directory = ROOT / 'spikes' / 'local-worker'
    assert {path.name for path in directory.glob('*.py')} == LOCAL_WORKER_RESEARCH
    for name in LOCAL_WORKER_RESEARCH:
        tree = ast.parse((directory / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split('.')[0] in sys.stdlib_module_names for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module.split('.')[0] in sys.stdlib_module_names
    for path in (ROOT / 'src' / 'ayvu').rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert 'local-worker' not in node.value and 'local_worker_spike' not in node.value
