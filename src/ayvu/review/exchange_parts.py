from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from ..content import TranslationUnit
from ..content import INTERNAL_MARKER_PATTERN, TAG_TOKEN_PATTERN
from .exchange_model import ReviewExchangeError, _PLACEHOLDER, _PLACEHOLDER_LIKE


_LOCAL_TOKEN = re.compile(r"__AYVU_TAG_(\d+)__")


def _plain_local_tokens(text: str, part_count: int) -> str:
    """Reject damaged, missing, repeated, invented, or reordered local reconstruction tokens."""
    found = list(_LOCAL_TOKEN.finditer(text))
    tokens = [match.group(0) for match in found]
    expected = [f"__AYVU_TAG_{index}__" for index in range(part_count)]
    if tokens != expected:
        if len(set(tokens)) != len(tokens):
            raise ReviewExchangeError("duplicate_placeholder")
        if any(token not in expected for token in tokens):
            raise ReviewExchangeError("unknown_placeholder")
        if len(tokens) != len(expected):
            raise ReviewExchangeError("missing_placeholder")
        raise ReviewExchangeError("invalid_placeholder_order")
    stripped = _LOCAL_TOKEN.sub("", text)
    if INTERNAL_MARKER_PATTERN.search(stripped):
        raise ReviewExchangeError("malformed_placeholder")
    return text


def _source_review_text(unit: TranslationUnit, package_id: str) -> str:
    text = unit.source_text
    result: list[str] = []
    cursor = 0
    owner = _unit_owner(package_id, unit.unit_id)
    for index, part in enumerate(unit.content.protected_parts):
        plain = text[cursor:part.start]
        if TAG_TOKEN_PATTERN.search(plain) or _PLACEHOLDER_LIKE in plain:
            raise ReviewExchangeError("reserved_placeholder_in_source")
        result.extend((plain, _placeholder(owner, index)))
        cursor = part.end
    plain = text[cursor:]
    if TAG_TOKEN_PATTERN.search(plain) or _PLACEHOLDER_LIKE in plain:
        raise ReviewExchangeError("reserved_placeholder_in_source")
    result.append(plain)
    return "".join(result)


def _machine_review_text(text: str, package_id: str, parts: Sequence[object], *, unit_id: str = "") -> str:
    owner = _unit_owner(package_id, unit_id)
    expected = {f"__AYVU_TAG_{index}__": _placeholder(owner, index) for index in range(len(parts))}
    return _LOCAL_TOKEN.sub(lambda match: expected.get(match.group(0), match.group(0)), text)


def _unit_owner(package_id: str, unit_id: str) -> str:
    return hashlib.sha256(f"{package_id}\0{unit_id}".encode("utf-8")).hexdigest()[:32]


def _placeholder(owner: str, index: int) -> str:
    return f"__AYVU_REVIEW_PART_{owner}_{index:04d}__"


def _validate_review_placeholders(text: str, expected: Sequence[str]) -> None:
    matches = list(_PLACEHOLDER.finditer(text))
    pieces = [match.group(0) for match in matches]
    if _PLACEHOLDER_LIKE in _PLACEHOLDER.sub("", text):
        raise ReviewExchangeError("malformed_placeholder")
    if len(pieces) != len(set(pieces)):
        raise ReviewExchangeError("duplicate_placeholder")
    unknown = set(pieces) - set(expected)
    if unknown:
        raise ReviewExchangeError("foreign_placeholder")
    missing = set(expected) - set(pieces)
    if missing:
        raise ReviewExchangeError("missing_placeholder")
    if pieces != list(expected):
        raise ReviewExchangeError("invalid_placeholder_order")


def _to_local_tokens(text: str, expected: Sequence[str]) -> str:
    lookup = {marker: f"__AYVU_TAG_{index}__" for index, marker in enumerate(expected)}
    return _PLACEHOLDER.sub(lambda match: lookup[match.group(0)], text)


def _validate_local_tokens_for_unit(text: str, unit: TranslationUnit) -> None:
    found = [match.group(0) for match in _LOCAL_TOKEN.finditer(text)]
    expected = [f"__AYVU_TAG_{index}__" for index in range(len(unit.content.protected_parts))]
    if found != expected:
        if len(found) != len(set(found)):
            raise ReviewExchangeError("duplicate_placeholder")
        if any(item not in expected for item in found):
            raise ReviewExchangeError("unknown_placeholder")
        if len(found) != len(expected):
            raise ReviewExchangeError("missing_placeholder")
        raise ReviewExchangeError("invalid_placeholder_order")
    if INTERNAL_MARKER_PATTERN.search(_LOCAL_TOKEN.sub("", text)):
        raise ReviewExchangeError("malformed_placeholder")
    # XML-forbidden code points cannot enter structural reconstruction.
    if re.search(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", _LOCAL_TOKEN.sub("", text)):
        raise ReviewExchangeError("invalid_text")
