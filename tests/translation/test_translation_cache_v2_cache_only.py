from __future__ import annotations
import socket
import pytest
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from ayvu.knowledge.cache.compatible_memory import CacheCompatibilityIdentity, CompatibleMemoryCandidate
from ayvu.knowledge.cache.entries import CacheLookupState, CacheMissReason, CacheProvenanceMetadata, ExactCacheEntry, ExactEntryKey, exact_entry_key
from ayvu.knowledge.cache.cache_only import CacheOnlyRequest, PinnedExecutionIdentityReference, ResolvedCacheExecutionIdentity
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from tests.support.translation_cache_v2_support import Env, KIND, Memory, MemoryCache
from tests.support.translation_cache_v2_support import OWNER, PROJECT, Provider, document, make_plan


def test_cache_only_pinned_and_compatible_modes_are_local_and_never_call_provider():
    cache = MemoryCache()
    fill = Env(cache=cache)
    subject = document("one", "two", key="fill.txt")
    fill.translate(subject)
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    pin = PinnedExecutionIdentityReference("job:execution-1")
    cache.pinned[pin.boundary_reference] = ResolvedCacheExecutionIdentity(scope, KIND, fill.plan.semantic_fingerprint)

    provider = Provider(fill.plan)
    pinned_env = Env(cache=cache, provider=provider)
    pinned = pinned_env.translate(
        document("one", "two", key="pinned.txt"), cache_only=CacheOnlyRequest.pinned(pin),
    )
    assert provider.requests == [] and all(unit.source is UnitSource.EXACT_CACHE for unit in pinned.units)
    assert pinned_env.artifacts.reuse_calls == []
    assert len([call for call in cache.calls if call[0] == "lookup"]) == 4

    provider = Provider(fill.plan)
    unpinned = Env(cache=cache, provider=provider).translate(
        document("one", "two", key="unpinned.txt"),
        context=ExecutionContext(fill.job, OWNER, progress_phase="unpinned"),
        cache_only=CacheOnlyRequest.resolve_compatible(),
    )
    assert provider.requests == [] and all(unit.source is UnitSource.EXACT_CACHE for unit in unpinned.units)


def test_cache_only_opens_no_socket_and_resolves_no_name_in_either_mode(monkeypatch):
    cache = MemoryCache()
    fill = Env(cache=cache)
    fill.translate(document("one", key="fill.txt"))
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    pin = PinnedExecutionIdentityReference("job:execution-1")
    cache.pinned[pin.boundary_reference] = ResolvedCacheExecutionIdentity(scope, KIND, fill.plan.semantic_fingerprint)
    opened = []

    def refuse(name):
        def call(*args, **kwargs):
            opened.append(name)
            raise AssertionError(f"transport_used:{name}")
        return call

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse(name))
    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, refuse(name))

    for key, request in (("hit-pinned.txt", CacheOnlyRequest.pinned(pin)),
                         ("hit-compatible.txt", CacheOnlyRequest.resolve_compatible())):
        provider = Provider(fill.plan)
        result = Env(cache=cache, provider=provider).translate(document("one", key=key), cache_only=request)
        assert provider.requests == [] and result.units[0].source is UnitSource.EXACT_CACHE
    for key, request in (("miss-pinned.txt", CacheOnlyRequest.pinned(pin)),
                         ("miss-compatible.txt", CacheOnlyRequest.resolve_compatible())):
        provider = Provider(fill.plan)
        result = Env(cache=cache, provider=provider).translate(document("never stored", key=key), cache_only=request)
        assert provider.requests == [] and result.units[0].source is UnitSource.CACHE_ONLY_MISSING
    assert opened == []


