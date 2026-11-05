from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
from uuid import UUID

import pytest

from ayvu.cache_identity import (
    CACHE_IDENTITY_VERSION,
    MAX_CACHE_ONLY_CANDIDATES,
    MAX_CANONICAL_PAYLOAD_BYTES,
    MAX_COLLECTION_ENTRIES,
    MAX_OPAQUE_SCALAR_BYTES,
    MAX_ROUTE_HOPS,
    AuthorizedCacheScope,
    CacheAuthorizationPolicy,
    CacheCompatibilityIdentity,
    CacheIdentityError,
    CacheIdentityResolutionState,
    CacheLookupState,
    CacheMissReason,
    CacheOnlyMode,
    CacheOnlyRequest,
    CacheProvenanceMetadata,
    CacheScope,
    CacheScopeKind,
    CacheValueKind,
    CanonicalValue,
    CompatibleMemoryCandidate,
    CompatibleMemoryQuery,
    ExactCacheEntry,
    ExactCacheLookup,
    ExactEntryKey,
    OpaqueSemanticRevision,
    PinnedExecutionIdentityReference,
    PutIfAbsentResult,
    PutIfAbsentState,
    SemanticOperationV2,
    SemanticRouteHop,
    TRANSLATION_STAGE_AGGREGATE,
    candidate_matches_query,
    exact_entry_key,
    resolve_compatible_identities,
    source_content_digest,
)
from ayvu.provenance import ProducerIdentity

PROJECT = str(UUID(int=0x1234))
PRODUCER = ProducerIdentity("provider@1", "model@1", "adapter@1", "pipeline@1")
CAPABILITIES = "capfp:v1:" + "a" * 64
PIPELINE_REVISION = OpaqueSemanticRevision("ayvu", "pipeline", 2, "translation-pipeline-v2")
SEGMENTATION_REVISION = OpaqueSemanticRevision("ayvu", "segmentation", 1, "unit-v1")
CHUNKING_REVISION = OpaqueSemanticRevision("ayvu", "chunking", 1, "chunks-v1")
PROTECTION_REVISION = OpaqueSemanticRevision("ayvu", "protection", 1, "protected-v1")
NORMALIZATION_REVISION = OpaqueSemanticRevision("ayvu", "normalization", 1, "whitespace-v1")
VALUE_KIND = TRANSLATION_STAGE_AGGREGATE


def hop(source="en", target="pt", producer=PRODUCER, capability=CAPABILITIES, config=None):
    return SemanticRouteHop(source, target, producer, capability, config)


def operation(**changes):
    values = {
        "operation": "translate",
        "source_locale": "en",
        "target_locale": "pt",
        "route_hops": (hop(),),
        "pipeline_revision": PIPELINE_REVISION,
        "stage_versions": (("unit_whitespace", 1), ("protection", 1), ("provider_chunks", 1),
                           ("placeholder_restoration", 1), ("raw_cache_write", 1)),
        "prompt_strategy": "none",
        "prompt_revision": None,
        "context_strategy": "none",
        "context_revision": None,
        "terminology_revision": None,
        "generation_parameters": (("temperature", CanonicalValue("decimal", Decimal("0.25"))),
                                   ("top_p", CanonicalValue("decimal", Decimal("1")))),
        "segmentation_revision": SEGMENTATION_REVISION,
        "chunk_limit": 3000,
        "chunking_revision": CHUNKING_REVISION,
        "protection_revision": PROTECTION_REVISION,
        "normalization_revision": NORMALIZATION_REVISION,
        "format_constraints": (("markup", CanonicalValue("boolean", True)),),
        "version": CACHE_IDENTITY_VERSION,
    }
    values.update(changes)
    return SemanticOperationV2(**values)


def canonical_pairs(subject=None):
    subject = subject or operation()
    return (
        ("operation", subject.operation),
        ("source_locale", subject.source_locale),
        ("target_locale", subject.target_locale),
        ("route_hops", subject.route_hops),
        ("pipeline_revision", subject.pipeline_revision),
        ("stage_versions", subject.stage_versions),
        ("prompt_strategy", subject.prompt_strategy),
        ("prompt_revision", subject.prompt_revision),
        ("context_strategy", subject.context_strategy),
        ("context_revision", subject.context_revision),
        ("terminology_revision", subject.terminology_revision),
        ("generation_parameters", subject.generation_parameters),
        ("segmentation_revision", subject.segmentation_revision),
        ("chunk_limit", subject.chunk_limit),
        ("chunking_revision", subject.chunking_revision),
        ("protection_revision", subject.protection_revision),
        ("normalization_revision", subject.normalization_revision),
        ("format_constraints", subject.format_constraints),
        ("version", subject.version),
    )


