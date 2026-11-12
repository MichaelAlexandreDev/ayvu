"""Bounded read queries sharing the exact-cache payload validator."""
from __future__ import annotations

from .entries import CanonicalCacheRecord, ExactEntryKey
from .identity_values import (
    CACHE_ENTRY_V2_LIMITS as LIMITS, MAX_CACHE_ONLY_CANDIDATES, CacheIdentityError,
    TRANSLATION_STAGE_AGGREGATE, _DIGEST,
)
from .repository import _key_values, _valid_row
from .scope import AuthorizedCacheScope

_PAYLOAD = ("source_text", "value", "canonical_identity", "metadata", "provenance",
            "created_at", "provenance_digest", "entry_digest")
_BOUNDS = (LIMITS.source_text, LIMITS.value, LIMITS.canonical_identity, LIMITS.metadata,
           LIMITS.provenance, LIMITS.timestamp, 71, 71)
_LENGTHS = ",".join(f"length(CAST({name} AS BLOB))" for name in _PAYLOAD)
_EXACT = "scope_kind=? AND scope_id=? AND value_kind=? AND semantic_fingerprint=? AND source_digest=?"


def validate_resolution(scope, value_kind, source_digest, limit):
    if (type(scope) is not AuthorizedCacheScope or value_kind != TRANSLATION_STAGE_AGGREGATE
            or type(value_kind) is not type(TRANSLATION_STAGE_AGGREGATE)
            or not isinstance(source_digest, str) or not _DIGEST.fullmatch(source_digest)
            or type(limit) is not int or not 1 <= limit <= MAX_CACHE_ONLY_CANDIDATES):
        raise CacheIdentityError("invalid_identity_resolution")


def _check_lengths(lengths):
    if any(type(size) is not int or not 0 <= size <= bound for size, bound in zip(lengths, _BOUNDS)):
        raise CacheIdentityError("cache_entry_corrupt")


def _record(connection, key):
    rows = connection.execute("SELECT " + ",".join(_PAYLOAD) +
                              " FROM cache_entries_v2 WHERE " + _EXACT + " LIMIT 2", _key_values(key)).fetchall()
    if len(rows) != 1 or not _valid_row(key, rows[0]):
        raise CacheIdentityError("cache_entry_corrupt")
    return CanonicalCacheRecord(key, *rows[0])


def exact_record(connection, key):
    lengths = connection.execute("SELECT " + _LENGTHS + " FROM cache_entries_v2 WHERE " +
                                 _EXACT + " LIMIT 2", _key_values(key)).fetchall()
    if not lengths:
        return None
    if len(lengths) != 1:
        raise CacheIdentityError("cache_entry_corrupt")
    _check_lengths(lengths[0])
    return _record(connection, key)


def candidate_records(connection, scope, value_kind, source_digest, limit):
    rows = connection.execute(
        "SELECT CASE WHEN length(CAST(semantic_fingerprint AS BLOB))=71 "
        "THEN semantic_fingerprint END," + _LENGTHS + " FROM cache_entries_v2 WHERE "
        "scope_kind=? AND scope_id=? AND value_kind=? AND source_digest=? "
        "ORDER BY semantic_fingerprint LIMIT ?",
        (scope.scope.kind.value, scope.scope.scope_id, value_kind.value, source_digest, limit + 1)).fetchall()
    if len(rows) > limit:
        raise CacheIdentityError("cache_candidate_limit_exceeded")
    result = []
    for fingerprint, *lengths in rows:
        _check_lengths(lengths)
        try:
            key = ExactEntryKey(scope, value_kind, fingerprint, source_digest)
        except (CacheIdentityError, TypeError):
            raise CacheIdentityError("cache_entry_corrupt") from None
        result.append(_record(connection, key))
    return tuple(result)
