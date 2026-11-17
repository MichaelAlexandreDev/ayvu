"""Batching bounds: a units cap, an estimated token budget, document order and oversized units."""
from __future__ import annotations

import pytest

from ayvu.providers.llm import BatchPolicy, LlmCoreError, PromptSegment, estimate_tokens, plan_batches


def segments(*texts):
    return tuple(PromptSegment(f"s{index}", text) for index, text in enumerate(texts))


def test_the_estimate_rounds_characters_per_token_up():
    policy = BatchPolicy(chars_per_token=4)
    assert [estimate_tokens(text, policy) for text in ("", "abc", "abcd", "abcde")] == [0, 1, 1, 2]


def test_the_units_cap_closes_a_batch():
    assert plan_batches(segments(*"abcde"), BatchPolicy(max_units=2)) == ((0, 1), (2, 3), (4,))


def test_the_token_budget_closes_a_batch_before_it_is_passed():
    # Each rendered segment {"id":"sN","text":"xxxxxxxxxx"}, is 31 characters: 31 tokens at one per character.
    policy = BatchPolicy(token_budget=100, chars_per_token=1)
    assert plan_batches(segments(*["x" * 10] * 4), policy) == ((0, 1, 2), (3,))
    assert plan_batches(segments(*["x" * 10] * 4), policy, overhead_tokens=40) == ((0,), (1,), (2,), (3,))


def test_an_oversized_unit_is_sent_alone_and_never_split():
    policy = BatchPolicy(token_budget=50, chars_per_token=1)
    assert plan_batches(segments("short", "x" * 500, "short"), policy) == ((0,), (1,), (2,))


def test_order_is_preserved_and_nothing_is_lost():
    groups = plan_batches(segments(*[str(index) * (index + 1) for index in range(30)]),
                          BatchPolicy(max_units=7, token_budget=120, chars_per_token=2))
    assert [index for group in groups for index in group] == list(range(30))
    assert all(1 <= len(group) <= 7 for group in groups)


@pytest.mark.parametrize("changes", [{"max_units": 0}, {"token_budget": -1}, {"chars_per_token": 1.5}])
def test_an_invalid_policy_is_refused(changes):
    with pytest.raises(LlmCoreError, match="invalid_batch_policy"):
        BatchPolicy(**changes)
