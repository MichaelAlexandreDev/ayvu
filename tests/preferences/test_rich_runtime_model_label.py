from __future__ import annotations
import json
from dataclasses import replace
import pytest
import ayvu.composition.builders.translate_project as composition
from ayvu.knowledge.cache import CacheEligibilityState, CacheIneligibleReason
from ayvu.preferences.config_facade import ConfigStore
from ayvu.preferences.model import AyvuConfig, config_from_preferences, config_to_preferences
from ayvu.preferences.model import GlobalPreferences, PreferencesError, preferences_from_mapping, preferences_to_mapping
from ayvu.provenance.model import ProducerIdentity, ProvenanceValidationError, declared_model
from ayvu.providers.ports import ProviderRoute
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
from ayvu.translation.plan import ResolvedTranslationPlan


@pytest.mark.parametrize("label", ["a", "model-2026_11", "0", "a" * 48])
def test_declared_label_roundtrips_existing_preferences_in_memory(label):
    assert declared_model(label) == "declared:" + label
    original = AyvuConfig.from_dict({"translator_models": {"libretranslate": label}})
    preferences = config_to_preferences(original)
    parsed, unknown = preferences_from_mapping(preferences_to_mapping(preferences))
    loaded = config_from_preferences(parsed)
    assert not unknown and loaded.translator_models == original.translator_models
    updated = replace(loaded, reader_app="reader")
    assert AyvuConfig.from_dict(updated.to_dict()).translator_models == original.translator_models


@pytest.mark.parametrize("label", ["a", "model-2026_11", "0", "a" * 48])
def test_declared_label_is_validated_namespaced_and_persisted(tmp_path, label):
    config = AyvuConfig(translator_models={"libretranslate": label})
    store = ConfigStore(tmp_path / "config.json")
    store.save(config)
    loaded = store.load()
    assert loaded.translator_models == config.translator_models
    assert json.loads(store.path.read_text())["translator_models"] == config.translator_models
    assert config_from_preferences(config_to_preferences(loaded)).translator_models == config.translator_models
    producer = replace(LIBRETRANSLATE_PRODUCER, model=declared_model(label))
    assert producer.model == "declared:" + label and producer.fully_identified
    assert "translator_models" not in repr(loaded) and "declared:<redacted>" in repr(producer)


@pytest.mark.parametrize("label", ["", "A", "a" * 49, "two words", "https://server/model", "/tmp/model", "../model",
                                 "a.b", "a:b", "a@b", "a\nb", "árvore", 12, None])
def test_invalid_labels_are_bounded_errors_without_caller_data(label):
    with pytest.raises(ProvenanceValidationError, match="^invalid_model_label$"):
        declared_model(label)
    with pytest.raises(PreferencesError, match="^invalid:translator_models$"):
        preferences_from_mapping({"translator_models": {"libretranslate": label}})


def test_namespace_is_only_accepted_for_the_model_and_unknown_stays_ineligible():
    with pytest.raises(ProvenanceValidationError):
        ProducerIdentity("declared:model", "model", "adapter", "pipeline")
    with pytest.raises(ProvenanceValidationError):
        replace(LIBRETRANSLATE_PRODUCER, model="observed:model")
    plan = ResolvedTranslationPlan(LIBRETRANSLATE_PRODUCER, ProviderRoute("en", "pt"), "capfp:v1:" + "a" * 64, 3000)
    assert plan.cache_eligibility.reason is CacheIneligibleReason.UNKNOWN_MODEL
    assert plan.semantic_fingerprint is None
    a = replace(plan, producer=replace(LIBRETRANSLATE_PRODUCER, model=declared_model("a")), route_hops=None)
    b = replace(a, producer=replace(a.producer, model=declared_model("b")), route_hops=None)
    assert a.cache_eligibility.state is CacheEligibilityState.ELIGIBLE
    assert a.semantic_fingerprint != b.semantic_fingerprint
    assert replace(b, producer=a.producer, route_hops=None).semantic_fingerprint == a.semantic_fingerprint


def test_absence_keeps_the_existing_configuration_payload_and_runtime_selection():
    assert "translator_models" not in AyvuConfig().to_dict()
    assert "translator_models" not in preferences_to_mapping(GlobalPreferences())
    assert composition.selected_model({}, "libretranslate") is None
    assert composition.selected_model({"libretranslate": "local-v1"}, " LIBRETRANSLATE ") == "local-v1"
    assert composition.selected_model({"libretranslate": "local-v1"}, "other") is None
