"""Schema validation of a structured answer, mapped to ids."""
from __future__ import annotations

import json

import pytest

from ayvu.providers.llm import StructuredOutputError, parse_translations

IDS = ("s0", "s1")


def document(*items):
    return json.dumps({"translations": [{"id": key, "text": text} for key, text in items]})


def test_ids_map_in_any_order():
    assert parse_translations(document(("s1", "Dois"), ("s0", "Um")), IDS) == {"s0": "Um", "s1": "Dois"}


@pytest.mark.parametrize("answer,code", [
    (document(("s0", "Um")), "missing_id"),
    (document(("s0", "Um"), ("s1", "Dois"), ("s2", "Tres")), "extra_id"),
    (document(("s0", "Um"), ("s0", "Um"), ("s1", "Dois")), "duplicate_id"),
    ('{"translations": [', "invalid_json"),
    ("NaN", "invalid_json"),
    ('{"translations": [{"id": "s0", "text": NaN}]}', "invalid_json"),
    ("[]", "invalid_shape"),
    ('{"translations": {}}', "invalid_shape"),
    ('{"translations": [], "note": "x"}', "invalid_shape"),
    ('{"translations": [{"id": "s0", "text": 1}, {"id": "s1", "text": "Dois"}]}', "invalid_shape"),
    ('{"translations": [{"id": "s0", "text": "a", "text": "b"}, {"id": "s1", "text": "Dois"}]}', "invalid_shape"),
])
def test_anything_outside_the_schema_fails_the_whole_answer(answer, code):
    with pytest.raises(StructuredOutputError, match=f"^{code}$"):
        parse_translations(answer, IDS)


def test_the_error_never_quotes_the_answer():
    with pytest.raises(StructuredOutputError) as caught:
        parse_translations("secret book text {", IDS)
    assert "secret" not in str(caught.value)
