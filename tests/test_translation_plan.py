from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from ayvu.provenance import ProducerIdentity
from ayvu.cache_identity import (
    CacheEligibilityState, CacheIneligibleReason, CanonicalValue, OpaqueSemanticRevision, SemanticRouteHop,
)
from ayvu.provider import ProviderRoute
from ayvu.provider_capabilities import (
    Capability, CapabilityFact, EvidenceSource, ProviderCapabilitySnapshot, ProviderDescriptor, ProviderRequirements, SupportState,
    capability_fingerprint,
)
from ayvu.translation_plan import (
    LINGUISTIC_STAGES, ResolvedTranslationPlan, TextStage, TranslationPlanError, resolve_plan,
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)
PRODUCER = ProducerIdentity('engine@1', 'model@1', 'adapter@1', 'not_applicable')
ROUTE = ProviderRoute('en', 'pt')
GLOSSARY = 'glossary:' + 'a' * 64
OTHER = 'glossary:' + 'b' * 64
EVIDENCE = {SupportState.SUPPORTED: EvidenceSource.ADAPTER_CODE, SupportState.UNSUPPORTED: EvidenceSource.ADAPTER_CODE,
            SupportState.UNKNOWN: EvidenceSource.NONE, SupportState.CONFIGURATION_DEPENDENT: EvidenceSource.CONFIGURATION}


def snapshot(**states):
    facts = tuple(CapabilityFact(item, state, EVIDENCE[state])
                  for item in Capability for state in [states.get(item.value, SupportState.SUPPORTED)])
    return ProviderCapabilitySnapshot(ProviderDescriptor('engine', 1, 'proto', 1, (1,), (1,)), facts, (), NOW)


def plan(**changes):
    values = {'producer': PRODUCER, 'route': ROUTE, 'capabilities': capability_fingerprint(snapshot()), 'chunk_limit': 3000,
              'glossary_ref': GLOSSARY, **changes}
    return ResolvedTranslationPlan(**values)


def bump(stage_id):
    return tuple(replace(item, version=2) if item.stage_id == stage_id else item for item in LINGUISTIC_STAGES)


def test_the_plan_is_immutable_and_hides_its_references():
    subject = plan(policy_ref='policy:' + 'c' * 64, context_ref='context:' + 'd' * 64)
    with pytest.raises(FrozenInstanceError):
        subject.chunk_limit = 1
    assert GLOSSARY not in repr(subject) and 'policy:' not in repr(subject)


@pytest.mark.parametrize('changes', [
    {'producer': ProducerIdentity('other@1', 'model@1', 'adapter@1', 'not_applicable')},
    {'producer': ProducerIdentity('engine@1', 'other@1', 'adapter@1', 'not_applicable')},
    {'producer': ProducerIdentity('engine@1', 'model@1', 'other@1', 'not_applicable')},
    {'producer': ProducerIdentity('engine@1', 'model@1', 'adapter@1', 'other@1')},
    {'route': ProviderRoute('en', 'pt', 'es')}, {'route': ProviderRoute('en', 'fr')},
    {'capabilities': 'capfp:v1:' + 'e' * 64}, {'chunk_limit': 1000},
    {'policy_ref': 'policy:' + 'c' * 64}, {'context_ref': 'context:' + 'd' * 64},
    {'stages': bump('protection')}, {'stages': bump('provider_chunks')}, {'stages': bump('placeholder_restoration')},
    {'stages': bump('raw_cache_write')}, {'stages': bump('unit_whitespace')},
], ids=range(15))
def test_every_upstream_change_invalidates_raw_output(changes):
    assert plan(**changes).raw_fingerprint != plan().raw_fingerprint


@pytest.mark.parametrize('changes', [{'glossary_ref': OTHER}, {'glossary_ref': None}, {'stages': bump('post_processing')}], ids=range(3))
def test_a_glossary_or_post_processing_change_keeps_raw_output_and_changes_post_processing(changes):
    changed = plan(**changes)
    assert changed.raw_fingerprint == plan().raw_fingerprint
    assert changed.post_processing_fingerprint != plan().post_processing_fingerprint


def test_a_glossary_the_provider_received_invalidates_raw_output():
    native = plan(glossary_native=True)
    assert native.raw_fingerprint != plan().raw_fingerprint
    assert plan(glossary_ref=OTHER, glossary_native=True).raw_fingerprint != native.raw_fingerprint


def test_upstream_changes_do_not_touch_the_post_processing_identity():
    assert plan(chunk_limit=10).post_processing_fingerprint == plan().post_processing_fingerprint