def test_cache_only_zero_or_many_candidates_are_typed_partial_outcomes_and_pins_are_exact():
    plan = make_plan()
    provider = Provider(plan)
    empty = MemoryCache()
    missing = Env(plan, cache=empty, provider=provider).translate(
        document("missing"), cache_only=CacheOnlyRequest.resolve_compatible(),
    )
    assert provider.requests == [] and not missing.complete and missing.artifact_id is None
    assert missing.units[0].source is UnitSource.CACHE_ONLY_MISSING
    assert missing.units[0].cache_miss_reason is CacheMissReason.ENTRY_NOT_FOUND

    unresolved_ref = PinnedExecutionIdentityReference("job:unresolved")
    pin_cache = MemoryCache()
    unresolved_env = Env(plan, cache=pin_cache, provider=Provider(plan))
    unresolved = unresolved_env.translate(
        document("unresolved"), cache_only=CacheOnlyRequest.pinned(unresolved_ref),
    )
    assert unresolved.units[0].source is UnitSource.CACHE_ONLY_MISSING
    assert [call[0] for call in pin_cache.calls] == ["resolve_pinned"]

    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    digest = exact_entry_key(scope, KIND, plan.semantic_fingerprint, "ambiguous").source_content_digest
    empty.entries[ExactEntryKey(scope, KIND, plan.semantic_fingerprint, digest)] = ExactCacheEntry(
        ExactEntryKey(scope, KIND, plan.semantic_fingerprint, digest), "first", CacheProvenanceMetadata("translate", plan.producer, plan.pipeline_revision))
    second_key = ExactEntryKey(scope, KIND, "sha256:" + "f" * 64, digest)
    empty.entries[second_key] = ExactCacheEntry(second_key, "second", CacheProvenanceMetadata("translate", plan.producer, plan.pipeline_revision))
    ambiguous = Env(plan, cache=empty, provider=provider).translate(
        document("ambiguous", key="ambiguous.txt"), cache_only=CacheOnlyRequest.resolve_compatible(),
        context=ExecutionContext(Env(plan).job, OWNER, progress_phase="ambiguous"),
    )
    assert provider.requests == [] and ambiguous.units[0].cache_lookup_state is CacheLookupState.AMBIGUOUS
    assert ambiguous.units[0].cache_miss_reason is CacheMissReason.AMBIGUOUS_CANDIDATES

    duplicate = MemoryCache()
    pin = PinnedExecutionIdentityReference("job:execution-2")
    duplicate.pinned[pin.boundary_reference] = ResolvedCacheExecutionIdentity(scope, KIND, plan.semantic_fingerprint)
    key = exact_entry_key(scope, KIND, plan.semantic_fingerprint, "duplicate")
    duplicate.duplicate_keys.add(key)
    with pytest.raises(TranslationExecutionError) as failure:
        Env(plan, cache=duplicate, provider=provider).translate(
            document("duplicate"), cache_only=CacheOnlyRequest.pinned(pin),
        )
    assert failure.value.failure.code == "duplicate_exact_key" and provider.requests == []


def test_cache_only_and_fuzzy_memory_are_isolated_and_scope_mismatches_fail_closed():
    plan = make_plan()
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    other_scope = CacheAuthorizationPolicy(PROJECT, (CacheScope(CacheScopeKind.LOCAL_USER, "user-x"),)).resolve(
        CacheScope(CacheScopeKind.LOCAL_USER, "user-x"))
    memory = Memory(lambda query: CompatibleMemoryCandidate(
        "near source", "near translation", 0.99, True,
        CacheCompatibilityIdentity(query.scope, query.value_kind, query.semantic_fingerprint),
    ))
    cache = MemoryCache()
    provider = Provider(plan)
    applied = Env(plan, cache=cache, memory=memory, provider=provider).translate(document("similar source"))
    assert provider.requests == [] and applied.units[0].source is UnitSource.APPLIED_MEMORY
    assert memory.queries and not hasattr(memory.queries[0], "source_content_digest")
    assert cache.entries == {}  # A fuzzy application is never promoted to the exact cache.

    provider = Provider(plan)
    cache_only = Env(plan, cache=cache, memory=memory, provider=provider).translate(
        document("similar source", key="cache-only.txt"), cache_only=CacheOnlyRequest.resolve_compatible(),
    )
    assert provider.requests == [] and cache_only.units[0].source is UnitSource.CACHE_ONLY_MISSING
    assert len(memory.queries) == 1

    pin = PinnedExecutionIdentityReference("job:foreign-scope")
    cache.pinned[pin.boundary_reference] = ResolvedCacheExecutionIdentity(other_scope, KIND, plan.semantic_fingerprint)
    with pytest.raises(TranslationExecutionError) as failure:
        Env(plan, cache=cache, provider=Provider(plan)).translate(
            document("similar source"), context=ExecutionContext(Env(plan).job, OWNER),
            cache_only=CacheOnlyRequest.pinned(pin),
        )
    assert failure.value.failure.code == "cache_scope_denied"
    assert scope.scope.kind.value == "project"


def test_incompatible_fuzzy_candidates_are_rejected_and_provider_result_is_stored_separately():
    plan = make_plan()
    cache = MemoryCache()
    wrong_scope = CacheAuthorizationPolicy(PROJECT, (CacheScope(CacheScopeKind.LOCAL_USER, "other-user"),)).resolve(
        CacheScope(CacheScopeKind.LOCAL_USER, "other-user"))
    memory = Memory(lambda query: CompatibleMemoryCandidate(
        "similar source", "incompatible translation", 1.0, True,
        CacheCompatibilityIdentity(wrong_scope, query.value_kind, query.semantic_fingerprint),
    ))
    provider = Provider(plan)
    result = Env(plan, cache=cache, memory=memory, provider=provider).translate(document("similar source"))
    assert result.units[0].source is UnitSource.PROVIDER and provider.requests
    assert len(cache.entries) == 1 and next(iter(cache.entries.values())).value == "[similar source]"