def test_canonical_serialization_has_a_stable_golden_fixture():
    payload = operation().canonical_payload()
    fixture = Path(__file__).parent / "fixtures" / "cache_identity_v2.golden"
    assert payload == fixture.read_bytes()
    assert b'"type":"integer"' in payload and b'"type":"boolean"' in payload


def test_mapping_insertion_order_and_independent_processes_are_deterministic():
    forward = operation(generation_parameters=(("alpha", CanonicalValue("integer", 3)),
                                               ("beta", CanonicalValue("boolean", False))))
    reverse = operation(generation_parameters=(("beta", CanonicalValue("boolean", False)),
                                               ("alpha", CanonicalValue("integer", 3))))
    assert forward.canonical_payload() == reverse.canonical_payload()
    expected = forward.fingerprint
    code = (
        "from ayvu.cache_identity import CanonicalValue, SemanticOperationV2, SemanticRouteHop, "
        "OpaqueSemanticRevision; from ayvu.provenance import ProducerIdentity; "
        "p=ProducerIdentity('provider@1','model@1','adapter@1','pipeline@1'); "
        "r=lambda n,v: OpaqueSemanticRevision('ayvu',n,1,v); "
        "h=SemanticRouteHop('en','pt',p,'capfp:v1:'+'a'*64); "
        "o=SemanticOperationV2('translate','en','pt',(h,),OpaqueSemanticRevision('ayvu','pipeline',2,'translation-pipeline-v2'),"
        "(('unit_whitespace',1),('protection',1),('provider_chunks',1),('placeholder_restoration',1),('raw_cache_write',1)), "
        "'none',None,'none',None,None,"
        "(('alpha',CanonicalValue('integer',3)),('beta',CanonicalValue('boolean',False))),"
        "r('segmentation','unit-v1'),3000,r('chunking','chunks-v1'),r('protection','protected-v1'),"
        "r('normalization','whitespace-v1'),(('markup',CanonicalValue('boolean',True)),),2); print(o.fingerprint)"
    )
    child = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    assert child.stdout.strip() == expected


def test_projection_rejects_unknown_duplicate_and_missing_fields():
    pairs = canonical_pairs()
    with pytest.raises(CacheIdentityError, match="^invalid_projection_fields$"):
        SemanticOperationV2.from_pairs((*pairs, ("display_name", "visible")))
    with pytest.raises(CacheIdentityError, match="^invalid_projection_fields$"):
        SemanticOperationV2.from_pairs((*pairs, ("operation", "translate")))
    with pytest.raises(CacheIdentityError, match="^invalid_projection_fields$"):
        SemanticOperationV2.from_pairs(pairs[:-1])
    with pytest.raises(CacheIdentityError, match="^duplicate_canonical_key$"):
        operation(generation_parameters=(("é", CanonicalValue("integer", 1)),
                                         ("e\u0301", CanonicalValue("integer", 2))))
    for key in ("system_prompt", "endpoint-url", "context-payload", "glossary_text"):
        with pytest.raises(CacheIdentityError, match="^invalid_semantic_mapping_key$"):
            operation(generation_parameters=((key, CanonicalValue("string", "CANARY-PROMPT")),))
    with pytest.raises(CacheIdentityError, match="^invalid_typed_value$"):
        CanonicalValue("string", "https://user:secret@example.invalid/path")


@pytest.mark.parametrize("field,value", [
    ("prompt_revision", OpaqueSemanticRevision("ayvu", "prompt", 1, "prompt-v1")),
    ("context_revision", OpaqueSemanticRevision("ayvu", "context", 1, "context-v1")),
])
def test_projection_rejects_invalid_optional_presence_pairs(field, value):
    values = dict(canonical_pairs())
    values[field] = value
    with pytest.raises(CacheIdentityError):
        SemanticOperationV2.from_pairs(tuple(values.items()))