def test_fingerprints_are_deterministic_and_shaped_for_artifacts():
    assert plan().raw_fingerprint == plan().raw_fingerprint and plan().raw_fingerprint.startswith('sha256:')
    assert len(plan().raw_fingerprint) == len('sha256:') + 64


@pytest.mark.parametrize('changes,code', [
    ({'schema_version': 2}, 'unsupported_plan_schema'), ({'schema_version': True}, 'unsupported_plan_schema'),
    ({'producer': 'engine'}, 'invalid_plan'), ({'route': 'en-pt'}, 'invalid_plan'), ({'capabilities': 'x'}, 'invalid_plan'),
    ({'chunk_limit': 0}, 'invalid_chunk_limit'), ({'chunk_limit': True}, 'invalid_chunk_limit'),
    ({'glossary_ref': 'plain'}, 'invalid_reference'), ({'policy_ref': 'plain'}, 'invalid_reference'), ({'context_ref': '/x'}, 'invalid_reference'),
    ({'glossary_ref': None, 'glossary_native': True}, 'invalid_plan'), ({'glossary_native': 1}, 'invalid_plan'),
    ({'stages': LINGUISTIC_STAGES[:-1]}, 'invalid_stages'), ({'stages': tuple(reversed(LINGUISTIC_STAGES))}, 'invalid_stages'),
    ({'stages': (*LINGUISTIC_STAGES[:-1], TextStage('other'))}, 'invalid_stages'), ({'stages': list(LINGUISTIC_STAGES)}, 'invalid_stages'),
], ids=lambda value: value if isinstance(value, str) and '_' in value else None)
def test_invalid_plans_are_refused(changes, code):
    with pytest.raises(TranslationPlanError, match=f'^{code}$'):
        plan(**changes)


def test_resolve_builds_a_plan_from_accepted_requirements():
    subject = resolve_plan(snapshot(), ProviderRequirements((Capability.PLAIN_TEXT_INPUT,)), PRODUCER, ROUTE, chunk_limit=500,
                           glossary_ref=GLOSSARY)
    assert subject.capabilities == capability_fingerprint(snapshot()) and subject.chunk_limit == 500


@pytest.mark.parametrize('state', [SupportState.UNSUPPORTED, SupportState.UNKNOWN, SupportState.CONFIGURATION_DEPENDENT])
def test_no_plan_comes_from_requirements_that_are_not_plainly_accepted(state):
    with pytest.raises(TranslationPlanError, match='^requirements_not_accepted$'):
        resolve_plan(snapshot(batch=state), ProviderRequirements((Capability.BATCH,)), PRODUCER, ROUTE, chunk_limit=500)


def test_a_capability_change_reaches_the_raw_fingerprint():
    changed = resolve_plan(snapshot(context=SupportState.UNSUPPORTED), ProviderRequirements(()), PRODUCER, ROUTE, chunk_limit=3000, glossary_ref=GLOSSARY)
    assert changed.raw_fingerprint != plan().raw_fingerprint


def test_v2_cache_identity_is_present_only_for_a_complete_semantic_plan():
    subject = plan()
    assert subject.cache_eligibility.state is CacheEligibilityState.ELIGIBLE
    assert subject.semantic_fingerprint and subject.semantic_fingerprint.startswith("sha256:")
    assert subject.semantic_fingerprint not in repr(subject)
    assert plan(glossary_ref=OTHER).semantic_fingerprint == subject.semantic_fingerprint
    assert plan(producer=ProducerIdentity("engine@1", "other@1", "adapter@1", "not_applicable")).semantic_fingerprint != subject.semantic_fingerprint
    assert plan(route=ProviderRoute("en", "pt", "es")).semantic_fingerprint != subject.semantic_fingerprint


def test_resolved_plan_rejects_route_hop_identity_not_resolved_by_its_provider():
    foreign = ProducerIdentity("other@1", "model@1", "adapter@1", "not_applicable")
    hop = SemanticRouteHop("en", "pt", foreign, capability_fingerprint(snapshot()))
    with pytest.raises(TranslationPlanError, match="^invalid_route_hops$"):
        plan(route_hops=(hop,))


