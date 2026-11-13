"""Local intention resolves only unique, compatible, canonical v2 evidence."""
from dataclasses import replace
import os
import socket
import sqlite3
import subprocess
import sys
from uuid import UUID

import pytest
import requests

from ayvu.knowledge.cache import (
    MAX_CACHE_ONLY_CANDIDATES, CacheAuthorizationPolicy, CacheIdentityError,
    CanonicalValue, OpaqueSemanticRevision, source_content_digest,
)
from ayvu.providers import ProviderRoute
from ayvu.translation import LocalCachePlanIntent, LocalCachePlanState
from tests.support.local_cache_plan_support import PRODUCER, SCOPE, SOURCE, evidence, intent_for, plan
from tests.support.translation_plan_support import bump


def revision(namespace, label="first"):
    return OpaqueSemanticRevision("test", namespace, 1, label, namespace + ":" + "a" * 64)


def test_unique_evidence_reconstructs_the_original_plan_without_network(evidence, monkeypatch):
    original = plan()
    key = evidence.store(original)
    def forbidden(*args, **kwargs):
        pytest.fail("network or provider session used by local resolution")
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(requests, "Session", forbidden)
    result = evidence.resolver().resolve(SOURCE)
    assert result.state is LocalCachePlanState.RESOLVED
    assert result.key == key
    assert result.plan == original
    assert result.plan.semantic_fingerprint == original.semantic_fingerprint == key.semantic_fingerprint
    assert "local-model" not in repr(result) + repr(intent_for(original))


