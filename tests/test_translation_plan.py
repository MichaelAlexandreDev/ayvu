from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from ayvu.provenance import ProducerIdentity
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
