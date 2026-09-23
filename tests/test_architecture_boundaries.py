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
    "ayvu.chunking": Owner("domain", "Translation"),
    "ayvu.cli": Owner("interface", "CLI"),
    "ayvu.cli_progress": Owner("interface", "CLI"),
    "ayvu.preflight": Owner("application", "Translation"),
    "ayvu.epub_io": Owner("format", "EPUB"),
    "ayvu.html_translate": Owner("format", "EPUB"),
    "ayvu.validation": Owner("format", "EPUB/Quality"),
    "ayvu.translator": Owner("provider", "Providers"),
    "ayvu.cache": Owner("knowledge", "Exact cache"),
    "ayvu.glossary": Owner("knowledge", "Glossary"),
    "ayvu.translation_memory": Owner("domain", "Knowledge/TM"),
    "ayvu.config": Owner("infrastructure", "Preferences"),
    "ayvu.resume": Owner("infrastructure", "Jobs"),
    "ayvu.review_export": Owner("infrastructure", "Review"),
    "ayvu.review_import": Owner("infrastructure", "Review"),
    "ayvu.library": Owner("application", "Library"),
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
    ("ayvu.cache", "ayvu.domain"),
    ("ayvu.cli", "ayvu.cli_progress"),
    ("ayvu.cli", "ayvu.domain"),
    ("ayvu.cli", "ayvu.library"),
    ("ayvu.cli", "ayvu.preflight"),
    ("ayvu.epub_io", "ayvu.domain"),
    ("ayvu.epub_io", "ayvu.html_translate"),
    ("ayvu.epub_io", "ayvu.translation_memory"),
    ("ayvu.html_translate", "ayvu.chunking"),
    ("ayvu.html_translate", "ayvu.domain"),
    ("ayvu.html_translate", "ayvu.translation_memory"),
    ("ayvu.preflight", "ayvu.domain"),
    ("ayvu.resume", "ayvu.domain"),
    ("ayvu.translation_memory", "ayvu.domain"),
    ("ayvu.validation", "ayvu.html_translate"),
}


EXCEPTIONS = {
    ("ayvu.cli", "sqlite3"): Exemption("CLI", 158, "CLI uses application outcomes"),
    ("ayvu.cli", "ayvu.cache"): Exemption("Knowledge", 150, "cache administration extracted"),
    ("ayvu.cli", "ayvu.config"): Exemption("Preferences", 152, "preferences use cases extracted"),
    ("ayvu.cli", "ayvu.epub_io"): Exemption("Translation", 145, "translation use case extracted"),
    ("ayvu.cli", "ayvu.glossary"): Exemption("Knowledge", 157, "glossary use cases extracted"),
    ("ayvu.cli", "ayvu.html_translate"): Exemption("CLI", 158, "neutral statistics outcome"),
    ("ayvu.cli", "ayvu.resume"): Exemption("Jobs", 146, "recovery use case extracted"),
    ("ayvu.cli", "ayvu.review_export"): Exemption("Review", 149, "review export use case extracted"),
    ("ayvu.cli", "ayvu.review_import"): Exemption("Review", 149, "review apply use case extracted"),
    ("ayvu.cli", "ayvu.translator"): Exemption("Providers", 128, "discovery/diagnostics use cases extracted"),
    ("ayvu.cli", "ayvu.validation"): Exemption("Translation", 145, "validation behind application port"),
    ("ayvu.preflight", "sqlite3"): Exemption("Translation", 145, "typed repository errors"),
    ("ayvu.preflight", "ayvu.cache"): Exemption("Translation", 145, "readiness uses repository port"),
    ("ayvu.preflight", "ayvu.epub_io"): Exemption("Translation", 145, "readiness uses format port"),
    ("ayvu.preflight", "ayvu.glossary"): Exemption("Translation", 145, "readiness uses glossary port"),
    ("ayvu.preflight", "ayvu.translator"): Exemption("Translation", 145, "readiness uses provider port"),
    ("ayvu.epub_io", "ayvu.cache"): Exemption("Formats", 144, "EPUB adapter isolated from pipeline"),
    ("ayvu.epub_io", "ayvu.glossary"): Exemption("Formats", 144, "glossary coordinated by pipeline"),
    ("ayvu.epub_io", "ayvu.review_export"): Exemption("Review", 149, "review export orchestration extracted"),
    ("ayvu.epub_io", "ayvu.review_import"): Exemption("Review", 149, "review apply orchestration extracted"),
    ("ayvu.epub_io", "ayvu.translator"): Exemption("Formats", 144, "provider port separated by #141"),
    ("ayvu.html_translate", "ayvu.cache"): Exemption("Formats", 143, "linguistic pipeline separated"),
    ("ayvu.html_translate", "ayvu.glossary"): Exemption("Formats", 143, "linguistic pipeline separated"),
    ("ayvu.html_translate", "ayvu.translator"): Exemption("Formats", 143, "provider port separated by #141"),
    ("ayvu.translation_memory", "rapidfuzz"): Exemption("Knowledge", 161, "TM matching behind repository boundary"),
    ("ayvu.translation_memory", "ayvu.cache"): Exemption("Knowledge", 161, "approved TM separated from exact cache"),
    ("ayvu.library", "subprocess"): Exemption("Library", 151, "reader launch behind process port"),
    ("ayvu.library", "os"): Exemption("Library", 151, "filesystem queries behind discovery port"),
    ("ayvu.library", "shutil"): Exemption("Library", 151, "executable discovery behind process port"),
    ("ayvu.library", "ayvu.config"): Exemption("Library", 151, "configuration passed as application values"),
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
