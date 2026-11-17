"""Read a model's structured answer strictly, mapping it to segment ids and never to positions."""
from __future__ import annotations

import json

from .model import LlmCoreError

MAX_ANSWER_CHARS = 16 * 1024 * 1024


class StructuredOutputError(LlmCoreError):
    """The answer as a whole is unusable. Its code names why; the answer itself is never quoted."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise StructuredOutputError("invalid_shape")
    return dict(pairs)


def _load(text: str) -> object:
    if not isinstance(text, str) or len(text) > MAX_ANSWER_CHARS:
        raise StructuredOutputError("invalid_json")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except StructuredOutputError:
        raise
    except (ValueError, RecursionError):
        raise StructuredOutputError("invalid_json") from None


def parse_translations(text: str, expected_ids: tuple[str, ...]) -> dict[str, str]:
    """``{"translations": [{"id": str, "text": str}]}`` with exactly the expected ids, in any order.

    A missing, extra or duplicate id, or anything else outside the schema, fails the whole answer:
    a model that confused its ids cannot be trusted on the ids it got right.
    """
    document = _load(text)
    if type(document) is not dict or set(document) != {"translations"}:
        raise StructuredOutputError("invalid_shape")
    items = document["translations"]
    if type(items) is not list:
        raise StructuredOutputError("invalid_shape")
    found: dict[str, str] = {}
    for item in items:
        if (type(item) is not dict or set(item) != {"id", "text"} or type(item["id"]) is not str
                or type(item["text"]) is not str):
            raise StructuredOutputError("invalid_shape")
        if item["id"] in found:
            raise StructuredOutputError("duplicate_id")
        found[item["id"]] = item["text"]
    expected = set(expected_ids)
    if set(found) - expected:
        raise StructuredOutputError("extra_id")
    if expected - set(found):
        raise StructuredOutputError("missing_id")
    return found