def test_unsupported_versions_and_untyped_numbers_fail_closed():
    with pytest.raises(CacheIdentityError, match="^unsupported_identity_version$"):
        operation(version=3)
    with pytest.raises(CacheIdentityError, match="^invalid_chunk_limit$"):
        operation(chunk_limit=2**63)
    with pytest.raises(CacheIdentityError, match="^invalid_typed_value$"):
        CanonicalValue("integer", True)
    with pytest.raises(CacheIdentityError, match="^invalid_typed_value$"):
        CanonicalValue("integer", 2**63)
    with pytest.raises(CacheIdentityError, match="^invalid_typed_value$"):
        CanonicalValue("decimal", Decimal("NaN"))
    with pytest.raises(CacheIdentityError, match="^invalid_typed_value$"):
        CanonicalValue("decimal", Decimal("1.0000000000000000001"))
    assert CanonicalValue("integer", 2**63 - 1).value == 2**63 - 1
    assert CanonicalValue("decimal", Decimal("1000000000")).value == Decimal("1000000000")
    with pytest.raises(CacheIdentityError, match="^invalid_typed_value$"):
        CanonicalValue("decimal", Decimal("1000000000.000000000000000001"))
    assert CanonicalValue("decimal", Decimal("1.000000000000000001")).value == Decimal("1.000000000000000001")


def test_route_hop_ceiling_accepts_eight_and_rejects_nine():
    locales = tuple("xx" if index == 0 else f"xx-p{index}" for index in range(MAX_ROUTE_HOPS + 1))
    eight = tuple(hop(a, b) for a, b in zip(locales, locales[1:]))
    assert len(operation(route_hops=eight, source_locale=locales[0], target_locale=locales[-1]).route_hops) == 8
    nine_locales = tuple("yy" if index == 0 else f"yy-p{index}" for index in range(MAX_ROUTE_HOPS + 2))
    nine = tuple(hop(a, b) for a, b in zip(nine_locales, nine_locales[1:]))
    with pytest.raises(CacheIdentityError, match="^route_hop_limit_exceeded$"):
        operation(route_hops=nine, source_locale=nine_locales[0], target_locale=nine_locales[-1])


@pytest.mark.parametrize("field", ["stage_versions", "generation_parameters", "format_constraints"])
def test_named_collection_ceiling_accepts_256_and_rejects_257(field):
    if field == "stage_versions":
        at_limit = tuple((f"stage_{index}", 1) for index in range(MAX_COLLECTION_ENTRIES))
        one_more = (*at_limit, ("stage_over", 1))
    else:
        at_limit = tuple((f"entry_{index}", CanonicalValue("integer", index))
                         for index in range(MAX_COLLECTION_ENTRIES))
        one_more = (*at_limit, ("entry_over", CanonicalValue("integer", 0)))
    assert len(getattr(operation(**{field: at_limit}), field)) == MAX_COLLECTION_ENTRIES
    with pytest.raises(CacheIdentityError, match="^collection_limit_exceeded$"):
        operation(**{field: one_more})


def _payload_at_limit():
    def size(candidate):
        try:
            return len(candidate.canonical_payload())
        except CacheIdentityError as error:
            if error.code == "canonical_payload_limit_exceeded":
                return MAX_CANONICAL_PAYLOAD_BYTES + 1
            raise

    for count in range(1, MAX_COLLECTION_ENTRIES + 1):
        prefix = tuple((f"pad_{index:03d}", CanonicalValue("string", "x" * MAX_OPAQUE_SCALAR_BYTES))
                       for index in range(count - 1))
        key = f"pad_{count - 1:03d}"

        def with_length(length):
            return operation(format_constraints=(*prefix, (key, CanonicalValue("string", "x" * length))))

        smallest, largest = with_length(1), with_length(MAX_OPAQUE_SCALAR_BYTES)
        minimum_size, maximum_size = size(smallest), size(largest)
        if minimum_size <= MAX_CANONICAL_PAYLOAD_BYTES <= maximum_size:
            low, high = 1, MAX_OPAQUE_SCALAR_BYTES
            while low <= high:
                middle = (low + high) // 2
                candidate = with_length(middle)
                candidate_size = size(candidate)
                if candidate_size == MAX_CANONICAL_PAYLOAD_BYTES:
                    return candidate, middle
                if candidate_size < MAX_CANONICAL_PAYLOAD_BYTES:
                    low = middle + 1
                else:
                    high = middle - 1
    raise AssertionError("could not construct the named canonical payload boundary")


def test_canonical_payload_accepts_64_kib_and_rejects_one_byte_more():
    candidate, last_scalar_length = _payload_at_limit()
    assert len(candidate.canonical_payload()) == MAX_CANONICAL_PAYLOAD_BYTES
    assert last_scalar_length < MAX_OPAQUE_SCALAR_BYTES
    entries = (*candidate.format_constraints[:-1],
               (candidate.format_constraints[-1][0], CanonicalValue("string", "x" * (last_scalar_length + 1))))
    with pytest.raises(CacheIdentityError, match="^canonical_payload_limit_exceeded$"):
        operation(format_constraints=entries).canonical_payload()


