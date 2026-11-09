from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ayvu.artifact import new_artifact
from ayvu.artifact_service import ReuseResult
from ayvu.cache_identity import (
    CacheAuthorizationPolicy,
    CacheCompatibilityIdentity,
    CacheLookupState,
    CacheMissReason,
    CacheOnlyRequest,
    CacheProvenanceMetadata,
    CacheScope,
    CacheScopeKind,
    CompatibleMemoryCandidate,
    ExactCacheEntry,
    ExactCacheLookup,
    ExactEntryKey,
    OpaqueSemanticRevision,
    PinnedExecutionIdentityReference,
    PutIfAbsentResult,
    PutIfAbsentState,
    ResolvedCacheExecutionIdentity,
    TRANSLATION_STAGE_AGGREGATE,
    exact_entry_key,
)
from ayvu.content import (
    AdapterIdentity,
    LocaleState,
    Locator,
    ProtectedPart,
    SemanticKind,
    SourceLocale,
    UnitContent,
    build_document,
)
from ayvu.job import JobLease, JobRequest, JobState, TranslationJob
from ayvu.job_events import JobFailureFamily
from ayvu.provenance import ProducerIdentity
from ayvu.provider import DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderResult
from ayvu.translation_document import (
    DocumentTranslator,
    ExecutionContext,
    TranslationExecutionError,
    UnitSource,
)
from ayvu.translation_plan import ResolvedTranslationPlan

PROJECT = str(UUID(int=0x4321))
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
OWNER = "a" * 64
PRODUCER = ProducerIdentity("provider@1", "model@1", "adapter@1", "pipeline@1")
ROUTE = ProviderRoute("en", "pt")
CAPABILITIES = "capfp:v1:" + "b" * 64
KIND = TRANSLATION_STAGE_AGGREGATE


def make_plan(**changes):
    values = {"producer": PRODUCER, "route": ROUTE, "capabilities": CAPABILITIES, "chunk_limit": 3000}
    values.update(changes)
    return ResolvedTranslationPlan(**values)


def document(*texts, key="chapter-a.txt", protected=False):
    units = []
    for index, text in enumerate(texts or (" Hello ", "Good night"), 1):
        if protected and index == 1:
            text = " Hello <b>"
            parts = (ProtectedPart(7, 10, "markup"),)
        else:
            parts = ()
        units.append(UnitContent(Locator("paragraph", 1, f"p-{index}"), text, SemanticKind("paragraph"), (index,),
                                 protected_parts=parts))
    return build_document(
        PROJECT, key, AdapterIdentity("plain", 1, 1), SourceLocale(LocaleState.EXPLICIT, "en"), tuple(units),
    )


class Provider:
    def __init__(self, plan, respond=None):
        self.plan = plan
        self.respond = respond or (lambda text: f"[{text}]")
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return TranslationProviderResult(
            request.request_id, ProviderStatus.SUCCEEDED, self.plan.producer, DeliveryState.COMPLETED,
            text=self.respond(request.text), route=self.plan.route, usage=ProviderUsage(),
        )


class MemoryCache:
    """In-memory v2 port fake. It models exact keys and never promotes fuzzy results."""

    def __init__(self):
        self.entries = {}
        self.calls = []
        self.duplicate_keys = set()
        self.pinned = {}

    def resolve_pinned(self, reference, scope):
        self.calls.append(("resolve_pinned", reference, scope))
        return self.pinned.get(reference.boundary_reference)

    def lookup(self, key):
        self.calls.append(("lookup", key))
        if key in self.duplicate_keys:
            return ExactCacheLookup(CacheLookupState.CORRUPT, reason=CacheMissReason.DUPLICATE_EXACT_KEY)
        entry = self.entries.get(key)
        if entry is None:
            return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)
        return ExactCacheLookup(CacheLookupState.HIT, value=entry.value)

    def resolve_compatible(self, scope, value_kind, source_digest, *, limit):
        self.calls.append(("resolve", scope, value_kind, source_digest, limit))
        return tuple(key for key in self.entries if key.scope == scope and key.value_kind == value_kind
                     and key.source_content_digest == source_digest)

    def put_if_absent(self, entry):
        self.calls.append(("put_if_absent", entry.key))
        winner = self.entries.get(entry.key)
        if winner is None:
            self.entries[entry.key] = entry
            return PutIfAbsentResult(PutIfAbsentState.INSERTED)
        if winner.value == entry.value and winner.provenance == entry.provenance:
            return PutIfAbsentResult(PutIfAbsentState.IDEMPOTENT)
        return PutIfAbsentResult(PutIfAbsentState.CONFLICT)


