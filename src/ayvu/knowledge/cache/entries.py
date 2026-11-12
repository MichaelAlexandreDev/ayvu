"""Exact cache keys, entries, metadata and repository outcomes."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from ...provenance import ProducerIdentity
from .identity_values import (
    CacheIdentityError, CacheValueKind, OpaqueSemanticRevision, _DIGEST,
    _slug,
)
from .scope import (
    AuthorizedCacheScope,
)


@dataclass(frozen=True, repr=False)
class ExactEntryKey:
    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)
    source_content_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind:
            raise CacheIdentityError("invalid_exact_entry_key")
        if not _DIGEST.fullmatch(self.semantic_fingerprint) or not _DIGEST.fullmatch(self.source_content_digest):
            raise CacheIdentityError("invalid_exact_entry_key")

    def __repr__(self) -> str:
        return f"ExactEntryKey(kind={self.value_kind.value!r}, identity=<redacted>)"


def source_content_digest(text: str) -> str:
    if not isinstance(text, str):
        raise CacheIdentityError("invalid_source_content")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise CacheIdentityError("invalid_source_content") from None
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def exact_entry_key(scope: AuthorizedCacheScope, value_kind: CacheValueKind, semantic_fingerprint: str,
                    source_text: str) -> ExactEntryKey:
    return ExactEntryKey(scope, value_kind, semantic_fingerprint, source_content_digest(source_text))


@dataclass(frozen=True, repr=False)
class CanonicalCacheRecord:
    """Validated stored evidence; construction belongs to the cache adapters."""

    key: ExactEntryKey
    source_text: str = field(repr=False)
    value: str = field(repr=False)
    canonical_identity: bytes = field(repr=False)
    metadata: bytes = field(repr=False)
    provenance: bytes = field(repr=False)
    created_at: str = field(repr=False)
    provenance_digest: str = field(repr=False)
    entry_digest: str = field(repr=False)

    def __repr__(self) -> str:
        return "CanonicalCacheRecord(<redacted>)"


class CacheLookupState(StrEnum):
    HIT = "hit"
    MISS = "miss"
    AMBIGUOUS = "ambiguous"
    CORRUPT = "corrupt"


class CacheMissReason(StrEnum):
    ENTRY_NOT_FOUND = "entry_not_found"
    CACHE_INELIGIBLE = "cache_ineligible"
    IDENTITY_MISMATCH = "identity_mismatch"
    SCOPE_DENIED = "scope_denied"
    AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
    DUPLICATE_EXACT_KEY = "duplicate_exact_key"
    LEGACY_NAMESPACE_ISOLATED = "legacy_namespace_isolated"


@dataclass(frozen=True, repr=False)
class ExactCacheLookup:
    state: CacheLookupState
    value: str | None = field(default=None, repr=False)
    reason: CacheMissReason | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not CacheLookupState or (self.state is CacheLookupState.HIT) != (self.value is not None):
            raise CacheIdentityError("invalid_cache_lookup")
        if self.value is not None and not isinstance(self.value, str):
            raise CacheIdentityError("invalid_cache_lookup")
        if self.state is CacheLookupState.HIT and self.reason is not None:
            raise CacheIdentityError("invalid_cache_lookup")
        if self.state is not CacheLookupState.HIT and type(self.reason) is not CacheMissReason:
            raise CacheIdentityError("invalid_cache_lookup")

    def __repr__(self) -> str:
        return f"ExactCacheLookup(state={self.state.value!r}, reason={None if self.reason is None else self.reason.value!r})"


@dataclass(frozen=True, repr=False)
class CacheProvenanceMetadata:
    operation: str
    producer: ProducerIdentity
    pipeline_revision: OpaqueSemanticRevision

    def __post_init__(self) -> None:
        _slug(self.operation, "invalid_cache_provenance")
        if type(self.producer) is not ProducerIdentity or type(self.pipeline_revision) is not OpaqueSemanticRevision:
            raise CacheIdentityError("invalid_cache_provenance")

    def __repr__(self) -> str:
        return "CacheProvenanceMetadata(<redacted>)"


@dataclass(frozen=True, repr=False)
class ExactCacheEntry:
    key: ExactEntryKey
    value: str = field(repr=False)
    provenance: CacheProvenanceMetadata
    source_text: str | None = field(default=None, repr=False)
    canonical_identity: bytes | None = field(default=None, repr=False)
    metadata: bytes = field(default=b"{}", repr=False)

    def __post_init__(self) -> None:
        if type(self.key) is not ExactEntryKey or not isinstance(self.value, str) or type(self.provenance) is not CacheProvenanceMetadata:
            raise CacheIdentityError("invalid_cache_entry")
        if (self.source_text is not None and not isinstance(self.source_text, str)
                or self.canonical_identity is not None and not isinstance(self.canonical_identity, bytes)
                or not isinstance(self.metadata, bytes)):
            raise CacheIdentityError("invalid_cache_entry")

    def __repr__(self) -> str:
        return "ExactCacheEntry(<redacted>)"


class PutIfAbsentState(StrEnum):
    INSERTED = "inserted"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    NOT_CACHEABLE = "not_cacheable"


@dataclass(frozen=True, repr=False)
class PutIfAbsentResult:
    state: PutIfAbsentState

    def __post_init__(self) -> None:
        if type(self.state) is not PutIfAbsentState:
            raise CacheIdentityError("invalid_put_result")

    def __repr__(self) -> str:
        return f"PutIfAbsentResult(state={self.state.value!r})"
