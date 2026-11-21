"""Composition of a run's usage and cost (#380): the price, the shared meter and cap, and a dry run's estimate.

The price of the selected translator and model comes from ``prices.<translator>.<model>`` in the
configuration, or else from the dated table in ``providers.prices``. A ``--max-cost`` with no known price
is refused here, before any request. The meter rides on the run's provider binding, so every worker's
provider reserves from it and every result is recorded through it.
"""
from __future__ import annotations

from datetime import UTC, datetime

from ...providers import ProviderSetupError, ResolvedPrice, UsageEstimate, cost_micro_usd, price_overrides, resolve_price
from ...providers.llm import LlmSettings, LlmUsageEstimator, TokenCounter
from ...translation import PreflightError, RunMeter, count_words
from .llm import glossary_hints
from .ollama import OLLAMA, OllamaSettings
from .rich_provider import LibreTranslateBinding, names_model


def run_price(translator_name: str, model: str | None, prices: object) -> ResolvedPrice | None:
    try:
        overrides = price_overrides(prices or {})
    except ProviderSetupError:
        raise PreflightError("A price in config.json is not valid.",
                             "Set prices.<translator>.<model> to {\"input\": ..., \"output\": ...}, "
                             "in US dollars per million tokens.", detail="invalid_price") from None
    return resolve_price(translator_name, model, overrides)


def run_meter(book) -> RunMeter:
    """The invocation's meter. Refuses a cap that cannot be enforced, before anything is prepared or sent."""
    name = book.translator_name.strip().lower()
    model = book.translator_models.get(name)
    price = run_price(name, model, getattr(book, "prices", None))
    cap = getattr(book, "max_cost", None)
    if cap is not None and price is None:
        raise PreflightError(f"No price is known for {name} model {model}, so --max-cost cannot be enforced. "
                             "Nothing was sent.",
                             f"Add prices.{name}.<model> to config.json (US dollars per million tokens), "
                             "or run without --max-cost.", detail="price_unknown")
    return RunMeter(name, model, price, max_cost_micro_usd=cap)


def metered_binding(binding, label: str | None, meter: RunMeter):
    """The run's provider binding carrying its meter; LibreTranslate's is built here when no model was prepared."""
    if binding is None:
        if label is None:
            return None
        binding = LibreTranslateBinding(label, lambda: datetime.now(UTC))
    return binding.with_usage(meter)


def llm_settings(translator_name: str) -> LlmSettings:
    """The settings a model translator's run uses, so its estimate renders the same requests."""
    return OllamaSettings().llm if translator_name == OLLAMA else LlmSettings()


class DryRunEstimate:
    """Collects the requests a dry run would send and prices them."""

    def __init__(self, translator_name: str, model: str | None, price: ResolvedPrice | None, glossary: object,
                 source: str, target: str, counter: TokenCounter | None = None) -> None:
        self._price = price
        self._estimator = LlmUsageEstimator(llm_settings(translator_name), source, target, glossary_hints(glossary),
                                            counter=counter)
        self.words = 0

    def __repr__(self) -> str:
        return "DryRunEstimate()"

    def add(self, text: str) -> None:
        self._estimator.add(text)
        self.words += count_words(text)

    def summary(self) -> UsageEstimate:
        estimate, resolved = self._estimator, self._price
        price = None if resolved is None else resolved.price
        return UsageEstimate(
            estimate.calls, estimate.characters, self.words, estimate.input_tokens, estimate.output_tokens,
            estimate.worst_output_tokens, cost_micro_usd(price, estimate.input_tokens, estimate.output_tokens),
            cost_micro_usd(price, estimate.input_tokens, estimate.worst_output_tokens),
            None if resolved is None else resolved.version, None if resolved is None else resolved.source.value,
            approximate=estimate.approximate)


def dry_run_estimate(book, glossary) -> DryRunEstimate | None:
    """A model translator's dry run estimates its usage; LibreTranslate's is unchanged and costs nothing."""
    name = book.translator_name.strip().lower()
    if not names_model(name):
        return None
    model = book.translator_models.get(name)
    return DryRunEstimate(name, model, run_price(name, model, getattr(book, "prices", None)), glossary,
                          book.options.source, book.options.target)
