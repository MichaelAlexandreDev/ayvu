from __future__ import annotations

from dataclasses import replace

import pytest

from ayvu.knowledge.cache.identity_values import MAX_CACHE_ONLY_CANDIDATES, MAX_COLLECTION_ENTRIES, CacheIdentityError, CacheValueKind
from ayvu.knowledge.cache.scope import AuthorizedCacheScope, CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from ayvu.knowledge.cache.compatible_memory import (
    CacheCompatibilityIdentity, CompatibleMemoryCandidate, CompatibleMemoryQuery, candidate_matches_query,
)
from ayvu.knowledge.cache.cache_only import (
    CacheIdentityResolutionState, CacheOnlyMode, CacheOnlyRequest, PinnedExecutionIdentityReference,
    resolve_compatible_identities,
)
from ayvu.knowledge.cache.entries import ExactEntryKey, exact_entry_key, source_content_digest

from tests.knowledge.cache.cache_identity_support import PROJECT, VALUE_KIND
from tests.knowledge.cache.cache_identity_support import operation


def test_scope_defaults_to_project_and_broader_scopes_need_explicit_policy():
    default = CacheAuthorizationPolicy(PROJECT).resolve()
    local = CacheScope(CacheScopeKind.LOCAL_USER, "user-local")
    work = CacheScope(CacheScopeKind.WORK, "work-1")
    with pytest.raises(CacheIdentityError, match="^cache_scope_denied$"):
        CacheAuthorizationPolicy(PROJECT).resolve(local)
    with pytest.raises(CacheIdentityError, match="^cache_scope_denied$"):
        AuthorizedCacheScope(PROJECT, local, explicitly_broader=True)
    assert CacheAuthorizationPolicy(PROJECT, (local, work)).resolve(local).explicitly_broader
    assert CacheAuthorizationPolicy(PROJECT, (local, work)).resolve(work).explicitly_broader
    with pytest.raises(CacheIdentityError, match="^cache_scope_denied$"):
        AuthorizedCacheScope(PROJECT, CacheScope(CacheScopeKind.LEGACY_COMPATIBILITY, "legacy-v1"))
    project_key = exact_entry_key(default, VALUE_KIND, operation().fingerprint, "source")
    local_key = exact_entry_key(CacheAuthorizationPolicy(PROJECT, (local,)).resolve(local), VALUE_KIND,
                                operation().fingerprint, "source")
    assert project_key.semantic_fingerprint == local_key.semantic_fingerprint
    assert project_key.scope != local_key.scope


def test_authorized_broader_scope_collection_obeys_256_entry_ceiling():
    scopes = tuple(CacheScope(CacheScopeKind.WORK, f"work-{index}") for index in range(MAX_COLLECTION_ENTRIES))
    assert CacheAuthorizationPolicy(PROJECT, scopes).resolve(scopes[-1]).explicitly_broader
    with pytest.raises(CacheIdentityError, match="^collection_limit_exceeded$"):
        CacheAuthorizationPolicy(PROJECT, (*scopes, CacheScope(CacheScopeKind.WORK, "work-over")))


def test_cache_only_has_only_pinned_and_unpinned_resolve_compatible_modes():
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    key = exact_entry_key(scope, VALUE_KIND, operation().fingerprint, "source")
    reference = PinnedExecutionIdentityReference("job:execution-17")
    pinned = CacheOnlyRequest.pinned(reference)
    unpinned = CacheOnlyRequest.resolve_compatible()
    assert pinned.mode is CacheOnlyMode.PINNED and unpinned.mode is CacheOnlyMode.RESOLVE_COMPATIBLE
    with pytest.raises(CacheIdentityError, match="^invalid_cache_only_request$"):
        CacheOnlyRequest(CacheOnlyMode.PINNED)
    with pytest.raises(CacheIdentityError, match="^invalid_cache_only_request$"):
        CacheOnlyRequest(CacheOnlyMode.RESOLVE_COMPATIBLE, reference)