class Memory:
    def __init__(self, candidate=None):
        self.candidate = candidate
        self.queries = []

    def lookup(self, query):
        self.queries.append(query)
        return self.candidate(query) if callable(self.candidate) else self.candidate


class FakeArtifacts:
    def __init__(self):
        self.calls = []
        self.reuse_calls = []

    def find_reusable(self, request):
        self.reuse_calls.append(request)
        return ReuseResult(None, "artifact_not_found")

    def publish(self, request, chunks, *, owner_token, cancellation=None):
        payload = b"".join(chunks)
        self.calls.append((request, payload))
        return new_artifact(request, str(uuid4()), NOW)


class FakeProvenance:
    def __init__(self):
        self.calls = []

    def append(self, request):
        self.calls.append(request)
        return SimpleNamespace(record_id=str(uuid4()))


class FakeEvents:
    def __init__(self):
        self.calls = []

    def emit(self, event):
        self.calls.append(event)


@dataclass
class Glossed:
    text: str
    usage: object = "glossary-applied"


class Env:
    def __init__(self, plan=None, *, cache=None, memory=None, provider=None, glossary=None):
        self.plan = plan or make_plan()
        self.cache = cache
        self.memory = memory
        self.provider = provider or Provider(self.plan)
        self.glossary = glossary or (lambda value: Glossed(value.upper()))
        self.artifacts = FakeArtifacts()
        self.provenance = FakeProvenance()
        self.events = FakeEvents()
        self.job = TranslationJob(
            str(UUID(int=0x8765)),
            JobRequest(PROJECT, "translate", 1, "sha256:" + "c" * 64),
            JobState.RUNNING, 1, NOW, NOW,
            JobLease(OWNER, NOW, NOW + timedelta(minutes=15)),
        )

    def translate(self, subject=None, *, context=None, **kwargs):
        translator = DocumentTranslator(
            self.plan, self.provider, self.glossary, self.artifacts, self.provenance, self.events,
            exact_cache=self.cache, memory=self.memory, clock=lambda: NOW, new_id=lambda: str(uuid4()),
        )
        return translator.translate(subject or document(), context or ExecutionContext(self.job, OWNER), **kwargs)


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


def test_fuzzy_lookup_compares_two_distinct_sources_only_under_the_same_identity():
    plan = make_plan()
    seen = []
    memory = Memory(lambda query: (
        seen.append(query) or CompatibleMemoryCandidate(
            query.source_text, "compatible translation", 0.95, True,
            CacheCompatibilityIdentity(query.scope, query.value_kind, query.semantic_fingerprint),
        )
    ))
    env = Env(plan, memory=memory, provider=Provider(plan))
    first = env.translate(document("source A", key="a.txt"))
    second_env = Env(plan, memory=memory, provider=Provider(plan))
    second = second_env.translate(
        document("source B", key="b.txt"), context=ExecutionContext(second_env.job, OWNER, progress_phase="fuzzy_b"),
    )
    assert first.units[0].source is second.units[0].source is UnitSource.APPLIED_MEMORY
    assert seen[0].source_text != seen[1].source_text
    assert seen[0].semantic_fingerprint == seen[1].semantic_fingerprint == plan.semantic_fingerprint


def test_fuzzy_candidate_with_different_value_kind_is_not_applied():
    plan = make_plan()
    wrong_kind = type(KIND)("translation_stage_fragment", 1)
    memory = Memory(lambda query: CompatibleMemoryCandidate(
        "similar source", "wrong kind translation", 1.0, True,
        CacheCompatibilityIdentity(query.scope, wrong_kind, query.semantic_fingerprint),
    ))
    provider = Provider(plan)
    result = Env(plan, memory=memory, provider=provider).translate(document("similar source"))
    assert result.units[0].source is UnitSource.PROVIDER and provider.requests


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