@pytest.mark.parametrize("producer,reason", [
    (ProducerIdentity("unknown", "model@1", "adapter@1", "not_applicable"), CacheIneligibleReason.UNKNOWN_PROVIDER),
    (ProducerIdentity("engine@1", "unknown", "adapter@1", "not_applicable"), CacheIneligibleReason.UNKNOWN_MODEL),
    (ProducerIdentity("engine@1", "model@1", "unknown", "not_applicable"), CacheIneligibleReason.UNKNOWN_ADAPTER),
    (ProducerIdentity("engine@1", "model@1", "adapter@1", "unknown"), CacheIneligibleReason.UNKNOWN_PRODUCER_PIPELINE),
])
def test_unknown_critical_identity_is_cache_ineligible(producer, reason):
    subject = plan(producer=producer)
    assert subject.cache_eligibility.reason is reason and subject.semantic_fingerprint is None


def test_low_entropy_inputs_require_owner_issued_revisions_with_no_hash_fallback():
    subject = plan(policy_ref="policy:" + "c" * 64)
    assert subject.cache_eligibility.reason is CacheIneligibleReason.MISSING_CONFIGURATION_REVISION
    context = plan(context_ref="context:" + "d" * 64)
    assert context.cache_eligibility.reason is CacheIneligibleReason.MISSING_CONTEXT_REVISION
    prompt = plan(prompt_strategy="system_prompt")
    assert prompt.cache_eligibility.reason is CacheIneligibleReason.MISSING_PROMPT_REVISION
    native = plan(glossary_native=True)
    assert native.cache_eligibility.reason is CacheIneligibleReason.MISSING_TERMINOLOGY_REVISION
    unbacked = plan(glossary_native=True, terminology_revision=OpaqueSemanticRevision(
        "ayvu", "terminology", 1, "unbacked-revision"))
    assert unbacked.cache_eligibility.reason is CacheIneligibleReason.MISSING_SEMANTIC_SNAPSHOT

    revision_a = OpaqueSemanticRevision("ayvu", "terminology", 1, "native-glossary-rev-a", "glossary:" + "a" * 64)
    revision_b = OpaqueSemanticRevision("ayvu", "terminology", 1, "native-glossary-rev-b", "glossary:" + "b" * 64)
    native_a = plan(glossary_native=True, terminology_revision=revision_a)
    native_b = plan(glossary_ref=OTHER, glossary_native=True, terminology_revision=revision_b)
    assert native_a.cache_eligibility.state is CacheEligibilityState.ELIGIBLE
    assert native_a.semantic_fingerprint != native_b.semantic_fingerprint


def test_context_prompt_configuration_generation_and_stage_revisions_are_semantic():
    context_ref = "context:" + "e" * 64
    context_ref_a = "context:" + "a" * 64
    context_ref_b = "context:" + "b" * 64
    context_a = OpaqueSemanticRevision("project", "context", 1, "book-context-a", context_ref_a)
    context_b = OpaqueSemanticRevision("project", "context", 1, "book-context-b", context_ref_b)
    subject = plan(context_ref=context_ref_a, context_revision=context_a)
    assert plan(context_ref=context_ref_b, context_revision=context_b).semantic_fingerprint != subject.semantic_fingerprint

    prompt_a = OpaqueSemanticRevision("provider", "prompt", 1, "system-prompt-a", "prompt:" + "a" * 64)
    prompt_b = OpaqueSemanticRevision("provider", "prompt", 1, "system-prompt-b", "prompt:" + "b" * 64)
    assert plan(prompt_strategy="system_prompt", prompt_revision=prompt_a).semantic_fingerprint != \
        plan(prompt_strategy="system_prompt", prompt_revision=prompt_b).semantic_fingerprint

    policy_a = "policy:" + "a" * 64
    policy_b = "policy:" + "b" * 64
    config_a = OpaqueSemanticRevision("provider", "configuration", 1, "config-a", policy_a)
    config_b = OpaqueSemanticRevision("provider", "configuration", 1, "config-b", policy_b)
    assert plan(policy_ref=policy_a, configuration_revision=config_a).semantic_fingerprint != \
        plan(policy_ref=policy_b, configuration_revision=config_b).semantic_fingerprint

    generation_a = (("temperature", CanonicalValue("decimal", "0.2")),)
    generation_b = (("temperature", CanonicalValue("decimal", "0.7")),)
    assert plan(generation_parameters=generation_a).semantic_fingerprint != plan(generation_parameters=generation_b).semantic_fingerprint
    assert plan(stages=bump("provider_chunks")).semantic_fingerprint != plan().semantic_fingerprint


def test_availability_time_does_not_change_the_capability_semantics():
    before = snapshot()
    later = replace(before, observed_at=datetime(2035, 1, 1, tzinfo=UTC))
    assert capability_fingerprint(before) == capability_fingerprint(later)
