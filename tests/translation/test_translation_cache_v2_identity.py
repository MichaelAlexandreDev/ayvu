from __future__ import annotations
import pytest
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from ayvu.knowledge.cache.entries import CacheLookupState, CacheMissReason, CacheProvenanceMetadata, ExactCacheEntry, ExactCacheLookup, exact_entry_key
from ayvu.knowledge.cache.identity_values import OpaqueSemanticRevision
from ayvu.provenance.model import ProducerIdentity
from ayvu.providers.ports import ProviderRoute
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from tests.support.translation_cache_v2_support import Env, Glossed, KIND, MemoryCache
from tests.support.translation_cache_v2_support import OWNER, PROJECT, Provider, document, make_plan


def test_rich_cache_hit_reapplies_downstream_glossary_after_protected_restoration():
    cache = MemoryCache()
    glossary_a = "glossary:" + "a" * 64
    glossary_b = "glossary:" + "b" * 64
    first_plan = make_plan(glossary_ref=glossary_a)
    first_provider = Provider(first_plan)
    first = Env(first_plan, cache=cache, provider=first_provider)
    original = document(" Hello ", "Good night", key="first.txt", protected=True)
    first_result = first.translate(original)
    assert len(first_provider.requests) == 2
    assert all(unit.source is UnitSource.PROVIDER for unit in first_result.units)
    assert [entry.value for entry in cache.entries.values()] == ["[Hello __AYVU_TAG_0__]", "[Good night]"]
    assert all(entry.key.value_kind == KIND for entry in cache.entries.values())
    assert {entry.key.source_content_digest for entry in cache.entries.values()} == {
        exact_entry_key(CacheAuthorizationPolicy(PROJECT).resolve(), KIND, first.plan.semantic_fingerprint,
                        unit.source_text).source_content_digest for unit in original.units
    }

    second_plan = make_plan(glossary_ref=glossary_b)
    second_provider = Provider(second_plan)
    second = Env(second_plan, cache=cache, provider=second_provider,
                 glossary=lambda value: Glossed(value.replace("[", "<").replace("]", ">")))
    replayed = second.translate(document(" Hello ", "Good night", key="second.txt", protected=True),
                               context=ExecutionContext(second.job, OWNER, progress_phase="second"))
    assert second_provider.requests == []
    assert [unit.source for unit in replayed.units] == [UnitSource.EXACT_CACHE, UnitSource.EXACT_CACHE]
    assert [unit.text for unit in replayed.units] == [" <Hello <b>>", "<Good night>"]
    assert [call.operation for call in second.provenance.calls if hasattr(call, "operation")] == [
        "reuse_exact_cache", "store_raw_results",
    ]


def test_source_digest_is_separate_from_semantics_and_changes_exact_key():
    cache = MemoryCache()
    first = Env(cache=cache)
    first.translate(document("one source", key="one.txt"))
    before = tuple(cache.entries)
    second_provider = Provider(make_plan())
    second = Env(cache=cache, provider=second_provider)
    second.translate(document("another source", key="two.txt"), context=ExecutionContext(second.job, OWNER, progress_phase="two"))
    after = tuple(cache.entries)
    assert len(before) == 1 and len(after) == 2
    assert before[0].semantic_fingerprint == after[1].semantic_fingerprint
    assert before[0].source_content_digest != after[1].source_content_digest
    assert len(second_provider.requests) == 1


def test_semantic_route_change_misses_and_low_entropy_unbacked_plan_never_reads_or_writes():
    cache = MemoryCache()
    first = Env(cache=cache)
    first.translate(document("same source", key="one.txt"))
    pivot = make_plan(route=ProviderRoute("en", "pt", "es"))
    provider = Provider(pivot)
    second = Env(pivot, cache=cache, provider=provider)
    second.translate(document("same source", key="two.txt"), context=ExecutionContext(second.job, OWNER, progress_phase="two"))
    assert len(provider.requests) == 1

    ineligible = make_plan(producer=ProducerIdentity("provider@1", "unknown", "adapter@1", "pipeline@1"))
    untouched = MemoryCache()
    provider = Provider(ineligible)
    result = Env(ineligible, cache=untouched, provider=provider).translate(document("secret source"))
    assert result.cache_eligibility.state.value == "ineligible"
    assert provider.requests and untouched.calls == [] and untouched.entries == {}


