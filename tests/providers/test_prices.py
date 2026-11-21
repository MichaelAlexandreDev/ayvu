"""The dated price table, its configuration overrides and the cost arithmetic (#380)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ayvu.providers import ProviderSetupError
from ayvu.providers.prices import (
    ANY_MODEL, PRICE_TABLE, PRICE_TABLE_VERSION, ModelPrice, PriceSource, UsageRecord, UsageTotals, cost_micro_usd,
    micro_usd, price_overrides, resolve_price,
)

PRICED = ModelPrice("0.25", "1.25", "0.025")


def test_the_table_is_dated_and_local_providers_cost_nothing():
    assert PRICE_TABLE_VERSION == "2026-11-20"
    for provider, model in (("libretranslate", "local-model-2026"), ("ollama", "qwen2.5:7b"), ("ollama", None)):
        resolved = resolve_price(provider, model)
        assert resolved.source is PriceSource.TABLE and resolved.version == PRICE_TABLE_VERSION
        assert cost_micro_usd(resolved.price, 10**9, 10**9, 10**9) == 0
    assert set(PRICE_TABLE) == {"libretranslate", "ollama"}


def test_an_unknown_model_has_no_price_and_its_cost_is_unknown_not_zero():
    assert resolve_price("fake_remote", "fake-model-1") is None
    assert cost_micro_usd(None, 0, 0, 0) is None


def test_a_configured_price_wins_over_the_table_and_is_cited_as_config():
    overrides = price_overrides({"ollama": {"qwen2.5:7b": {"input": "0.25", "output": 1.25, "cached_input": "0.025"}}})
    resolved = resolve_price("ollama", "qwen2.5:7b", overrides)
    assert (resolved.price, resolved.source, resolved.version) == (PRICED, PriceSource.CONFIG, "config")
    assert resolve_price("ollama", "llama3.2:3b", overrides).source is PriceSource.TABLE
    every = price_overrides({"fake_remote": {ANY_MODEL: {"input": "3", "output": "15"}}})
    assert resolve_price("fake_remote", "any-model", every).price == ModelPrice("3", "15")


def test_a_cost_is_tokens_times_dollars_per_million_in_micro_dollars_rounded_up():
    # 1,000 input at $0.25/M = 250; 2,000 output at $1.25/M = 2,500; 4,000 cached at $0.025/M = 100.
    assert cost_micro_usd(PRICED, 1_000, 2_000, 4_000) == 2_850
    assert cost_micro_usd(PRICED, 1, 0, 0) == 1  # 0.25 micro-dollars, rounded up
    assert cost_micro_usd(PRICED, 0, 0, 0) == 0


def test_a_priced_count_that_is_unknown_makes_the_cost_unknown_and_a_free_one_does_not():
    assert cost_micro_usd(PRICED, None, 10, 0) is None
    assert cost_micro_usd(PRICED, 10, None, 0) is None
    assert cost_micro_usd(ModelPrice("1", "0"), 10, None, None) == 10


@pytest.mark.parametrize("value,expected", [("2.50", 2_500_000), ("0", 0), ("0.0000019", 1), (3, 3_000_000)])
def test_a_cap_in_dollars_becomes_micro_dollars_rounded_down(value, expected):
    assert micro_usd(value) == expected


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "abc", True, None, "1e7"])
def test_an_invalid_amount_is_refused_with_a_code(value):
    with pytest.raises(ProviderSetupError, match="^invalid_price$"):
        micro_usd(value)


@pytest.mark.parametrize("data", [
    [], {"Ollama": {}}, {"ollama": []}, {"ollama": {"m": {"input": "1"}}}, {"ollama": {"m": {"input": "1", "output": "-2"}}},
    {"ollama": {"m": {"input": "1", "output": "2", "extra": "3"}}}, {"ollama": {"\x00": {"input": "1", "output": "2"}}},
])
def test_invalid_overrides_are_refused_without_quoting_them(data):
    with pytest.raises(ProviderSetupError, match="^invalid_price$"):
        price_overrides(data)


def test_a_price_keeps_its_exact_decimal_form():
    assert ModelPrice(0.1, "0.3").input == Decimal("0.1")


def test_totals_add_up_and_one_unknown_part_makes_the_total_unknown():
    known = UsageRecord("r1", "j", "d", "u", "ollama", "m", 10, 2, 5, 7, 0, 3, PRICE_TABLE_VERSION, "table")
    unknown = UsageRecord("r2", "j", "d", "u", "ollama", "m", 4, 1, None, 2, 0, None, None, None)
    total = UsageTotals().plus(UsageTotals.of(known)).plus(UsageTotals.of(known))
    assert total == UsageTotals(2, 20, 4, 10, 14, 0, 6)
    with_unknown = total.plus(UsageTotals.of(unknown))
    assert (with_unknown.calls, with_unknown.input_tokens, with_unknown.output_tokens, with_unknown.cost_micro_usd) == (
        3, None, 16, None)
