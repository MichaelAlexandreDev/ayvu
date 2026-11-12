"""Cache-only requests and local compatible identity resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable
from .identity_values import (
    CacheIdentityError, CacheValueKind, MAX_CACHE_ONLY_CANDIDATES, _DIGEST,
    _safe_scalar,
)
from .scope import (
    AuthorizedCacheScope,
)
from .entries import (
    ExactEntryKey,
)


class CacheOnlyMode(StrEnum):
    PINNED = "pinned"
    RESOLVE_COMPATIBLE = "resolve_compatible"


@dataclass(frozen=True, repr=False)
class PinnedExecutionIdentityReference:
    """Opaque reference to an identity that the local durable boundary must resolve."""

    boundary_reference: str = field(repr=False)

    def __post_init__(self) -> None:
        _safe_scalar(self.boundary_reference, "invalid_pinned_identity")

    def __repr__(self) -> str:
        return "PinnedExecutionIdentityReference(<redacted>)"


@dataclass(frozen=True, repr=False)
class ResolvedCacheExecutionIdentity:
    """Identity metadata resolved from a local durable project/job/provenance boundary."""

    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind
                or not _DIGEST.fullmatch(self.semantic_fingerprint)):
            raise CacheIdentityError("invalid_resolved_identity")

    def __repr__(self) -> str:
        return f"ResolvedCacheExecutionIdentity(kind={self.value_kind.value!r}, identity=<redacted>)"


@dataclass(frozen=True, repr=False)
class CacheOnlyRequest:
    mode: CacheOnlyMode
    pin: PinnedExecutionIdentityReference | None = None

    def __post_init__(self) -> None:
        if type(self.mode) is not CacheOnlyMode:
            raise CacheIdentityError("invalid_cache_only_mode")
        if (self.mode is CacheOnlyMode.PINNED) != (self.pin is not None):
            raise CacheIdentityError("invalid_cache_only_request")
        if self.pin is not None and type(self.pin) is not PinnedExecutionIdentityReference:
            raise CacheIdentityError("invalid_cache_only_request")

    @classmethod
    def pinned(cls, reference: PinnedExecutionIdentityReference) -> CacheOnlyRequest:
        return cls(CacheOnlyMode.PINNED, reference)

    @classmethod
    def resolve_compatible(cls) -> CacheOnlyRequest:
        return cls(CacheOnlyMode.RESOLVE_COMPATIBLE)

    def __repr__(self) -> str:
        return f"CacheOnlyRequest(mode={self.mode.value!r})"


class CacheIdentityResolutionState(StrEnum):
    MISS = "miss"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    CORRUPT = "corrupt"


@dataclass(frozen=True, repr=False)
class CacheIdentityResolution:
    state: CacheIdentityResolutionState
    key: ExactEntryKey | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not CacheIdentityResolutionState:
            raise CacheIdentityError("invalid_identity_resolution")
        if (self.state is CacheIdentityResolutionState.RESOLVED) != (self.key is not None):
            raise CacheIdentityError("invalid_identity_resolution")
        if self.key is not None and type(self.key) is not ExactEntryKey:
            raise CacheIdentityError("invalid_identity_resolution")

    def __repr__(self) -> str:
        return f"CacheIdentityResolution(state={self.state.value!r})"


def resolve_compatible_identities(candidates: Iterable[ExactEntryKey], *, scope: AuthorizedCacheScope,
                                  value_kind: CacheValueKind, source_digest: str) -> CacheIdentityResolution:
    """Resolve only a bounded, same-scope local candidate set; never choose by order or age."""
    if type(scope) is not AuthorizedCacheScope or type(value_kind) is not CacheValueKind or not _DIGEST.fullmatch(source_digest):
        raise CacheIdentityError("invalid_identity_resolution")
    collected = tuple(candidates)
    if len(collected) > MAX_CACHE_ONLY_CANDIDATES:
        raise CacheIdentityError("cache_candidate_limit_exceeded")
    if any(type(candidate) is not ExactEntryKey for candidate in collected):
        raise CacheIdentityError("invalid_identity_resolution")
    if any(candidate.scope != scope for candidate in collected):
        raise CacheIdentityError("cache_scope_denied")
    if any(candidate.value_kind is not value_kind or candidate.source_content_digest != source_digest for candidate in collected):
        raise CacheIdentityError("invalid_compatible_candidate")
    identities = {(item.semantic_fingerprint, item.source_content_digest, item.value_kind, item.scope) for item in collected}
    if len(identities) != len(collected):
        return CacheIdentityResolution(CacheIdentityResolutionState.CORRUPT)
    if not collected:
        return CacheIdentityResolution(CacheIdentityResolutionState.MISS)
    if len(collected) > 1:
        return CacheIdentityResolution(CacheIdentityResolutionState.AMBIGUOUS)
    return CacheIdentityResolution(CacheIdentityResolutionState.RESOLVED, collected[0])
