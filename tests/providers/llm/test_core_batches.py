"""The core over a fake model: batches, id mapping, structured answers and the strict result check."""
from __future__ import annotations

import pytest

from ayvu.kernel import JobFailureFamily
from ayvu.providers.llm import BatchPolicy, FinishReason, LlmCoreError, LlmSettings, LlmTranslationProvider, ModelResponse
from ayvu.providers.ports import DeliveryState, ProviderRoute, ProviderStatus
from ayvu.providers.service import execute
from tests.support.llm_fakes import LLM_PRODUCER, FakeModel, answer, unit_request

TEXTS = ("One", "Two", "Three")


def provider(model, **settings):
    return LlmTranslationProvider(model, LLM_PRODUCER, settings=LlmSettings(**settings))


def batch(*texts, **changes):
    return tuple(unit_request(index, text, **changes) for index, text in enumerate(texts))


def test_success_maps_each_result_to_its_request_with_one_call():
    model = FakeModel()
    results = provider(model).execute_batch(batch(*TEXTS))
    assert model.batches() == [["s0", "s1", "s2"]]
    assert [item.status for item in results] == [ProviderStatus.SUCCEEDED] * 3
    assert [item.text for item in results] == ["PT[One]", "PT[Two]", "PT[Three]"]
    assert [item.request_id for item in results] == [item.request_id for item in batch(*TEXTS)]
    assert all(item.producer == LLM_PRODUCER and item.route == ProviderRoute("en", "pt") for item in results)
    assert all(item.delivery is DeliveryState.COMPLETED and item.warnings == () for item in results)
    assert [item.usage.characters_submitted for item in results] == [3, 3, 5]


def test_reordered_answers_map_by_id_never_by_position():
    model = FakeModel(answer([("s2", "Tres"), ("s0", "Um"), ("s1", "Dois")]))
    results = provider(model).execute_batch(batch(*TEXTS))
    assert [item.text for item in results] == ["Um", "Dois", "Tres"]
    assert len(model.requests) == 1


@pytest.mark.parametrize("broken", [
    answer([("s0", "Um"), ("s1", "Dois")]),
    answer([("s0", "Um"), ("s1", "Dois"), ("s2", "Tres"), ("s9", "Extra")]),
    answer([("s0", "Um"), ("s0", "Um de novo"), ("s1", "Dois"), ("s2", "Tres")]),
    ModelResponse(FinishReason.STOP, '{"translations": [{"id": "s0", "text": "Um"'),
    ModelResponse(FinishReason.STOP, '{"translations": [{"id": "s0", "text": "Um", "note": "x"}]}'),
    ModelResponse(FinishReason.LENGTH, '{"translations": ['),
], ids=["missing", "extra", "duplicate", "invalid_json", "outside_schema", "truncated"])
def test_a_structural_failure_fails_the_batch_and_retries_each_unit_alone(broken):
    model = FakeModel(broken)
    results = provider(model).execute_batch(batch(*TEXTS))
    assert model.batches() == [["s0", "s1", "s2"], ["s0"], ["s1"], ["s2"]]
    assert [item.text for item in results] == ["PT[One]", "PT[Two]", "PT[Three]"]


@pytest.mark.parametrize("broken,code", [
    (ModelResponse(FinishReason.STOP, "not json"), "provider_output_invalid"),
    (answer([("other", "x")]), "provider_output_invalid"),
    (ModelResponse(FinishReason.LENGTH, '{"translations": ['), "provider_output_truncated"),
])
def test_a_unit_still_broken_alone_is_an_invalid_output_failure(broken, code):
    model = FakeModel(broken)
    result = provider(model).execute(unit_request(0, "Alone"))
    assert result.status is ProviderStatus.FAILED and result.text is None
    assert (result.failure.family, result.failure.code, result.failure.retryable) == (
        JobFailureFamily.PROVIDER_PERMANENT, code, False)
    assert result.delivery is DeliveryState.COMPLETED and len(model.requests) == 1


