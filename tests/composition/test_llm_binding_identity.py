"""The LLM binding's plan: prompt, batch policy, decoding and glossary hints are v2 semantic identity (D9)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from ayvu.composition.builders.llm import LLM_REQUIREMENTS, glossary_hints, llm_plan_inputs
from ayvu.knowledge.cache import CacheEligibilityState
from ayvu.knowledge.glossary import Glossary
from ayvu.providers import ProviderRoute
from ayvu.providers.llm import PROMPT_V1, BatchPolicy, DecodingSettings, GlossaryRule, LlmSettings, LlmTranslationProvider
from ayvu.translation.document_contracts import ExecutionContext, UnitSource
from ayvu.translation.plan import resolve_plan
from tests.support.llm_fakes import LLM_PRODUCER, FakeModel, fake_snapshot
from tests.support.translation_cache_v2_support import NOW, OWNER, Env, MemoryCache, document

GLOSSARY_A = "glossary:" + "a" * 64
GLOSSARY_B = "glossary:" + "b" * 64


def llm_plan(settings=LlmSettings(), glossary_ref=GLOSSARY_A, producer=LLM_PRODUCER):
    return resolve_plan(fake_snapshot(NOW), LLM_REQUIREMENTS, producer, ProviderRoute("en", "pt"), chunk_limit=3000,
                        **llm_plan_inputs(settings, glossary_ref))


def test_the_plan_is_cache_eligible_and_names_the_prompt():
    plan = llm_plan()
    assert plan.cache_eligibility.state is CacheEligibilityState.ELIGIBLE
    operation = plan.semantic_operation
    assert operation.prompt_strategy == "llm_structured_batch"
    assert (operation.prompt_revision.namespace, operation.prompt_revision.version) == ("llm_prompt", 1)
    assert operation.terminology_revision.snapshot_reference == GLOSSARY_A


@pytest.mark.parametrize("changed", [
    {"settings": LlmSettings(prompt=replace(PROMPT_V1, revision=2))},
    {"settings": LlmSettings(prompt=replace(PROMPT_V1, system=PROMPT_V1.system + " Be formal."))},
    {"settings": LlmSettings(batch=BatchPolicy(max_units=4))},
    {"settings": LlmSettings(decoding=DecodingSettings(temperature="0.7"))},
    {"glossary_ref": GLOSSARY_B},
    {"producer": replace(LLM_PRODUCER, model="fake-model@2")},
], ids=["prompt_revision", "prompt_text", "batch_policy", "decoding", "glossary_in_prompt", "model"])
def test_every_output_changing_input_gives_a_new_semantic_fingerprint(changed):
    assert llm_plan(**changed).semantic_fingerprint != llm_plan().semantic_fingerprint


def test_operational_settings_keep_the_fingerprint():
    assert llm_plan(LlmSettings(rate_limit_retries=4)).semantic_fingerprint == llm_plan().semantic_fingerprint


def test_a_prompt_revision_change_is_never_served_from_the_v2_cache():
    cache = MemoryCache()

    def run(settings, key, phase):
        plan = llm_plan(settings)
        model = FakeModel()
        env = Env(plan, cache=cache, provider=LlmTranslationProvider(model, plan.producer, settings=settings))
        result = env.translate(document(" Hello ", "Good night", key=key),
                               context=ExecutionContext(env.job, OWNER, progress_phase=phase))
        return result, model

    first, first_model = run(LlmSettings(), "one.txt", "one")
    assert len(first_model.requests) == 2 and len(cache.entries) == 2
    assert [unit.text for unit in first.units] == [" PT[HELLO] ", "PT[GOOD NIGHT]"]
    assert sorted(entry.value for entry in cache.entries.values()) == ["PT[Good night]", "PT[Hello]"]

    reused, reused_model = run(LlmSettings(), "two.txt", "two")
    assert reused_model.requests == []
    assert [unit.source for unit in reused.units] == [UnitSource.EXACT_CACHE] * 2

    revised, revised_model = run(LlmSettings(prompt=replace(PROMPT_V1, revision=2)), "three.txt", "three")
    assert len(revised_model.requests) == 2
    assert [unit.source for unit in revised.units] == [UnitSource.PROVIDER] * 2
    assert len(cache.entries) == 4


def test_glossary_hints_keep_the_rules_and_their_order():
    glossary = Glossary({"Hello": "Olá"})
    hints = glossary_hints(glossary)
    assert [(item.rule, item.term, item.translation) for item in hints] == [(GlossaryRule.TRANSLATE, "Hello", "Olá")]
    assert glossary_hints(None) == () and glossary_hints(Glossary()) == ()