def test_importing_the_resolver_loads_no_network_or_storage_adapter():
    code = """
import importlib, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('network called while importing local resolution')
socket.getaddrinfo = socket.create_connection = forbidden
module = importlib.import_module('ayvu.translation.local_cache_plan')
assert module.LocalCachePlanResolver
assert not {'requests', 'sqlite3', 'ayvu.network.http', 'ayvu.providers.session',
            'ayvu.knowledge.cache.read_only', 'ayvu.knowledge.cache.repository'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=30,
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def test_no_candidate_is_a_miss(evidence):
    result = evidence.resolver().resolve(SOURCE)
    assert result.state is LocalCachePlanState.MISS
    assert result.key is result.plan is result.reason is None


@pytest.mark.parametrize("changes", [
    {"producer": replace(PRODUCER, model="declared:other-model")},
    {"producer": replace(PRODUCER, provider="other@1")},
    {"producer": replace(PRODUCER, adapter="other@1")},
    {"producer": replace(PRODUCER, pipeline="other@1")},
    {"chunk_limit": 2999},
    {"pipeline_revision": revision("pipeline")},
    {"segmentation_revision": revision("segmentation")},
    {"chunking_revision": revision("chunking")},
    {"protection_revision": revision("protection")},
    {"normalization_revision": revision("normalization")},
    {"generation_parameters": (("temperature", CanonicalValue("decimal", "0.5")),)},
    {"format_constraints": (("markup", CanonicalValue("boolean", True)),)},
    {"prompt_strategy": "template", "prompt_revision": revision("prompt")},
    {"policy_ref": "configuration:" + "a" * 64, "configuration_revision": revision("configuration")},
    {"glossary_native": True, "glossary_ref": "terminology:" + "a" * 64,
     "terminology_revision": revision("terminology")},
    {"route": ProviderRoute("fr", "pt")},
    {"route": ProviderRoute("en", "es")},
])
def test_incompatible_dimensions_are_misses(evidence, changes):
    evidence.store(plan(**changes))
    assert evidence.resolver().resolve(SOURCE).state is LocalCachePlanState.MISS


def test_raw_stage_revision_is_compared(evidence):
    evidence.store(replace(plan(), stages=bump("provider_chunks")))
    assert evidence.resolver().resolve(SOURCE).state is LocalCachePlanState.MISS


def test_all_local_pins_and_semantic_inputs_round_trip(evidence):
    original = plan(
        prompt_strategy="template", prompt_revision=revision("prompt"),
        policy_ref="configuration:" + "a" * 64, configuration_revision=revision("configuration"),
        glossary_native=True, glossary_ref="terminology:" + "a" * 64,
        terminology_revision=revision("terminology"),
        generation_parameters=(("alpha", CanonicalValue("decimal", "0.50")),
                               ("zeta", CanonicalValue("boolean", True))),
        format_constraints=(("markup", CanonicalValue("string", "html")),),
    )
    key = evidence.store(original)
    local = replace(intent_for(original), generation_parameters=tuple(reversed(original.generation_parameters)))
    result = evidence.resolver(local).resolve(SOURCE)
    assert result.plan == original and result.key == key
    assert result.plan.semantic_fingerprint == original.semantic_fingerprint
    # A different low-entropy pin cannot be smuggled in through a reference.
    for field in ("policy_ref", "glossary_ref"):
        intent = replace(intent_for(original), **{field: "other:" + "b" * 64})
        assert evidence.resolver(intent).resolve(SOURCE).state is LocalCachePlanState.MISS


def test_current_post_processing_is_not_inferred_from_raw_cache(evidence):
    evidence.store(plan(glossary_ref="glossary:" + "a" * 64))
    intent = replace(intent_for(plan()), glossary_ref="glossary:" + "b" * 64, stages=bump("post_processing"))
    result = evidence.resolver(intent).resolve(SOURCE)
    assert result.state is LocalCachePlanState.RESOLVED
    assert result.plan.glossary_ref == intent.glossary_ref and result.plan.stages == intent.stages


@pytest.mark.parametrize("reverse", [False, True])
def test_two_compatible_routes_are_ambiguous_without_preference(evidence, reverse):
    originals = [plan(), plan(route=ProviderRoute("en", "pt", "es"))]
    for original in reversed(originals) if reverse else originals:
        evidence.store(original)
    result = evidence.resolver().resolve(SOURCE)
    assert result.state is LocalCachePlanState.AMBIGUOUS
    assert result.key is result.plan is None


def test_a_complete_recorded_bridge_is_accepted(evidence):
    original = plan(route=ProviderRoute("en", "pt", "es"))
    evidence.store(original)
    result = evidence.resolver().resolve(SOURCE)
    assert result.state is LocalCachePlanState.RESOLVED
    assert result.plan == original and result.plan.route_hops == original.route_hops
    assert result.plan.capabilities == original.capabilities


def test_filtering_happens_before_uniqueness_at_the_candidate_ceiling(evidence):
    expected = evidence.store(plan())
    for index in range(MAX_CACHE_ONLY_CANDIDATES - 1):
        evidence.store(plan(chunk_limit=4000 + index))
    result = evidence.resolver().resolve(SOURCE)
    assert result.state is LocalCachePlanState.RESOLVED and result.key == expected


@pytest.mark.parametrize("count", [MAX_CACHE_ONLY_CANDIDATES + 1, 2001])
def test_incompatible_overflow_is_not_filtered_into_a_miss(evidence, count):
    for index in range(count):
        evidence.store(plan(chunk_limit=4000 + index))
    with pytest.raises(CacheIdentityError, match="cache_candidate_limit_exceeded"):
        evidence.resolver().resolve(SOURCE)


def test_key_fingerprint_disagreeing_with_canonical_is_corrupt(evidence):
    evidence.store(plan())
    with sqlite3.connect(evidence.path) as connection:
        connection.execute("UPDATE cache_entries_v2 SET semantic_fingerprint=?", ("sha256:" + "0" * 64,))
    with pytest.raises(CacheIdentityError, match="cache_entry_corrupt"):
        evidence.resolver().resolve(SOURCE)


def test_resolver_itself_recomputes_the_fingerprint(evidence):
    evidence.store(plan())
    record, = evidence.records()
    class DamagedPort:
        def canonical_records(self, scope, value_kind, source_digest, *, limit):
            assert scope == SCOPE and source_digest == source_content_digest(SOURCE)
            assert limit == MAX_CACHE_ONLY_CANDIDATES
            return (replace(record, key=replace(record.key, semantic_fingerprint="sha256:" + "0" * 64)),)
    with pytest.raises(CacheIdentityError, match="cache_entry_corrupt"):
        evidence.resolver(port=DamagedPort()).resolve(SOURCE)


def test_foreign_project_evidence_is_not_accessible(evidence):
    other = CacheAuthorizationPolicy(str(UUID(int=0x9876))).resolve()
    evidence.store(plan(), scope=other)
    assert evidence.resolver().resolve(SOURCE).state is LocalCachePlanState.MISS
    record = evidence.resolver(scope=other).resolve(SOURCE)
    assert record.state is LocalCachePlanState.RESOLVED


def test_a_port_returning_foreign_scope_fails_closed(evidence):
    evidence.store(plan())
    record, = evidence.records()
    other = CacheAuthorizationPolicy(str(UUID(int=0x9876))).resolve()
    class ForeignPort:
        def canonical_records(self, *args, **kwargs):
            return (replace(record, key=replace(record.key, scope=other)),)
    with pytest.raises(CacheIdentityError, match="cache_scope_denied"):
        evidence.resolver(port=ForeignPort()).resolve(SOURCE)


def test_context_requiring_preparation_is_explicitly_unsupported(evidence):
    original = plan(context_strategy="bounded_context_v1", context_ref="context:" + "a" * 64,
                    context_revision=revision("context"))
    evidence.store(original)
    class UnavailablePort:
        def canonical_records(self, *args, **kwargs):
            pytest.fail("unsupported context must be refused before evidence I/O")
    result = evidence.resolver(intent_for(original), port=UnavailablePort()).resolve(SOURCE)
    assert result.state is LocalCachePlanState.UNSUPPORTED
    assert result.reason == "offline_context_preparation_required"
    assert result.key is result.plan is None


def test_each_source_can_resolve_a_different_recorded_route(evidence):
    direct, bridge = plan(), plan(route=ProviderRoute("en", "pt", "es"))
    evidence.store(direct)
    evidence.store(bridge, source="Another unit")
    resolver = evidence.resolver()
    assert resolver.resolve(SOURCE).plan == direct
    assert resolver.resolve("Another unit").plan == bridge
