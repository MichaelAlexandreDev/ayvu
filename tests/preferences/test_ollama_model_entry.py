"""``translator_models.ollama`` names an installed model, next to LibreTranslate's declared label (#376)."""
from __future__ import annotations

import pytest

from ayvu.composition.builders import rich_provider
from ayvu.preferences.config_facade import ConfigStore
from ayvu.preferences.model import AyvuConfig, PreferencesError, preferences_from_mapping


def test_both_entries_are_kept_and_persisted(tmp_path):
    models = {"libretranslate": "local-v1", "ollama": "qwen2.5:7b"}
    store = ConfigStore(tmp_path / "config.json")
    store.save(AyvuConfig(translator_models=models))
    assert store.load().translator_models == models
    assert rich_provider.selected_model(models, "ollama") == "qwen2.5:7b"
    assert rich_provider.selected_model(models, "libretranslate") == "local-v1"


@pytest.mark.parametrize("models", [
    {"ollama": "two words"}, {"ollama": "a@b"}, {"ollama": ""}, {"ollama": 7},
    # Each translator keeps its own rule: a model name is not a declared label.
    {"libretranslate": "qwen2.5:7b"}, {"other": "x"},
])
def test_an_invalid_entry_is_refused(models):
    with pytest.raises(PreferencesError, match="^invalid:translator_models$"):
        preferences_from_mapping({"translator_models": models})


def test_a_model_translator_selection_names_the_model_and_the_workers():
    options = type("Options", (), {"workers": 2})()
    assert rich_provider.model_selection({"ollama": "qwen2.5:7b"}, "Ollama", options) == {
        "model": "qwen2.5:7b", "workers": 2}
    assert rich_provider.model_selection(None, "ollama", options) == {"model": None, "workers": 2}
    assert rich_provider.model_selection({"ollama": "qwen2.5:7b"}, "libretranslate", options) == {}
