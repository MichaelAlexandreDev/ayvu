"""Manifest matrix, dependency failures and immutable implementation traceability."""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace

import pytest

from ayvu.composition.editions import EDITIONS, get_edition, validate_edition
from ayvu.composition.feature_catalog import (
    FEATURES, INFRASTRUCTURE_INVENTORY_IDS, Feature, validate_features,
)
from tests.support.paths import PROJECT_ROOT


@pytest.mark.parametrize("edition_id,name,interface,preset,style,planned", (
    ("lite", "Lite", "desktop", "low", "summary", set()),
    ("free", "Free", "desktop", None, "full", set()),
    ("ai", "AI", "desktop", None, "full", {"ai_assistance", "ai_translation_providers"}),
    ("cli_free", "CLI Free", "cli", None, "full", set()),
    ("cli_pro", "CLI Pro", "cli", None, "full", {"ai_assistance", "ai_translation_providers"}),
))
def test_product_manifest(edition_id, name, interface, preset, style, planned):
    edition = get_edition(edition_id)
    validate_edition(edition)
    assert (edition.name, edition.interfaces) == (name, {interface})
    assert (edition.execution_preset, edition.report_style) == (preset, style)
    assert edition.planned_features == planned
    assert not edition.features & {Feature.AI_ASSISTANCE, Feature.AI_TRANSLATION_PROVIDERS}


def test_feature_matrix_has_literal_inventory_membership():
    core = {"1.1", "1.10", "2.1", "2.2", "4.7", "7.1", "7.2", "7.3", "1.7", "1.12", "3.6"}
    extended = {"3.1", "1.3", "1.4", "1.5", "1.6", "4.5", "4.6", "4.8", "4.9", "5.1", "5.2", "7.4", "7.5", "6.1", "6.2"}
    terminal = {"1.8", "1.9", "3.2", "3.3", "3.4", "3.7", "4.2", "4.3", "4.4", "7.6"}
    for edition_id, expected, count in (
        ("lite", core, 11), ("free", core | extended, 27), ("ai", core | extended, 27),
        ("cli_free", core | extended | terminal, 37), ("cli_pro", core | extended | terminal, 37),
    ):
        selected = get_edition(edition_id).features
        assert len(selected) == count
        assert {item for definition in FEATURES if definition.id in selected for item in definition.inventory_ids} == expected
    assert INFRASTRUCTURE_INVENTORY_IDS == ("1.2", "1.11", "2.3", "2.4", "2.5", "3.5", "3.8", "4.1", "6.4", "8.1")
    assert len(EDITIONS) == len({edition.id for edition in EDITIONS}) == 5


def test_catalog_bindings_name_real_source_symbols_without_importing_them():
    assert {definition.id for definition in FEATURES} == set(Feature)
    for definition in FEATURES:
        assert definition.inventory_ids and definition.requires <= set(Feature)
        if definition.binding is None:
            assert definition.inventory_ids in (("8.3",), ("8.4",))
            continue
        module, symbol = definition.binding.rsplit(".", 1)
        tree = ast.parse((PROJECT_ROOT / "src" / "ayvu" / (module.replace(".", "/") + ".py")).read_text())
        assert symbol in {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}, definition.binding


@pytest.mark.parametrize("changes,message", (
    ({"features": frozenset({"unknown"})}, "Unknown feature"),
    ({"planned_features": frozenset({"unknown"})}, "Unknown planned"),
    ({"features": frozenset({Feature.AI_ASSISTANCE})}, "without binding"),
    ({"features": frozenset({Feature.PREVIEW})}, "Missing feature dependency"),
    ({"report_style": "full"}, "Report style"),
    ({"interfaces": frozenset({"server"})}, "product interfaces"),
    ({"planned_features": frozenset({Feature.PREVIEW})}, "disjoint"),
))
def test_invalid_manifests_fail(changes, message):
    with pytest.raises(ValueError, match=message):
        validate_edition(replace(get_edition("lite"), **changes))


def test_fixed_preset_cannot_keep_free_performance_selection():
    with pytest.raises(ValueError, match="Fixed preset"):
        validate_edition(replace(get_edition("free"), execution_preset="low"))


def test_missing_binding_and_unknown_dependency_are_not_silently_filtered():
    selected = get_edition("cli_free").features
    for replacement, message in (
        (replace(FEATURES[0], binding=None), "without binding"),
        (replace(FEATURES[0], requires=frozenset({"unknown"})), "Unknown feature dependency"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_features(selected, definitions=(replacement, *FEATURES[1:]))
    with pytest.raises(ValueError, match="Duplicate feature"):
        validate_features(selected, definitions=(*FEATURES, FEATURES[0]))
    with pytest.raises(ValueError, match="Unknown edition"):
        get_edition("unknown")


def test_declarations_are_deeply_immutable():
    def immutable(value):
        if is_dataclass(value):
            assert value.__dataclass_params__.frozen
            for field in fields(value):
                immutable(getattr(value, field.name))
        elif isinstance(value, (tuple, frozenset)):
            for item in value:
                immutable(item)
        else:
            assert value is None or isinstance(value, (str, bool))
    immutable((EDITIONS, FEATURES))
    with pytest.raises(FrozenInstanceError):
        EDITIONS[0].name = "changed"