def test_a_broken_placeholder_fails_only_its_own_unit():
    texts = ("Keep __AYVU_TAG_0__ here__AYVU_TAG_1__", "Plain")
    model = FakeModel(answer([("s0", "Mantenha aqui__AYVU_TAG_1__"), ("s1", "Simples")]))
    broken, plain = provider(model).execute_batch(batch(*texts))
    assert (broken.status, broken.failure.code) == (ProviderStatus.FAILED, "placeholder_mismatch")
    assert broken.failure.family is JobFailureFamily.PROVIDER_PERMANENT
    assert (plain.status, plain.text) == (ProviderStatus.SUCCEEDED, "Simples")


@pytest.mark.parametrize("translated", [
    "Mantenha __AYVU_TAG_1__ aqui__AYVU_TAG_0__",
    "Mantenha __AYVU_TAG_0__ __AYVU_TAG_0__ aqui__AYVU_TAG_1__",
    "Mantenha AYVU_TAG_0 aqui__AYVU_TAG_1__",
])
def test_placeholders_must_stay_once_and_in_order(translated):
    result = provider(FakeModel(answer([("s0", translated)]))).execute(
        unit_request(0, "Keep __AYVU_TAG_0__ here__AYVU_TAG_1__"))
    assert result.failure.code == "placeholder_mismatch"


def test_placeholders_kept_in_order_pass_the_strict_check():
    result = provider(FakeModel(answer([("s0", "Mantenha __AYVU_TAG_0__ aqui__AYVU_TAG_1__")]))).execute(
        unit_request(0, "Keep __AYVU_TAG_0__ here__AYVU_TAG_1__"))
    assert result.text == "Mantenha __AYVU_TAG_0__ aqui__AYVU_TAG_1__"


def test_an_empty_result_is_a_failure_and_never_a_silent_pass():
    result = provider(FakeModel(answer([("s0", "  ")]))).execute(unit_request(0, "Hello"))
    assert (result.status, result.failure.code) == (ProviderStatus.FAILED, "empty_result")


def test_text_equal_to_the_source_across_languages_is_a_warning():
    model = FakeModel(translate=lambda text: text)
    warned, same_language = (provider(model).execute(unit_request(0, "Hello")),
                             provider(model).execute(unit_request(1, "Hello", target="en-GB")))
    assert warned.status is ProviderStatus.SUCCEEDED and [item.code for item in warned.warnings] == ["untranslated_result"]
    assert same_language.warnings == ()
    numbers = provider(model).execute(unit_request(2, "1984"))
    assert numbers.warnings == ()


def test_outer_whitespace_comes_back_from_the_source():
    result = provider(FakeModel(answer([("s0", "Ola")]))).execute(unit_request(0, "  Hello \n"))
    assert result.text == "  Ola \n"


def test_batches_follow_the_policy_and_keep_document_order():
    model = FakeModel()
    texts = tuple(f"Unit {index}" for index in range(5))
    results = provider(model, batch=BatchPolicy(max_units=2)).execute_batch(batch(*texts))
    assert model.batches() == [["s0", "s1"], ["s2", "s3"], ["s4"]]
    assert [item.text for item in results] == [f"PT[Unit {index}]" for index in range(5)]


def test_single_request_execute_runs_through_the_service_wrapper():
    model = FakeModel()
    item = unit_request(0, "Hello", route=ProviderRoute("en", "pt"))
    result = execute(provider(model), item)
    assert (result.status, result.text, result.request_id) == (ProviderStatus.SUCCEEDED, "PT[Hello]", item.request_id)


@pytest.mark.parametrize("requests,code", [
    ((), "invalid_batch"),
    ((unit_request(0, "a"), unit_request(0, "b")), "duplicate_request"),
    ((unit_request(0, "a"), unit_request(1, "b", target="es")), "mixed_languages"),
])
def test_a_malformed_batch_is_refused_before_any_call(requests, code):
    model = FakeModel()
    with pytest.raises(LlmCoreError, match=code):
        provider(model).execute_batch(requests)
    assert model.requests == []
