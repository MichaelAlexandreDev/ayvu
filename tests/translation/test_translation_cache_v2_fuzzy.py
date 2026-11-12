from __future__ import annotations
from ayvu.knowledge.cache.compatible_memory import CacheCompatibilityIdentity, CompatibleMemoryCandidate
from ayvu.translation.document_contracts import ExecutionContext, UnitSource
from tests.support.translation_cache_v2_support import Env, KIND, Memory
from tests.support.translation_cache_v2_support import OWNER, Provider, document, make_plan


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
