from __future__ import annotations



from ayvu.knowledge.cache.identity_values import OpaqueSemanticRevision
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from ayvu.knowledge.cache.compatible_memory import CacheCompatibilityIdentity, CompatibleMemoryCandidate, CompatibleMemoryQuery
from ayvu.knowledge.cache.entries import CacheLookupState, CacheProvenanceMetadata, ExactCacheLookup, PutIfAbsentState, exact_entry_key

from tests.knowledge.cache.cache_identity_support import PIPELINE_REVISION, PRODUCER, PROJECT, VALUE_KIND, _concurrent_put
from tests.knowledge.cache.cache_identity_support import _entry, operation


def test_concurrent_put_if_absent_is_idempotent_for_identical_value_and_metadata():
    entry = _entry("translation")
    cache, statuses = _concurrent_put((entry, entry))
    assert sorted(item.value for item in statuses) == sorted((PutIfAbsentState.INSERTED.value,
                                                              PutIfAbsentState.IDEMPOTENT.value))
    assert cache.entries[entry.key] == entry


def test_concurrent_divergent_value_or_provenance_commits_one_and_returns_typed_conflict():
    first = _entry("translation A")
    second = _entry("translation B")
    cache, statuses = _concurrent_put((first, second))
    assert sorted(item.value for item in statuses) == sorted((PutIfAbsentState.INSERTED.value,
                                                              PutIfAbsentState.CONFLICT.value))
    assert len(cache.entries) == 1 and next(iter(cache.entries.values())) in (first, second)
    first = _entry("same translation")
    second = _entry("same translation", model="model@2")
    cache, statuses = _concurrent_put((first, second))
    assert sorted(item.value for item in statuses) == sorted((PutIfAbsentState.INSERTED.value,
                                                              PutIfAbsentState.CONFLICT.value))
    assert len(cache.entries) == 1


def test_identity_provenance_cache_results_and_memory_candidates_redact_correlators():
    secret_source = "CANARY-SOURCE-CONTENT"
    secret_translation = "CANARY-TRANSLATION-CONTENT"
    secret_scope = "CANARY-SCOPE-ID"
    secret_revision = OpaqueSemanticRevision("owner", "prompt", 1, "CANARY-PROMPT-REVISION", "prompt:" + "d" * 64)
    subject = operation(prompt_strategy="system_prompt", prompt_revision=secret_revision)
    scope = CacheAuthorizationPolicy(PROJECT, (CacheScope(CacheScopeKind.LOCAL_USER, secret_scope),)).resolve(
        CacheScope(CacheScopeKind.LOCAL_USER, secret_scope))
    key = exact_entry_key(scope, VALUE_KIND, subject.fingerprint, secret_source)
    metadata = CacheProvenanceMetadata("translate", PRODUCER, PIPELINE_REVISION)
    candidate = CompatibleMemoryCandidate(secret_source, secret_translation, 0.8, False,
        CacheCompatibilityIdentity(scope, VALUE_KIND, subject.fingerprint))
    query = CompatibleMemoryQuery(scope, VALUE_KIND, subject.fingerprint, secret_source, 8)
    lookup = ExactCacheLookup(CacheLookupState.HIT, value=secret_translation)
    traces = " ".join(map(repr, (subject, subject.pipeline_revision, key, scope, metadata, candidate, query, lookup)))
    for canary in (secret_source, secret_translation, secret_scope, "CANARY-PROMPT-REVISION",
                   subject.fingerprint, key.source_content_digest, key.semantic_fingerprint):
        assert canary not in traces
    assert "https://" not in traces
