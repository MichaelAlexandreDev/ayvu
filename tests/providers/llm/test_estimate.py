"""A dry run's token estimate against what a fake model reports for the same requests (#380).

The fake model counts tokens with a crude subword rule: every run of up to four letters or digits, and every
punctuation mark, is one token; whitespace is free. It translates by lengthening each word, as Portuguese
lengthens English. The stated tolerance is ±25% for input, output and total tokens.
"""
from __future__ import annotations

import json

import pytest

from ayvu.providers.llm import (
    OUTPUT_TOKEN_FACTOR, LlmSettings, LlmTranslationProvider, LlmUsageEstimator, PromptSegment, estimate_call,
    render_prompt,
)
from tests.support.llm_fakes import LLM_PRODUCER, CountingModel, unit_request

TOLERANCE = 0.25
PARAGRAPHS = (
    "It was a bright cold day in April, and the clocks were striking thirteen.",
    "The house stood on a slight rise just on the edge of the village. It stood on its own and looked out over "
    "a broad spread of West Country farmland.",
    "Call me Ishmael. Some years ago, never mind how long precisely, having little or no money in my purse, and "
    "nothing particular to interest me on shore, I thought I would sail about a little and see the watery part "
    "of the world.",
    "Hello reader.",
    "In the beginning the Universe was created. This has made a lot of people very angry and been widely "
    "regarded as a bad move.",
) * 4


def within(estimate: int, actual: int) -> bool:
    return abs(estimate - actual) <= TOLERANCE * actual


def test_the_estimate_is_within_the_stated_tolerance_of_the_actual_run():
    estimator = LlmUsageEstimator(LlmSettings(), "en", "pt")
    for text in PARAGRAPHS:
        estimator.add(text)
    model = CountingModel()
    provider = LlmTranslationProvider(model, LLM_PRODUCER)
    results = [provider.execute(unit_request(index, text)) for index, text in enumerate(PARAGRAPHS)]
    actual_input = sum(result.usage.input_tokens for result in results)
    actual_output = sum(result.usage.output_tokens for result in results)
    assert estimator.calls == len(model.requests) == len(PARAGRAPHS)
    assert estimator.characters == sum(map(len, PARAGRAPHS)) and estimator.approximate
    assert within(estimator.input_tokens, actual_input), (estimator.input_tokens, actual_input)
    assert within(estimator.output_tokens, actual_output), (estimator.output_tokens, actual_output)
    assert within(estimator.input_tokens + estimator.output_tokens, actual_input + actual_output)
    # The worst case is the full output allowance of every call, as the cost cap reserves it.
    assert estimator.worst_output_tokens == len(PARAGRAPHS) * LlmSettings().decoding.max_output_tokens


def test_the_estimate_renders_the_same_request_the_run_sends():
    settings = LlmSettings()
    segments = (PromptSegment("s0", PARAGRAPHS[1]),)
    request = render_prompt(settings.prompt, "en", "pt", segments, settings.decoding)
    model = CountingModel()
    LlmTranslationProvider(model, LLM_PRODUCER).execute(unit_request(0, PARAGRAPHS[1]))
    assert (model.requests[0].system, model.requests[0].user) == (request.system, request.user)
    estimate = estimate_call(request, segments, settings.batch)
    assert estimate.input_tokens == -(-len(request.system + request.user) // settings.batch.chars_per_token)
    answer = len(json.dumps({"translations": [{"id": "s0", "text": PARAGRAPHS[1]}]}, separators=(",", ":")))
    assert estimate.output_tokens == -(-(answer * OUTPUT_TOKEN_FACTOR.numerator)
                                       // (settings.batch.chars_per_token * OUTPUT_TOKEN_FACTOR.denominator))


class FakeCounter:
    """The provider's own counter, as AI-07 will wire one; here it only counts."""

    def __init__(self, answer=7) -> None:
        self.answer, self.seen = answer, []

    def count_input_tokens(self, request):
        self.seen.append(request)
        return self.answer


def test_a_provider_counter_replaces_the_approximate_input_and_is_not_labelled_approximate():
    counter = FakeCounter()
    estimator = LlmUsageEstimator(LlmSettings(), "en", "pt", counter=counter)
    estimator.add("Hello reader.")
    estimator.add("Goodbye reader.")
    assert estimator.input_tokens == 14 and not estimator.approximate and len(counter.seen) == 2


@pytest.mark.parametrize("answer", [None, -1, 1.5, True])
def test_a_counter_answer_that_is_not_a_count_falls_back_to_the_approximation(answer):
    settings = LlmSettings()
    segments = (PromptSegment("s0", "Hello reader."),)
    request = render_prompt(settings.prompt, "en", "pt", segments, settings.decoding)
    estimate = estimate_call(request, segments, settings.batch, FakeCounter(answer))
    assert estimate.approximate and estimate.input_tokens == estimate_call(request, segments, settings.batch).input_tokens