def test_opaque_scalar_is_bounded_by_utf8_bytes_and_rejects_bare_hash_fallback():
    at_limit = OpaqueSemanticRevision("ayvu", "prompt", 1, "😀" * (MAX_OPAQUE_SCALAR_BYTES // 4))
    assert len(at_limit.value.encode("utf-8")) == MAX_OPAQUE_SCALAR_BYTES
    with pytest.raises(CacheIdentityError, match="^invalid_semantic_revision$"):
        OpaqueSemanticRevision("ayvu", "prompt", 1, "😀" * (MAX_OPAQUE_SCALAR_BYTES // 4 + 1))
    with pytest.raises(CacheIdentityError, match="^invalid_semantic_revision$"):
        OpaqueSemanticRevision("ayvu", "prompt", 1, "a" * 64)


def test_changed_effective_dimensions_change_semantics_but_source_changes_only_the_exact_key():
    base = operation()
    changes = (
        replace(base, route_hops=(hop("en", "es"), hop("es", "pt"))),
        replace(base, route_hops=(hop(producer=ProducerIdentity("provider@2", "model@1", "adapter@1", "pipeline@1")),)),
        replace(base, route_hops=(hop(producer=ProducerIdentity("provider@1", "model@2", "adapter@1", "pipeline@1")),)),
        replace(base, terminology_revision=OpaqueSemanticRevision("provider", "terminology", 1, "native-terms-v2",
                                                                  "terminology:" + "a" * 64)),
        replace(base, prompt_strategy="system_prompt", prompt_revision=OpaqueSemanticRevision(
            "provider", "prompt", 1, "prompt-v2", "prompt:" + "b" * 64)),
        replace(base, context_strategy="book_context", context_revision=OpaqueSemanticRevision(
            "project", "context", 1, "context-v2", "context:" + "c" * 64)),
        replace(base, generation_parameters=(("temperature", CanonicalValue("decimal", Decimal("0.5"))),)),
        replace(base, stage_versions=((*base.stage_versions[:-1], (base.stage_versions[-1][0], 2)))),
        replace(base, format_constraints=(("xml", CanonicalValue("boolean", True)),)),
        replace(base, chunk_limit=1000),
    )
    assert all(item.fingerprint != base.fingerprint for item in changes)
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    first = exact_entry_key(scope, VALUE_KIND, base.fingerprint, "different source one")
    second = exact_entry_key(scope, VALUE_KIND, base.fingerprint, "different source two")
    assert first.semantic_fingerprint == second.semantic_fingerprint == base.fingerprint
    assert first.source_content_digest != second.source_content_digest
    assert source_content_digest("same") == source_content_digest("same")


def test_direct_and_pivot_routes_and_per_hop_provider_models_are_distinct():
    direct = operation()
    pivot = operation(source_locale="fr", target_locale="pt", route_hops=(hop("fr", "en"), hop("en", "pt")))
    other_hop = operation(source_locale="fr", target_locale="pt", route_hops=(
        hop("fr", "en"), hop("en", "pt", producer=ProducerIdentity("other@1", "model@1", "adapter@1", "pipeline@1")),
    ))
    assert len({direct.fingerprint, pivot.fingerprint, other_hop.fingerprint}) == 3


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


class AtomicFake:
    def __init__(self):
        self.entries = {}
        self.lock = Lock()

    def put_if_absent(self, entry):
        with self.lock:
            current = self.entries.get(entry.key)
            if current is None:
                self.entries[entry.key] = entry
                return PutIfAbsentResult(PutIfAbsentState.INSERTED)
            if current.value == entry.value and current.provenance == entry.provenance:
                return PutIfAbsentResult(PutIfAbsentState.IDEMPOTENT)
            return PutIfAbsentResult(PutIfAbsentState.CONFLICT)


def _concurrent_put(entries):
    cache = AtomicFake()
    gate = Barrier(len(entries))

    def submit(entry):
        gate.wait()
        return cache.put_if_absent(entry).state

    with ThreadPoolExecutor(max_workers=len(entries)) as pool:
        statuses = tuple(pool.map(submit, entries))
    return cache, statuses


def _entry(value, *, model="model@1"):
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    key = exact_entry_key(scope, VALUE_KIND, operation().fingerprint, "source")
    producer = ProducerIdentity("provider@1", model, "adapter@1", "pipeline@1")
    provenance = CacheProvenanceMetadata("translate", producer, PIPELINE_REVISION)
    return ExactCacheEntry(key, value, provenance)


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
