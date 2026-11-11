"""Static checks of the accepted ADR; never import the inspected modules."""
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

from tests.architecture.architecture_boundaries_support import BoundaryError, CLI_ROOT, EXCEPTIONS, Exemption, FIXTURES
from tests.architecture.architecture_boundaries_support import LOCAL_WORKER_RESEARCH, ROOT, TEST_OWNERS, check_tree, imports
from tests.architecture.architecture_boundaries_support import make_tree


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
