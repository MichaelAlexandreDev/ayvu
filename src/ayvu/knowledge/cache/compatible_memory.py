"""Compatible memory candidates and query matching."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from .identity_values import (
    CacheIdentityError, CacheValueKind, MAX_COLLECTION_ENTRIES, _DIGEST,
)
from .scope import (
    AuthorizedCacheScope,
)


@dataclass(frozen=True, repr=False)
class CompatibleMemoryQuery:
    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)
    source_text: str = field(repr=False)
    max_candidates: int

    def __post_init__(self) -> None:
        if (type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind
                or not _DIGEST.fullmatch(self.semantic_fingerprint) or not isinstance(self.source_text, str)
                or type(self.max_candidates) is not int or not 1 <= self.max_candidates <= MAX_COLLECTION_ENTRIES):
            raise CacheIdentityError("invalid_memory_query")

    def __repr__(self) -> str:
        return "CompatibleMemoryQuery(<redacted>)"


@dataclass(frozen=True, repr=False)
class CacheCompatibilityIdentity:
    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind
                or not _DIGEST.fullmatch(self.semantic_fingerprint)):
            raise CacheIdentityError("invalid_memory_identity")

    def __repr__(self) -> str:
        return f"CacheCompatibilityIdentity(kind={self.value_kind.value!r}, identity=<redacted>)"


@dataclass(frozen=True, repr=False)
class CompatibleMemoryCandidate:
    original: str = field(repr=False)
    translated: str = field(repr=False)
    score: float
    applied: bool
    identity: CacheCompatibilityIdentity

    def __post_init__(self) -> None:
        if (not isinstance(self.original, str) or not isinstance(self.translated, str)
                or type(self.score) not in (int, float) or not math.isfinite(self.score) or not 0 <= self.score <= 1
                or type(self.applied) is not bool or type(self.identity) is not CacheCompatibilityIdentity):
            raise CacheIdentityError("invalid_memory_candidate")

    def __repr__(self) -> str:
        return f"CompatibleMemoryCandidate(score={self.score!r}, applied={self.applied})"


def candidate_matches_query(candidate: CompatibleMemoryCandidate, query: CompatibleMemoryQuery) -> bool:
    """Defense in depth for ports: no scope, kind or semantic mismatch may reach the pipeline."""
    return (type(candidate) is CompatibleMemoryCandidate and candidate.identity.scope == query.scope
            and candidate.identity.value_kind is query.value_kind
            and candidate.identity.semantic_fingerprint == query.semantic_fingerprint)
