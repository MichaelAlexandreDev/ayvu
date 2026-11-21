"""How usage, cost and a dry run's estimate read in the terminal and Markdown reports (#380).

Money is US dollars. An unknown token count or cost reads "unknown", never zero.
"""
from __future__ import annotations

from decimal import Decimal


def format_usd(micro: int | None) -> str:
    """``$0.00`` style, keeping the micro-dollars a small amount needs."""
    if micro is None:
        return "unknown"
    dollars = (Decimal(micro) / 1_000_000).quantize(Decimal("0.000001"))
    text = format(dollars, "f").rstrip("0")
    whole, _, cents = text.partition(".")
    return f"${whole}.{cents.ljust(2, '0')}"


def _count(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def _per_thousand_words(micro: int | None, words: int) -> str:
    if micro is None or words <= 0:
        return "-"
    return format_usd(-(-micro * 1000 // words))


def _price(version: str | None, source: str | None) -> str:
    if version is None:
        return "unknown (no price for this model)"
    return "config.json" if source == "config" else version


def usage_rows(summary) -> list[tuple[str, str]]:
    run = summary.run
    rows = [
        ("Provider calls", _count(run.calls)),
        ("Characters sent", _count(run.characters)),
        ("Tokens in / out / cached", " / ".join(_count(value) for value in (
            run.input_tokens, run.output_tokens, run.cached_input_tokens))),
        ("Cost (USD)", format_usd(run.cost_micro_usd)),
        ("Cost per 1,000 words", _per_thousand_words(run.cost_micro_usd, run.words)),
        ("Price table", _price(None if summary.price is None else summary.price.version,
                               None if summary.price is None else summary.price.source.value)),
    ]
    if summary.max_cost_micro_usd is not None:
        rows.append(("Cost cap (USD)", f"{format_usd(summary.max_cost_micro_usd)}, "
                                       f"{format_usd(summary.spent_micro_usd)} counted against it"))
    if summary.resumed:
        job = summary.job
        rows.append(("Job total", "unknown" if job is None else
                     f"{_count(job.calls)} calls, {format_usd(job.cost_micro_usd)}"))
    return rows


def estimate_rows(estimate) -> list[tuple[str, str]]:
    label = "approximate" if estimate.approximate else "estimated"
    return [
        ("Estimated calls", _count(estimate.calls)),
        (f"Tokens in / out ({label})", f"{_count(estimate.input_tokens)} / {_count(estimate.output_tokens)}"),
        (f"Cost ({label}, USD)", format_usd(estimate.cost_micro_usd)),
        ("Worst case (what --max-cost reserves)", format_usd(estimate.worst_cost_micro_usd)),
        (f"Cost per 1,000 words ({label})", _per_thousand_words(estimate.cost_micro_usd, estimate.words)),
        ("Price table", _price(estimate.price_version, estimate.price_source)),
    ]


def report_rows(report) -> list[tuple[str, str]]:
    usage, estimate = getattr(report, "usage", None), getattr(report, "usage_estimate", None)
    return (usage_rows(usage) if usage is not None else []) + (estimate_rows(estimate) if estimate is not None else [])


def report_markdown(report) -> list[str]:
    usage, estimate = getattr(report, "usage", None), getattr(report, "usage_estimate", None)
    if usage is None and estimate is None:
        return []
    title = "## Usage" if usage is not None else "## Usage estimate"
    return ["", title, *(f"- {name}: {value}" for name, value in report_rows(report))]


def budget_line(summary) -> str:
    """What a stop at the cap says, in every mode: how much this run counted, and the cap."""
    return (f"Spent in this run: {format_usd(summary.spent_micro_usd)} of the "
            f"{format_usd(summary.max_cost_micro_usd)} cap (--max-cost). "
            f"Provider calls: {summary.run.calls}.")
