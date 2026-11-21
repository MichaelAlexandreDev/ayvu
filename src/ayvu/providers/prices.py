"""What provider usage costs: a dated price table, its configuration overrides and the priced usage values.

Money is US dollars only. A price is dollars per million tokens, so one token at a price of ``p`` costs ``p``
micro-dollars, and every cost here is an integer number of micro-dollars, rounded up. Prices are never
hidden in code without a date: the table names its version, and a report cites it. A local provider costs
nothing. A model the table does not know has no price, and its cost is unknown, never zero.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .errors import ProviderSetupError

# The date the table below was last checked. A new price is a new version.
PRICE_TABLE_VERSION = "2026-11-20"
CONFIG_PRICE_VERSION = "config"
ANY_MODEL = "*"
MAX_PRICE = Decimal(1_000_000)  # dollars per million tokens; far above any real price
MAX_MICRO_USD = 2**62
_MODEL_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,128}")


class PriceSource(StrEnum):
    TABLE = "table"
    CONFIG = "config"


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ProviderSetupError("invalid_price")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ProviderSetupError("invalid_price") from None
    if not number.is_finite() or not Decimal(0) <= number <= MAX_PRICE:
        raise ProviderSetupError("invalid_price")
    return number


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per million tokens of each kind. Cached input is read from a provider's prompt cache."""

    input: Decimal
    output: Decimal
    cached_input: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("input", "output", "cached_input"):
            object.__setattr__(self, name, _price(getattr(self, name)))


NO_COST = ModelPrice(Decimal(0), Decimal(0), Decimal(0))

# Provider -> model -> price. ``ANY_MODEL`` prices every model of a provider. LibreTranslate and Ollama run
# on this machine and cost nothing; remote providers add their dated prices in their own issues.
PRICE_TABLE: Mapping[str, Mapping[str, ModelPrice]] = {
    "libretranslate": {ANY_MODEL: NO_COST},
    "ollama": {ANY_MODEL: NO_COST},
}


@dataclass(frozen=True)
class ResolvedPrice:
    """The price that applies to one run, and where it came from, as a report cites it."""

    price: ModelPrice
    source: PriceSource
    version: str


def price_overrides(data: object) -> dict[str, dict[str, ModelPrice]]:
    """``prices.<translator>.<model>`` from the configuration: ``{"input": ..., "output": ..., "cached_input": ...}``.

    Raises ``ProviderSetupError`` with a code; the message never quotes the value.
    """
    if not isinstance(data, Mapping):
        raise ProviderSetupError("invalid_price")
    parsed: dict[str, dict[str, ModelPrice]] = {}
    for translator, models in data.items():
        if not isinstance(translator, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", translator):
            raise ProviderSetupError("invalid_price")
        if not isinstance(models, Mapping) or len(models) > 64:
            raise ProviderSetupError("invalid_price")
        parsed[translator] = {}
        for model, value in models.items():
            if not isinstance(model, str) or not _MODEL_NAME.fullmatch(model) or not isinstance(value, Mapping):
                raise ProviderSetupError("invalid_price")
            if set(value) - {"input", "output", "cached_input"} or not {"input", "output"} <= set(value):
                raise ProviderSetupError("invalid_price")
            parsed[translator][model] = ModelPrice(value["input"], value["output"], value.get("cached_input", 0))
    return parsed


def resolve_price(provider: str, model: str | None,
                  overrides: Mapping[str, Mapping[str, ModelPrice]] | None = None) -> ResolvedPrice | None:
    """A configured price wins over the table; ``None`` when neither knows the model."""
    configured = (overrides or {}).get(provider, {})
    if model is not None and model in configured:
        return ResolvedPrice(configured[model], PriceSource.CONFIG, CONFIG_PRICE_VERSION)
    if ANY_MODEL in configured:
        return ResolvedPrice(configured[ANY_MODEL], PriceSource.CONFIG, CONFIG_PRICE_VERSION)
    table = PRICE_TABLE.get(provider, {})
    found = table.get(model) if model is not None else None
    found = found or table.get(ANY_MODEL)
    return None if found is None else ResolvedPrice(found, PriceSource.TABLE, PRICE_TABLE_VERSION)


def cost_micro_usd(price: ModelPrice | None, input_tokens: int | None, output_tokens: int | None,
                   cached_input_tokens: int | None = 0) -> int | None:
    """The cost in micro-dollars, rounded up. ``None`` when the price, or a priced count, is unknown."""
    if price is None:
        return None
    total = Decimal(0)
    for count, rate in ((input_tokens, price.input), (output_tokens, price.output),
                        (cached_input_tokens, price.cached_input)):
        if rate == 0:
            continue
        if count is None:
            return None
        total += Decimal(count) * rate
    return min(MAX_MICRO_USD, math.ceil(total))


def micro_usd(dollars: object) -> int:
    """A dollar amount given by a person (``--max-cost``), as micro-dollars, rounded down so a cap is never raised."""
    amount = _price(dollars)
    return math.floor(amount * 1_000_000)


def _add(left: int | None, right: int | None) -> int | None:
    return None if left is None or right is None else left + right


@dataclass(frozen=True)
class UsageRecord:
    """One provider request of one job: counts only, never text. ``None`` is unknown."""

    request_id: str
    job_id: str
    document_id: str
    unit_id: str
    provider: str
    model: str | None
    characters: int
    words: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cost_micro_usd: int | None
    price_version: str | None
    price_source: str | None


@dataclass(frozen=True)
class UsageTotals:
    """Sums of usage records. A total is unknown (``None``) as soon as one of its parts is."""

    calls: int = 0
    characters: int = 0
    words: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    cached_input_tokens: int | None = 0
    cost_micro_usd: int | None = 0

    def plus(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            self.calls + other.calls, self.characters + other.characters, self.words + other.words,
            _add(self.input_tokens, other.input_tokens), _add(self.output_tokens, other.output_tokens),
            _add(self.cached_input_tokens, other.cached_input_tokens),
            _add(self.cost_micro_usd, other.cost_micro_usd))

    @staticmethod
    def of(record: UsageRecord) -> UsageTotals:
        return UsageTotals(1, record.characters, record.words, record.input_tokens, record.output_tokens,
                           record.cached_input_tokens, record.cost_micro_usd)


@dataclass(frozen=True)
class UsageEstimate:
    """A dry run's estimate. ``approximate`` says the input was not counted by the provider's own tokenizer.

    ``worst_*`` is what the cost cap reserves: the estimated input plus the full output allowance.
    """

    calls: int
    characters: int
    words: int
    input_tokens: int
    output_tokens: int
    worst_output_tokens: int
    cost_micro_usd: int | None
    worst_cost_micro_usd: int | None
    price_version: str | None
    price_source: str | None
    approximate: bool = True
