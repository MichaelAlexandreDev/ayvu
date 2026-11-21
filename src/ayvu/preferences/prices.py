"""The ``prices`` preference (#380): price overrides per translator and model, in US dollars per million tokens.

``config.json`` holds ``{"prices": {"<translator>": {"<model>": {"input": "0.25", "output": "1.25",
"cached_input": "0.025"}}}}``; ``cached_input`` is optional and ``"*"`` prices every model of a translator.
The rules of a valid price belong to ``providers.prices``; this module only keeps the value immutable and
writes it back as strings, so a saved price keeps its exact decimal form.
"""
from __future__ import annotations

from typing import Callable

from ..providers import ProviderSetupError, price_overrides

# (translator, model, input, output, cached_input), sorted.
PriceRows = tuple[tuple[str, str, str, str, str], ...]


def prices_from_mapping(data: object, error: Callable[[], Exception]) -> PriceRows:
    """Raises ``error()`` for anything ``providers.prices`` does not accept. An absent or empty value is no price."""
    if data is None or data == {}:
        return ()
    try:
        parsed = price_overrides(data)
    except ProviderSetupError:
        raise error() from None
    return tuple(sorted((translator, model, format(price.input, "f"), format(price.output, "f"),
                         format(price.cached_input, "f"))
                        for translator, models in parsed.items() for model, price in models.items()))


def check_price_rows(rows: object, error: Callable[[], Exception]) -> None:
    if type(rows) is not tuple or any(type(row) is not tuple or len(row) != 5 for row in rows):
        raise error()
    prices_from_mapping(prices_to_mapping(rows), error)


def prices_to_mapping(rows: PriceRows) -> dict[str, dict[str, dict[str, str]]]:
    mapping: dict[str, dict[str, dict[str, str]]] = {}
    for translator, model, input_price, output_price, cached in rows:
        mapping.setdefault(translator, {})[model] = {"input": input_price, "output": output_price,
                                                     "cached_input": cached}
    return mapping
