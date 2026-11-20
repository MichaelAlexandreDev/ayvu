"""Static checks of the accepted ADR; never import the inspected modules."""
from __future__ import annotations
import ast
import sys
import pytest
from tests.architecture.architecture_boundaries_support import BoundaryError, CLI_ROOT, Exemption
from tests.architecture.architecture_boundaries_support import LOCAL_WORKER_RESEARCH, ROOT, TEST_OWNERS, check_tree, imports
from tests.architecture.architecture_boundaries_support import make_tree


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
    root = make_tree(tmp_path, 'ayvu.cli', 'from .composition.cli import SQLiteExactCache')
    (root / 'composition' / 'cli.py').write_text('from ..knowledge.cache.repository import SQLiteExactCache')
    with pytest.raises(BoundaryError, match='concrete re-export'):
        check_tree(root, TEST_OWNERS, allowed_edges={('ayvu.cli', CLI_ROOT), (CLI_ROOT, 'ayvu.knowledge.cache.repository')})


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
