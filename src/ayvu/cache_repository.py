"""Local v2 exact-cache adapter; v1 rows are never consulted."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .cache_identity import (
    CACHE_ENTRY_V2_LIMITS as LIMITS, MAX_CACHE_ONLY_CANDIDATES, AuthorizedCacheScope,
    CacheIdentityError, CacheLookupState, CacheMissReason, CacheValueKind, ExactCacheEntry,
    ExactCacheLookup, ExactEntryKey, PinnedExecutionIdentityReference, PutIfAbsentResult,
    PutIfAbsentState, source_content_digest, semantic_operation_from_canonical,
    _CONTENT_BEARING_MAPPING_KEYS, _CONTENT_BEARING_KEY_PARTS,
)
from .cache_schema import prepare_cache, participating_connection

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CacheIdentityError("invalid_cache_timestamp")
    result = value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if len(result) != LIMITS.timestamp or not _TIMESTAMP.fullmatch(result):
        raise CacheIdentityError("invalid_cache_timestamp")
    return result


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) != LIMITS.timestamp or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").isoformat(timespec="microseconds") + "Z" == value
    except ValueError:
        return False


def _validate_payload(key: ExactEntryKey, source: object, value: object, canonical: object,
                      metadata: object, provenance: object, timestamp: object | None = None) -> bool:
    if not isinstance(source, str) or not isinstance(value, str) or not all(isinstance(x, bytes) for x in (canonical, metadata, provenance)):
        return False
    try:
        if (len(source.encode("utf-8")) > LIMITS.source_text or len(value.encode("utf-8")) > LIMITS.value
                or len(canonical) > LIMITS.canonical_identity or len(metadata) > LIMITS.metadata
                or len(provenance) > LIMITS.provenance):
            return False
        if source_content_digest(source) != key.source_content_digest or _digest(canonical) != key.semantic_fingerprint:
            return False
        # Require one JSON value and valid UTF-8, with no non-finite values.
        semantic_operation_from_canonical(canonical)
        def unique(pairs):
            result = {}
            for name, item in pairs:
                if name in result:
                    raise ValueError()
                result[name] = item
            return result
        metadata_value = json.loads(metadata.decode("utf-8"), object_pairs_hook=unique,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(metadata_value, dict):
            return False
        for name, item in metadata_value.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
                    or name in _CONTENT_BEARING_MAPPING_KEYS or any(part in name for part in _CONTENT_BEARING_KEY_PARTS)):
                return False
            values = item if isinstance(item, list) else [item]
            if any(value is not None and type(value) not in (int, bool) for value in values):
                return False
            if any(type(value) is int and not -(2**63 - 1) <= value <= 2**63 - 1 for value in values):
                return False
        # Provenance is serialized from the closed DTO on write; validate its shape on read.
        obj = json.loads(provenance.decode("utf-8"))
        if not isinstance(obj, dict) or set(obj) != {"operation", "producer", "pipeline_revision"}:
            return False
        return timestamp is None or _valid_timestamp(timestamp)
    except (ValueError, UnicodeError, CacheIdentityError, RecursionError):
        return False


def _key_values(key: ExactEntryKey) -> tuple[str, str, str, str, str]:
    return (key.scope.scope.kind.value, key.scope.scope.scope_id, key.value_kind.value,
            key.semantic_fingerprint, key.source_content_digest)


def _entry_digest(key: ExactEntryKey, source: str, value: str, canonical: bytes,
                  metadata: bytes, provenance: bytes, timestamp: str, provenance_digest: str) -> str:
    parts = (*_key_values(key), source, value, canonical, metadata, provenance, timestamp, provenance_digest)
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8") if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def _valid_row(key: ExactEntryKey, row: tuple) -> bool:
    if len(row) != 8 or not _validate_payload(key, *row[:6]):
        return False
    source, value, canonical, metadata, provenance, timestamp, provenance_digest, entry_digest = row
    return (provenance_digest == _digest(provenance)
            and entry_digest == _entry_digest(key, source, value, canonical, metadata, provenance, timestamp, provenance_digest))


class SQLiteExactCache:
    """Explicit v2 boundary. Each operation participates in migration locking."""

    def __init__(self, path: Path, *, authorized_directory: Path | None = None,
                 clock: Callable[[], datetime] | None = None, checkpoint=None) -> None:
        self._path = Path(path)
        self._authorized_directory = authorized_directory
        self._clock = clock or (lambda: datetime.now(UTC))
        prepare_cache(self._path, authorized_directory=authorized_directory, checkpoint=checkpoint)

    def __repr__(self) -> str:
        return "SQLiteExactCache(<redacted>)"

    def resolve_pinned(self, reference: PinnedExecutionIdentityReference, scope: AuthorizedCacheScope):
        # A cache record is not an authoritative project/job identity boundary.
        if type(reference) is not PinnedExecutionIdentityReference or type(scope) is not AuthorizedCacheScope:
            raise CacheIdentityError("invalid_pinned_identity")
        return None

    def lookup(self, key: ExactEntryKey) -> ExactCacheLookup:
        if type(key) is not ExactEntryKey:
            raise CacheIdentityError("invalid_exact_entry_key")
        try:
            with participating_connection(self._path, authorized_directory=self._authorized_directory) as connection:
                row = connection.execute("SELECT source_text, value, canonical_identity, metadata, provenance, created_at, provenance_digest, entry_digest FROM cache_entries_v2 WHERE scope_kind=? AND scope_id=? AND value_kind=? AND semantic_fingerprint=? AND source_digest=?", _key_values(key)).fetchone()
            if row is None:
                return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)
            if not _valid_row(key, row):
                raise CacheIdentityError("cache_entry_corrupt")
            return ExactCacheLookup(CacheLookupState.HIT, value=row[1])
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("cache_repository_failed") from None

    def resolve_compatible(self, scope: AuthorizedCacheScope, value_kind: CacheValueKind,
                           source_digest: str, *, limit: int) -> tuple[ExactEntryKey, ...]:
        if (type(scope) is not AuthorizedCacheScope or type(value_kind) is not CacheValueKind
                or not isinstance(source_digest, str) or not _DIGEST.fullmatch(source_digest)
                or type(limit) is not int or not 1 <= limit <= MAX_CACHE_ONLY_CANDIDATES):
            raise CacheIdentityError("invalid_identity_resolution")
        try:
            with participating_connection(self._path, authorized_directory=self._authorized_directory) as connection:
                rows = connection.execute("SELECT semantic_fingerprint, source_text, value, canonical_identity, metadata, provenance, created_at, provenance_digest, entry_digest FROM cache_entries_v2 WHERE scope_kind=? AND scope_id=? AND value_kind=? AND source_digest=? LIMIT ?", (scope.scope.kind.value, scope.scope.scope_id, value_kind.value, source_digest, limit + 1)).fetchall()
            if len(rows) > limit:
                raise CacheIdentityError("cache_candidate_limit_exceeded")
            result = []
            for fingerprint, *payload in rows:
                key = ExactEntryKey(scope, value_kind, fingerprint, source_digest)
                if not _valid_row(key, tuple(payload)):
                    raise CacheIdentityError("cache_entry_corrupt")
                result.append(key)
            return tuple(result)
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("cache_repository_failed") from None

    def put_if_absent(self, entry: ExactCacheEntry) -> PutIfAbsentResult:
        if type(entry) is not ExactCacheEntry:
            raise CacheIdentityError("invalid_cache_entry")
        try:
            provenance = _json(asdict(entry.provenance))
            if not _validate_payload(entry.key, entry.source_text, entry.value, entry.canonical_identity,
                                     entry.metadata, provenance):
                return PutIfAbsentResult(PutIfAbsentState.NOT_CACHEABLE)
            with participating_connection(self._path, authorized_directory=self._authorized_directory) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT source_text, value, canonical_identity, metadata, provenance, created_at, provenance_digest, entry_digest FROM cache_entries_v2 WHERE scope_kind=? AND scope_id=? AND value_kind=? AND semantic_fingerprint=? AND source_digest=?", _key_values(entry.key)).fetchone()
                if row is not None:
                    if not _valid_row(entry.key, row):
                        raise CacheIdentityError("cache_entry_corrupt")
                    state = (PutIfAbsentState.IDEMPOTENT if row[:5] == (entry.source_text, entry.value, entry.canonical_identity, entry.metadata, provenance)
                             else PutIfAbsentState.CONFLICT)
                else:
                    created = _timestamp(self._clock)
                    provenance_digest = _digest(provenance)
                    entry_digest = _entry_digest(entry.key, entry.source_text, entry.value, entry.canonical_identity,
                                                 entry.metadata, provenance, created, provenance_digest)
                    connection.execute("INSERT INTO cache_entries_v2 (scope_kind, scope_id, value_kind, semantic_fingerprint, source_digest, canonical_identity, source_text, value, metadata, provenance, created_at, provenance_digest, entry_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*_key_values(entry.key), entry.canonical_identity, entry.source_text, entry.value, entry.metadata, provenance, created, provenance_digest, entry_digest))
                    state = PutIfAbsentState.INSERTED
                connection.commit()
            return PutIfAbsentResult(state)
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("cache_repository_failed") from None
