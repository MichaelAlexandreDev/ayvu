"""Literal product expectations and comparison with the installed Typer tree."""
from __future__ import annotations

from dataclasses import asdict, replace
import os
import subprocess
import sys

import pytest

from ayvu.composition.cli import CliSurface, cli_surface, installed_cli_surface, project_cli_surface
from ayvu.composition.editions import get_edition
from ayvu.composition.feature_catalog import Feature
from ayvu.composition.surface_rules import BINDING_RULES, COMMAND_RULES, MENU_RULES, validate_rules
from tests.architecture.cli_surface_support import normalized_cli_contract


@pytest.mark.parametrize("edition_id,commands,menu,extraction,reports", (
    ("lite", {"translate", "resume", "test-translator", "consent revoke"},
     (("1", "translate"), ("2", "preview"), ("4", "settings"), ("6", "help"), ("0", "exit")), (), ()),
    ("free", {"translate", "resume", "test-translator", "consent revoke", "inspect", "extract", "apply-review"},
     (("1", "translate"), ("2", "preview"), ("3", "library"), ("4", "settings"), ("5", "glossaries"), ("6", "help"), ("0", "exit")), ("markdown",), ("markdown",)),
    ("ai", {"translate", "resume", "test-translator", "consent revoke", "inspect", "extract", "apply-review"},
     (("1", "translate"), ("2", "preview"), ("3", "library"), ("4", "settings"), ("5", "glossaries"), ("6", "help"), ("0", "exit")), ("markdown",), ("markdown",)),
    ("cli_free", {"translate", "resume", "test-translator", "consent revoke", "inspect", "extract", "apply-review", "languages", "cache inspect", "cache clean", "cache export", "cache import"},
     (("1", "translate"), ("2", "preview"), ("3", "library"), ("4", "settings"), ("5", "glossaries"), ("6", "help"), ("0", "exit")), ("markdown",), ("markdown",)),
    ("cli_pro", {"translate", "resume", "test-translator", "consent revoke", "inspect", "extract", "apply-review", "languages", "cache inspect", "cache clean", "cache export", "cache import"},
     (("1", "translate"), ("2", "preview"), ("3", "library"), ("4", "settings"), ("5", "glossaries"), ("6", "help"), ("0", "exit")), ("markdown",), ("markdown",)),
))
def test_literal_edition_projection(edition_id, commands, menu, extraction, reports):
    edition = get_edition(edition_id)
    plan = project_cli_surface(edition.features, execution_preset=edition.execution_preset, report_style=edition.report_style)
    assert {" ".join(path) for path in plan.commands} == commands
    assert plan.menu_actions == menu
    assert plan.providers == ("libretranslate",)
    assert plan.input_formats == ("epub",)
    assert plan.extraction_writers == extraction
    assert plan.report_writers == reports
    assert plan.assistance_roles == ()
    assert plan.preview is True
    assert (plan.execution_preset, plan.report_style) == (edition.execution_preset, edition.report_style)
    assert isinstance(plan, CliSurface)
    if edition_id in ("cli_free", "cli_pro"):
        assert cli_surface(edition_id) == plan
    else:
        with pytest.raises(ValueError, match="CLI interface"):
            cli_surface(edition_id)


def test_installed_selection_matches_real_typer_tree_and_existing_menu_keys():
    from ayvu.interfaces.cli.guided import menu
    contract = normalized_cli_contract()
    def leaves(record, prefix=()):
        return {path for name, child in record["commands"].items()
                for path in (leaves(child, (*prefix, name)) if child["commands"] else {(*prefix, name)})}
    plan = installed_cli_surface()
    assert set(plan.commands) == leaves(contract)
    assert {path[0] for path in plan.commands} == set(contract["commands"])
    assert len(contract["commands"]) == 9
    assert set(contract["commands"]["consent"]["commands"]) == {"revoke"}
    assert set(contract["commands"]["cache"]["commands"]) == {"inspect", "clean", "export", "import"}
    assert "--preview" in {option for parameter in contract["parameters"] for option in parameter["opts"]}
    assert tuple(key for key, _ in plan.menu_actions) == (
        menu.GUIDED_TRANSLATE_OPTION, menu.GUIDED_PREVIEW_OPTION, menu.GUIDED_LIBRARY_OPTION,
        menu.GUIDED_SETTINGS_OPTION, menu.GUIDED_GLOSSARY_OPTION, menu.GUIDED_HELP_OPTION, menu.GUIDED_EXIT_OPTION,
    )
    assert plan == cli_surface("cli_free") == cli_surface("cli_pro")
    assert "edition" not in repr(asdict(plan)).lower()


def test_synthetic_capability_removal_changes_only_its_surface():
    features = get_edition("cli_free").features
    before = project_cli_surface(features)
    after = project_cli_surface(features - {Feature.EXTRACT_MARKDOWN})
    assert set(before.commands) - set(after.commands) == {("extract",)}
    assert after.extraction_writers == ()
    assert after.report_writers == ("markdown",)
    assert after.menu_actions == before.menu_actions
    assert after.providers == before.providers
    assert project_cli_surface(features) == before
    assert project_cli_surface(features) is not before


@pytest.mark.parametrize("changes,message", (
    ({"commands": (*COMMAND_RULES, COMMAND_RULES[0])}, "Duplicate command"),
    ({"menu": (*MENU_RULES, replace(MENU_RULES[0], key="9", action="orphan", requires="missing"))}, "Orphan"),
    ({"menu": (*MENU_RULES, replace(MENU_RULES[0], key="9"))}, "Duplicate menu"),
    ({"menu": (replace(MENU_RULES[0], action="orphan"),)}, "Orphan menu"),
    ({"bindings": (*BINDING_RULES, replace(BINDING_RULES[0], id="unknown", requires=frozenset({"missing"})))}, "Unknown binding"),
))
def test_inconsistent_surface_rules_fail(changes, message):
    with pytest.raises(ValueError, match=message):
        validate_rules(**changes)


def test_fresh_import_and_all_plans_acquire_no_resources(tmp_path):
    code = '''
import os, sys, requests

def deny(*args, **kwargs):
    raise AssertionError('resource acquired')
requests.Session.__init__ = deny

def audit(event, args):
    if event.startswith('socket.') or event in {'sqlite3.connect', 'subprocess.Popen'}:
        raise AssertionError(event)
    if event == 'open' and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if any(path.startswith(os.environ[name] + os.sep) for name in ('HOME', 'XDG_CONFIG_HOME')):
            raise AssertionError('user store opened')
sys.addaudithook(audit)
from ayvu.composition.editions import EDITIONS
from ayvu.composition.cli import installed_cli_surface, project_cli_surface
for edition in (*EDITIONS, *reversed(EDITIONS)):
    project_cli_surface(edition.features, execution_preset=edition.execution_preset, report_style=edition.report_style)
assert installed_cli_surface() == installed_cli_surface()
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
             "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "home").exists() and not (tmp_path / "config").exists()
