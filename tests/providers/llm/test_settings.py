"""The semantic part of the settings: prompt, batch policy and decoding, but nothing operational."""
from __future__ import annotations

from dataclasses import replace

import pytest

from ayvu.providers.llm import PROMPT_V1, BatchPolicy, DecodingSettings, LlmCoreError, LlmSettings


def test_semantics_name_the_prompt_revision_and_every_output_changing_value():
    semantics = LlmSettings().semantics()
    assert (semantics.prompt_strategy, semantics.prompt_version, semantics.prompt_value) == (
        "llm_structured_batch", 1, "llm-prompt-v1")
    assert semantics.prompt_snapshot == "llm_prompt:" + PROMPT_V1.digest
    assert dict((key, (kind, value)) for key, kind, value in semantics.generation_parameters) == {
        "batch_chars_per_token": ("integer", 3), "batch_max_units": ("integer", 16),
        "batch_token_budget": ("integer", 2048), "decoding_max_output_tokens": ("integer", 4096),
        "decoding_temperature": ("decimal", "0"), "llm_core_version": ("integer", 1),
        "output_schema_revision": ("integer", 1),
    }


@pytest.mark.parametrize("changes", [
    {"prompt": replace(PROMPT_V1, revision=2)}, {"batch": BatchPolicy(max_units=8)},
    {"batch": BatchPolicy(token_budget=4096)}, {"decoding": DecodingSettings(temperature="0.2")},
    {"decoding": DecodingSettings(top_p="0.9")}, {"decoding": DecodingSettings(seed=7)},
    {"decoding": DecodingSettings(max_output_tokens=1024)},
])
def test_each_semantic_change_changes_the_semantics(changes):
    assert LlmSettings(**changes).semantics() != LlmSettings().semantics()


def test_operational_settings_stay_out_of_the_semantics():
    assert LlmSettings(rate_limit_retries=3, max_retry_wait_ms=5).semantics() == LlmSettings().semantics()


def test_decimals_have_one_canonical_form():
    assert DecodingSettings(temperature="0.20") == DecodingSettings(temperature="0.2")
    assert DecodingSettings(temperature="0.0").temperature == "0"


@pytest.mark.parametrize("changes", [
    {"temperature": "3"}, {"temperature": "NaN"}, {"temperature": 0.2}, {"temperature": "0.0000001"},
    {"top_p": "1.5"}, {"max_output_tokens": 0}, {"seed": -1},
])
def test_invalid_decoding_is_refused(changes):
    with pytest.raises(LlmCoreError, match="invalid_decoding_settings"):
        DecodingSettings(**changes)


@pytest.mark.parametrize("changes", [{"rate_limit_retries": 9}, {"max_retry_wait_ms": -1}, {"batch": None}])
def test_invalid_settings_are_refused(changes):
    with pytest.raises(LlmCoreError, match="invalid_llm_settings"):
        LlmSettings(**changes)
