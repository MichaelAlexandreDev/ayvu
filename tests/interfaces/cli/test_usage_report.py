"""How usage, cost and an estimate read in the reports (#380). Unknown reads "unknown", never zero."""
from __future__ import annotations

import pytest

from ayvu.interfaces.cli.report import _render_markdown_report
from ayvu.interfaces.cli.usage_report import budget_line, estimate_rows, format_usd, usage_rows
from ayvu.providers import UsageEstimate, UsageTotals
from ayvu.providers.prices import ModelPrice, PriceSource, ResolvedPrice
from ayvu.translation import TranslationReport, UsageSummary

TABLE = ResolvedPrice(ModelPrice("0", "0"), PriceSource.TABLE, "2026-11-20")


@pytest.mark.parametrize("micro,expected", [
    (0, "$0.00"), (1, "$0.000001"), (1_234, "$0.001234"), (1_500_000, "$1.50"), (12_345_678, "$12.345678"), (None, "unknown"),
])
def test_dollars_keep_the_digits_a_small_amount_needs(micro, expected):
    assert format_usd(micro) == expected


def summary(**changes):
    values = dict(provider="ollama", model="m", run=UsageTotals(3, 120, 2_000, 300, 90, 0, 4_000),
                  job=UsageTotals(5, 200, 3_000, 500, 150, 0, 7_000), resumed=True, price=TABLE,
                  max_cost_micro_usd=2_500_000, spent_micro_usd=4_100)
    return UsageSummary(**{**values, **changes})


def test_the_rows_of_a_resumed_capped_run():
    rows = dict(usage_rows(summary()))
    assert rows["Provider calls"] == "3" and rows["Characters sent"] == "120"
    assert rows["Tokens in / out / cached"] == "300 / 90 / 0"
    assert rows["Cost (USD)"] == "$0.004" and rows["Cost per 1,000 words"] == "$0.002"
    assert rows["Price table"] == "2026-11-20"
    assert rows["Cost cap (USD)"] == "$2.50, $0.0041 counted against it"
    assert rows["Job total"] == "5 calls, $0.007"


def test_unknown_usage_and_an_unknown_price_read_unknown():
    unknown = summary(run=UsageTotals(1, 10, 2, None, None, None, None), job=None, price=None, max_cost_micro_usd=None,
                      spent_micro_usd=None)
    rows = dict(usage_rows(unknown))
    assert rows["Tokens in / out / cached"] == "unknown / unknown / unknown" and rows["Cost (USD)"] == "unknown"
    assert rows["Cost per 1,000 words"] == "-" and rows["Price table"].startswith("unknown")
    assert rows["Job total"] == "unknown" and "Cost cap (USD)" not in rows


def test_an_estimate_is_labelled_approximate_and_shows_the_worst_case():
    estimate = UsageEstimate(4, 80, 1_000, 1_200, 100, 16_384, 1_500, 250_000, "config", "config")
    rows = dict(estimate_rows(estimate))
    assert rows["Tokens in / out (approximate)"] == "1,200 / 100"
    assert rows["Cost (approximate, USD)"] == "$0.0015" and rows["Worst case (what --max-cost reserves)"] == "$0.25"
    assert rows["Cost per 1,000 words (approximate)"] == "$0.0015" and rows["Price table"] == "config.json"


def test_the_markdown_report_has_a_usage_section():
    report = TranslationReport(usage=summary())
    markdown = _render_markdown_report(report, dry_run=False)
    assert "\n## Usage\n" in markdown and "- Cost (USD): $0.004\n" in markdown and "- Job total: 5 calls" in markdown
    estimated = _render_markdown_report(TranslationReport(usage_estimate=UsageEstimate(
        1, 1, 1, 1, 1, 1, 0, 0, "2026-11-20", "table")), dry_run=True)
    assert "\n## Usage estimate\n" in estimated and "## Usage\n" not in estimated
    assert "## Usage" not in _render_markdown_report(TranslationReport(), dry_run=False)


def test_a_stop_at_the_cap_says_what_was_spent_and_the_cap():
    assert budget_line(summary()) == "Spent in this run: $0.0041 of the $2.50 cap (--max-cost). Provider calls: 3."
