from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import subprocess
import sys

import pytest

from ayvu.knowledge.cache.identity_values import MAX_COLLECTION_ENTRIES, MAX_OPAQUE_SCALAR_BYTES, MAX_ROUTE_HOPS, CacheIdentityError, CanonicalValue, OpaqueSemanticRevision
from ayvu.knowledge.cache.identity import MAX_CANONICAL_PAYLOAD_BYTES, SemanticOperationV2
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy
from ayvu.knowledge.cache.entries import exact_entry_key, source_content_digest
from ayvu.provenance.model import ProducerIdentity

from tests.knowledge.cache.cache_identity_support import PROJECT, VALUE_KIND
from tests.knowledge.cache.cache_identity_support import _payload_at_limit, canonical_pairs, hop, operation
from tests.support.paths import TESTS_ROOT


def test_canonical_serialization_has_a_stable_golden_fixture():
    payload = operation().canonical_payload()
    fixture = TESTS_ROOT / "fixtures" / "cache_identity_v2.golden"
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
        "from ayvu.knowledge.cache.identity import SemanticOperationV2; "
        "from ayvu.knowledge.cache.identity_values import CanonicalValue, SemanticRouteHop, OpaqueSemanticRevision; "
        "from ayvu.provenance import ProducerIdentity; "
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
