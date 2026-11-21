"""The LLM core reports each result's tokens and asks the run's cost meter before every call (#380)."""
from __future__ import annotations

from ayvu.kernel import JobFailureFamily
from ayvu.providers.llm import BatchPolicy, LlmSettings, LlmTranslationProvider, ModelResponse
from ayvu.providers.ports import DeliveryState, ProviderStatus
from tests.support.llm_fakes import LLM_PRODUCER, FakeModel, answer, unit_request


def reported(segments, *, input_tokens=100, output_tokens=40, cached=None):
    response = answer([(item["id"], f"PT[{item['text']}]") for item in segments])
    return ModelResponse(response.finish, response.text, input_tokens, output_tokens, cached)


class Meter:
    """Records the reservations and settlements; ``room`` is how many calls fit."""

    def __init__(self, room=10**6) -> None:
        self.room, self.reserved, self.settled = room, [], []

    def reserve(self, input_tokens, max_output_tokens):
        if len(self.reserved) >= self.room:
            return None
        self.reserved.append((input_tokens, max_output_tokens))
        return len(self.reserved)

    def settle(self, reservation, spent):
        self.settled.append((reservation, spent))


def test_a_result_carries_the_tokens_the_model_reported():
    model = FakeModel(lambda segments: reported(segments, cached=5))
    result = LlmTranslationProvider(model, LLM_PRODUCER).execute(unit_request(0, "Hello reader."))
    usage = result.usage
    assert result.status is ProviderStatus.SUCCEEDED
    assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (100, 40, 5)
    assert usage.characters_submitted == len("Hello reader.")


def test_a_model_that_reports_nothing_leaves_the_tokens_unknown_not_zero():
    result = LlmTranslationProvider(FakeModel(), LLM_PRODUCER).execute(unit_request(0, "Hello reader."))
    assert (result.usage.input_tokens, result.usage.output_tokens) == (None, None)


def test_a_batch_call_is_split_over_its_units_and_the_shares_add_up():
    model = FakeModel(lambda segments: reported(segments, input_tokens=101, output_tokens=10, cached=0))
    provider = LlmTranslationProvider(model, LLM_PRODUCER, settings=LlmSettings(batch=BatchPolicy(max_units=3)))
    results = provider.execute_batch(tuple(unit_request(index, f"text {index}") for index in range(3)))
    assert len(model.requests) == 1
    assert [item.usage.input_tokens for item in results] == [34, 34, 33]
    assert sum(item.usage.output_tokens for item in results) == 10


def test_a_retried_unit_adds_its_own_call_to_its_share_of_the_failed_batch():
    broken = ModelResponse(answer([("s0", "only one")]).finish, answer([("s0", "only one")]).text, 60, 6)
    model = FakeModel(broken, lambda segments: reported(segments, input_tokens=50, output_tokens=5),
                      lambda segments: reported(segments, input_tokens=70, output_tokens=7))
    provider = LlmTranslationProvider(model, LLM_PRODUCER, settings=LlmSettings(batch=BatchPolicy(max_units=2)))
    first, second = provider.execute_batch((unit_request(0, "one"), unit_request(1, "two")))
    assert len(model.requests) == 3
    assert (first.usage.input_tokens, first.usage.output_tokens) == (30 + 50, 3 + 5)
    assert (second.usage.input_tokens, second.usage.output_tokens) == (30 + 70, 3 + 7)


def test_every_call_reserves_its_worst_case_and_is_settled_with_what_it_used():
    meter = Meter()
    model = FakeModel(lambda segments: reported(segments, input_tokens=120, output_tokens=30, cached=4))
    LlmTranslationProvider(model, LLM_PRODUCER, meter=meter).execute(unit_request(0, "Hello reader."))
    [(estimated, allowance)] = meter.reserved
    request = model.requests[0]
    assert estimated == -(-len(request.system + request.user) // 3) and allowance == request.decoding.max_output_tokens
    [(reservation, spent)] = meter.settled
    assert reservation == 1 and (spent.input_tokens, spent.output_tokens, spent.cached_input_tokens) == (120, 30, 4)


def test_a_call_that_does_not_fit_the_cap_is_never_sent():
    meter = Meter(room=0)
    model = FakeModel()
    result = LlmTranslationProvider(model, LLM_PRODUCER, meter=meter).execute(unit_request(0, "Hello reader."))
    assert model.requests == [] and meter.settled == []
    assert result.status is ProviderStatus.FAILED and result.delivery is DeliveryState.NOT_SENT
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.POLICY, "cost_budget_reached")
    assert result.usage.input_tokens == 0 and result.usage.output_tokens == 0


def test_a_call_whose_outcome_is_unknown_keeps_its_whole_reservation():
    meter = Meter()
    model = FakeModel(TimeoutError("socket"))
    result = LlmTranslationProvider(model, LLM_PRODUCER, meter=meter).execute(unit_request(0, "Hello reader."))
    assert result.delivery is DeliveryState.UNKNOWN
    assert meter.settled == [(1, None)]  # nothing is known, so the reserved worst case stays counted
    assert result.usage.input_tokens is None