def test_provider_native_terminology_revision_changes_identity_and_downstream_glossary_does_not():
    cache = MemoryCache()
    downstream_a = make_plan(glossary_ref="glossary:" + "a" * 64)
    downstream_b = make_plan(glossary_ref="glossary:" + "b" * 64)
    assert downstream_a.semantic_fingerprint == downstream_b.semantic_fingerprint

    glossary_a = "glossary:" + "c" * 64
    glossary_b = "glossary:" + "d" * 64
    native_a = make_plan(
        glossary_ref=glossary_a, glossary_native=True,
        terminology_revision=OpaqueSemanticRevision("provider", "terminology", 1, "terms-a", glossary_a),
    )
    native_b = make_plan(
        glossary_ref=glossary_b, glossary_native=True,
        terminology_revision=OpaqueSemanticRevision("provider", "terminology", 1, "terms-b", glossary_b),
    )
    assert native_a.semantic_fingerprint != native_b.semantic_fingerprint
    first = Env(native_a, cache=cache)
    first.translate(document("same source", key="native-a.txt"))
    provider = Provider(native_b)
    second = Env(native_b, cache=cache, provider=provider)
    second.translate(document("same source", key="native-b.txt"),
                     context=ExecutionContext(second.job, OWNER, progress_phase="native_b"))
    assert len(provider.requests) == 1


def test_concurrent_runtime_conflict_preserves_the_first_exact_value():
    plan = make_plan()
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    key = exact_entry_key(scope, KIND, plan.semantic_fingerprint, "race source")
    committed = ExactCacheEntry(key, "first committed", CacheProvenanceMetadata("translate", plan.producer,
                                                                                plan.pipeline_revision))

    class RacingCache(MemoryCache):
        def lookup(self, requested):
            self.calls.append(("lookup", requested))
            return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)

        def put_if_absent(self, entry):
            self.entries.setdefault(entry.key, committed)
            return super().put_if_absent(entry)

    cache = RacingCache()
    provider = Provider(plan, respond=lambda text: "different provider result")
    env = Env(plan, cache=cache, provider=provider)
    with pytest.raises(TranslationExecutionError) as failure:
        env.translate(document("race source"))
    assert failure.value.failure.code == "cache_write_conflict"
    assert cache.entries[key] == committed


def test_cache_identity_and_repository_errors_redact_text_secrets_urls_and_correlators(caplog):
    source = "private source sample"
    translated = "private translated sample"
    scope = CacheScope(CacheScopeKind.LOCAL_USER, "CANARY-SCOPE-ID")
    authorized = CacheAuthorizationPolicy(PROJECT, (scope,)).resolve(scope)
    plan = make_plan(prompt_strategy="system_prompt", prompt_revision=OpaqueSemanticRevision(
        "owner", "prompt", 1, "private_prompt_revision", "prompt:" + "e" * 64,
    ))
    cache = MemoryCache()
    provider = Provider(plan, respond=lambda text: f"{translated} {text}")
    env = Env(plan, cache=cache, provider=provider)
    result = env.translate(document(source), context=ExecutionContext(env.job, OWNER, cache_scope=authorized))
    fingerprint = plan.semantic_fingerprint
    exact_key = next(iter(cache.entries))
    traces = " ".join((repr(result), repr(provider.requests), repr(cache.calls), repr(env.events.calls),
                       repr(env.provenance.calls), repr(plan), repr(exact_key), caplog.text))
    for canary in (source, translated, "CANARY-SCOPE-ID", "private_prompt_revision", fingerprint,
                   exact_key.semantic_fingerprint, exact_key.source_content_digest):
        assert canary not in traces

    class ExplodingCache(MemoryCache):
        def lookup(self, key):
            raise RuntimeError("CANARY-ENDPOINT https://user:secret@example.invalid CANARY-ERROR")

    failing = Env(plan, cache=ExplodingCache(), provider=Provider(plan))
    with pytest.raises(TranslationExecutionError) as failure:
        failing.translate(document(source))
    assert failure.value.failure.code == "cache_repository_failed"
    error_text = f"{failure.value} {failure.value.failure!r} {failure.value.__repr__()}"
    assert "CANARY-ENDPOINT" not in error_text and "https://" not in error_text and "secret" not in error_text
