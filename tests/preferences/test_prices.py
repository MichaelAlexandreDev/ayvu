"""``prices.<translator>.<model>`` in config.json: validated, kept immutable and saved back exactly (#380)."""
from __future__ import annotations

import json

import pytest

from ayvu.preferences.config_facade import ConfigStore
from ayvu.preferences.model import AyvuConfig, GlobalPreferences, PreferencesError, preferences_from_mapping
from ayvu.preferences.prices import prices_from_mapping, prices_to_mapping

PRICES = {"ollama": {"qwen2.5:7b": {"input": "0.25", "output": "1.25", "cached_input": "0.025"}},
          "fake_remote": {"*": {"input": "3", "output": "15", "cached_input": "0"}}}


def test_prices_are_kept_and_persisted_with_their_exact_decimal_form(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save(AyvuConfig(translator_models={"ollama": "qwen2.5:7b"}, prices=PRICES))
    assert store.load().prices == PRICES
    assert json.loads((tmp_path / "config.json").read_text())["prices"] == PRICES


def test_a_price_given_as_a_number_or_without_cached_input_is_normalised():
    value, unknown = preferences_from_mapping({"prices": {"ollama": {"m": {"input": 0.5, "output": 2}}}})
    assert not unknown and value.prices == (("ollama", "m", "0.5", "2", "0"),)


def test_saving_other_settings_keeps_the_prices(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"prices": PRICES}))
    store = ConfigStore(path)
    loaded = store.load()
    store.save(AyvuConfig(default_target_language="es", prices=loaded.prices, _preferences_revision=loaded._preferences_revision))
    assert store.load().prices == PRICES


@pytest.mark.parametrize("prices", [
    [], {"Ollama": {"m": {"input": "1", "output": "1"}}}, {"ollama": {"m": {"input": "1"}}},
    {"ollama": {"m": {"input": "-1", "output": "1"}}}, {"ollama": {"m": {"input": "x", "output": "1"}}},
    {"ollama": {"m": {"input": "1", "output": "1", "currency": "EUR"}}},
])
def test_an_invalid_price_is_refused(prices):
    with pytest.raises(PreferencesError, match="^invalid:prices$"):
        preferences_from_mapping({"prices": prices})


def test_the_immutable_value_is_checked_too():
    with pytest.raises(PreferencesError, match="^invalid:prices$"):
        GlobalPreferences(prices=(("ollama", "m", "-1", "1", "0"),))
    rows = prices_from_mapping(PRICES, lambda: AssertionError("valid"))
    assert prices_from_mapping(prices_to_mapping(rows), lambda: AssertionError("valid")) == rows
