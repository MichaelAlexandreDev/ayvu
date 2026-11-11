"""Cache namespace authority and explicit scope authorization."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from ...kernel import validate_job_identity
from .identity_values import (
    CacheIdentityError, MAX_COLLECTION_ENTRIES, _safe_scalar,
)


class CacheScopeKind(StrEnum):
    PROJECT = "project"
    LOCAL_USER = "local_user"
    WORK = "work"
    LEGACY_COMPATIBILITY = "legacy_compatibility"


_CACHE_SCOPE_AUTHORIZATION_TOKEN = object()


@dataclass(frozen=True, repr=False)
class CacheScope:
    kind: CacheScopeKind
    scope_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not CacheScopeKind:
            raise CacheIdentityError("invalid_cache_scope")
        _safe_scalar(self.scope_id, "invalid_cache_scope")

    def __repr__(self) -> str:
        return f"CacheScope(kind={self.kind.value!r}, scope_id=<redacted>)"


@dataclass(frozen=True, repr=False, init=False)
class AuthorizedCacheScope:
    project_id: str = field(repr=False)
    scope: CacheScope
    explicitly_broader: bool = False

    def __init__(self, project_id: str, scope: CacheScope, explicitly_broader: bool = False, *,
                 _authorization_token: object | None = None) -> None:
        if _authorization_token is not _CACHE_SCOPE_AUTHORIZATION_TOKEN:
            raise CacheIdentityError("cache_scope_denied")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "explicitly_broader", explicitly_broader)
        try:
            validate_job_identity(self.project_id)
        except Exception:
            raise CacheIdentityError("invalid_cache_scope") from None
        if type(self.scope) is not CacheScope or type(self.explicitly_broader) is not bool:
            raise CacheIdentityError("invalid_cache_scope")
        if self.scope.kind is CacheScopeKind.LEGACY_COMPATIBILITY:
            raise CacheIdentityError("legacy_scope_isolated")
        if self.scope.kind is CacheScopeKind.PROJECT:
            if self.scope.scope_id != self.project_id or self.explicitly_broader:
                raise CacheIdentityError("cache_scope_denied")
        elif not self.explicitly_broader:
            raise CacheIdentityError("cache_scope_denied")

    def __repr__(self) -> str:
        return f"AuthorizedCacheScope(kind={self.scope.kind.value!r}, scope_id=<redacted>)"


@dataclass(frozen=True, repr=False)
class CacheAuthorizationPolicy:
    project_id: str = field(repr=False)
    broader_scopes: tuple[CacheScope, ...] = ()

    def __post_init__(self) -> None:
        try:
            validate_job_identity(self.project_id)
        except Exception:
            raise CacheIdentityError("invalid_cache_scope") from None
        if type(self.broader_scopes) is not tuple or len(self.broader_scopes) > MAX_COLLECTION_ENTRIES:
            raise CacheIdentityError("collection_limit_exceeded")
        if any(type(item) is not CacheScope or item.kind in (CacheScopeKind.PROJECT, CacheScopeKind.LEGACY_COMPATIBILITY)
               for item in self.broader_scopes) or len(set(self.broader_scopes)) != len(self.broader_scopes):
            raise CacheIdentityError("invalid_cache_scope_policy")

    def resolve(self, scope: CacheScope | None = None) -> AuthorizedCacheScope:
        if scope is None:
            scope = CacheScope(CacheScopeKind.PROJECT, self.project_id)
        if type(scope) is not CacheScope:
            raise CacheIdentityError("cache_scope_denied")
        if scope.kind is CacheScopeKind.PROJECT and scope.scope_id == self.project_id:
            return AuthorizedCacheScope(self.project_id, scope, _authorization_token=_CACHE_SCOPE_AUTHORIZATION_TOKEN)
        if scope in self.broader_scopes:
            return AuthorizedCacheScope(self.project_id, scope, explicitly_broader=True,
                                        _authorization_token=_CACHE_SCOPE_AUTHORIZATION_TOKEN)
        raise CacheIdentityError("cache_scope_denied")


def legacy_compatibility_scope() -> CacheScope:
    """One named v1 namespace. V2 authorization never returns it."""
    return CacheScope(CacheScopeKind.LEGACY_COMPATIBILITY, "legacy-v1")