def test_unpinned_resolution_handles_zero_one_many_and_duplicate_exact_rows():
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    digest = source_content_digest("source")
    first = ExactEntryKey(scope, VALUE_KIND, "sha256:" + "1" * 64, digest)
    second = ExactEntryKey(scope, VALUE_KIND, "sha256:" + "2" * 64, digest)
    assert resolve_compatible_identities((), scope=scope, value_kind=VALUE_KIND, source_digest=digest).state is CacheIdentityResolutionState.MISS
    resolved = resolve_compatible_identities((first,), scope=scope, value_kind=VALUE_KIND, source_digest=digest)
    assert resolved.state is CacheIdentityResolutionState.RESOLVED and resolved.key == first
    assert resolve_compatible_identities((first, second), scope=scope, value_kind=VALUE_KIND,
                                         source_digest=digest).state is CacheIdentityResolutionState.AMBIGUOUS
    at_limit = tuple(ExactEntryKey(scope, VALUE_KIND, f"sha256:{index:064x}", digest)
                     for index in range(MAX_CACHE_ONLY_CANDIDATES))
    assert resolve_compatible_identities(at_limit, scope=scope, value_kind=VALUE_KIND,
                                         source_digest=digest).state is CacheIdentityResolutionState.AMBIGUOUS
    assert resolve_compatible_identities((first, first), scope=scope, value_kind=VALUE_KIND,
                                         source_digest=digest).state is CacheIdentityResolutionState.CORRUPT
    with pytest.raises(CacheIdentityError, match="^cache_candidate_limit_exceeded$"):
        resolve_compatible_identities((first,) * (MAX_CACHE_ONLY_CANDIDATES + 1), scope=scope,
                                      value_kind=VALUE_KIND, source_digest=digest)


def test_unpinned_resolution_refuses_a_candidate_from_another_scope_or_value_kind():
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    other_scope = CacheAuthorizationPolicy(PROJECT, (CacheScope(CacheScopeKind.LOCAL_USER, "u1"),)).resolve(
        CacheScope(CacheScopeKind.LOCAL_USER, "u1"))
    digest = source_content_digest("source")
    foreign = ExactEntryKey(other_scope, VALUE_KIND, operation().fingerprint, digest)
    with pytest.raises(CacheIdentityError, match="^cache_scope_denied$"):
        resolve_compatible_identities((foreign,), scope=scope, value_kind=VALUE_KIND, source_digest=digest)
    other_kind = CacheValueKind("translation_stage_fragment", 1)
    wrong_kind = ExactEntryKey(scope, other_kind, operation().fingerprint, digest)
    with pytest.raises(CacheIdentityError, match="^invalid_compatible_candidate$"):
        resolve_compatible_identities((wrong_kind,), scope=scope, value_kind=VALUE_KIND, source_digest=digest)


def test_fuzzy_query_omits_exact_source_digest_and_rejects_semantic_kind_or_scope_changes():
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    other_scope = CacheAuthorizationPolicy(PROJECT, (CacheScope(CacheScopeKind.LOCAL_USER, "u2"),)).resolve(
        CacheScope(CacheScopeKind.LOCAL_USER, "u2"))
    fingerprint = operation().fingerprint
    one = CompatibleMemoryQuery(scope, VALUE_KIND, fingerprint, "one source", 10)
    two = CompatibleMemoryQuery(scope, VALUE_KIND, fingerprint, "a different source", 10)
    assert not hasattr(one, "source_content_digest") and one.semantic_fingerprint == two.semantic_fingerprint
    compatible = CompatibleMemoryCandidate("source A", "translation A", 0.9, True,
                                           CacheCompatibilityIdentity(scope, VALUE_KIND, fingerprint))
    assert compatible.identity.scope == two.scope and compatible.identity.value_kind == two.value_kind
    assert compatible.identity.semantic_fingerprint == two.semantic_fingerprint
    assert not candidate_matches_query(
        replace(compatible, identity=CacheCompatibilityIdentity(scope, CacheValueKind("other_value", 1), fingerprint)), one)
    assert not candidate_matches_query(
        replace(compatible, identity=CacheCompatibilityIdentity(other_scope, VALUE_KIND, fingerprint)), one)
    assert not candidate_matches_query(
        replace(compatible, identity=CacheCompatibilityIdentity(scope, VALUE_KIND, "sha256:" + "f" * 64)), one)
    at_limit = CompatibleMemoryQuery(scope, VALUE_KIND, fingerprint, "source", MAX_COLLECTION_ENTRIES)
    assert at_limit.max_candidates == MAX_COLLECTION_ENTRIES
    with pytest.raises(CacheIdentityError, match="^invalid_memory_query$"):
        CompatibleMemoryQuery(scope, VALUE_KIND, fingerprint, "source", MAX_COLLECTION_ENTRIES + 1)
