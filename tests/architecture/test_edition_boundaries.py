"""Product identities and private composition data must stay behind the roots."""
from __future__ import annotations

import ast

import pytest

from tests.architecture.architecture_boundaries_support import BoundaryError, _read_modules
from tests.architecture.edition_boundaries_support import check_edition_boundaries
from tests.support.architecture_owners import OWNERS
from tests.support.paths import PROJECT_ROOT
from tests.support.public_api_support import PUBLIC_MODULES


def test_production_has_no_edition_import_or_identity_outside_composition():
    modules = _read_modules(PROJECT_ROOT / "src" / "ayvu", OWNERS)
    check_edition_boundaries(modules)
    assert not {"composition.editions", "composition.feature_catalog", "composition.surface_rules"} & PUBLIC_MODULES


def modules_for(tmp_path, code, root_code=""):
    sources = {
        "ayvu": (True, ""), "ayvu.interfaces": (True, ""), "ayvu.interfaces.cli": (True, ""),
        "ayvu.interfaces.cli.probe": (False, code), "ayvu.composition": (True, ""),
        "ayvu.composition.cli": (False, root_code),
        "ayvu.composition.editions": (False, "class Edition: pass"),
        "ayvu.composition.feature_catalog": (False, "class Feature: pass"),
        "ayvu.composition.surface_rules": (False, ""),
    }
    modules = {}
    for name, (package, source) in sources.items():
        path = tmp_path / (name.replace(".", "_") + ".py")
        path.write_text(source)
        modules[name] = (path, package, ast.parse(source))
    return modules


@pytest.mark.parametrize("code", (
    "from ayvu.composition.editions import Edition as Product",
    "from ...composition.editions import Edition",
    "from ayvu.composition import editions as products",
    "import ayvu.composition.feature_catalog as catalog",
    "from ayvu.composition.surface_rules import COMMAND_RULES",
    "if TYPE_CHECKING:\n    from ayvu.composition.editions import Edition",
    "import importlib\nmodule = importlib.import_module('ayvu.composition.editions')",
    "module = __import__('ayvu.composition.surface_rules', fromlist=['project_surface'])",
    "value = getattr(root, 'Edition')",
    "module = importlib.import_module('ayvu.composition.' + 'feature_catalog')",
    "if selected == 'lite': pass",
    "match selected:\n    case 'cli_free': pass",
    "name = 'CLI Pro'",
    "name = 'AI'",
    "value = CLI_FREE",
    "# Lite chooses fewer commands\nvalue = 1",
    "name = os.environ['AYVU_EDITION']",
    "value = settings.edition",
))
def test_forbidden_imports_names_comments_and_dynamic_selectors(tmp_path, code):
    with pytest.raises(BoundaryError):
        check_edition_boundaries(modules_for(tmp_path, code))


@pytest.mark.parametrize("root_code", (
    "from .editions import Edition as Product",
    "from .editions import Edition\nProduct = Edition",
    "from . import editions\nProduct = editions.Edition",
))
def test_alias_reexports_cannot_launder_identity_through_root(tmp_path, root_code):
    with pytest.raises(BoundaryError, match="reexport"):
        check_edition_boundaries(modules_for(tmp_path, "from ayvu.composition.cli import Product", root_code))


def test_whole_names_allow_unrelated_technical_words_and_named_root_factories(tmp_path):
    code = '''
from ayvu.composition.cli import installed_cli_surface
FAIL_FAST = True
failure = 'failed translation'
# Framework-free values have no product selection.
'''
    check_edition_boundaries(modules_for(tmp_path, code, "def installed_cli_surface(): pass"))


def test_only_frozen_provenance_ai_tags_are_technical_references(tmp_path):
    modules = modules_for(tmp_path, "")
    path = tmp_path / "provenance.py"
    source = 'class ActorKind:\n    AI = "ai"\nclass SourceKind:\n    AI = "ai"\n'
    path.write_text(source)
    modules["ayvu.provenance.model"] = (path, False, ast.parse(source))
    check_edition_boundaries(modules)
    for extra in ('selected = "ai"', 'name = "CLI Pro"', 'value = AI', '# Lite product'):
        path.write_text(source + extra)
        modules["ayvu.provenance.model"] = (path, False, ast.parse(source + extra))
        with pytest.raises(BoundaryError):
            check_edition_boundaries(modules)
