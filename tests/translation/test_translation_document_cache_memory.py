from __future__ import annotations
import pytest
from ayvu.provenance.model import SourceKind, UNKNOWN
from ayvu.translation.document_contracts import UnitSource
from ayvu.knowledge.memory.fuzzy import TranslationMemoryMatch
from ayvu.translation.stages.memory_retrieval import RetrievalPolicy
from ayvu.knowledge.cache.cache_only import CacheOnlyRequest
from tests.support.translation_document_support import GLOSSARY_B, Glossed
from tests.support.translation_document_support import PlanCache, PlanMemory, Provider
from tests.support.translation_document_support import cue, document, env, plan
from tests.support.paths import PROJECT_ROOT


def test_exact_cache_is_keyed_by_the_plan_and_reuse_skips_the_provider(env):
    cache = PlanCache()
    env.translate(document(key='one.vtt'), translator_options={'exact_cache': cache})
    assert {key.semantic_fingerprint for key in cache.stored} == {plan().semantic_fingerprint} and len(cache.stored) == 2
    assert len({key.source_content_digest for key in cache.stored}) == 2
    provider = Provider()
    result = env.translate(document(key='two.vtt'), provider, context=env.context(progress_phase='second'), translator_options={'exact_cache': cache})
    assert provider.requests == [] and result.units[0].source is UnitSource.EXACT_CACHE and result.units[0].text == ' [HELLO THERE] '


def test_another_plan_does_not_see_the_cache_entries(env):
    cache = PlanCache()
    env.translate(document(key='one.vtt'), translator_options={'exact_cache': cache})
    provider = Provider()
    env.translate(document(key='two.vtt'), provider, subject_plan=plan(chunk_limit=999), context=env.context(progress_phase='second'),
                  translator_options={'exact_cache': cache})
    assert len(provider.requests) == 2


def test_the_generic_path_cannot_reach_the_legacy_cache():
    from pathlib import Path
    source = (PROJECT_ROOT / 'src' / 'ayvu' / 'translation/document.py').read_text()
    for name in ('from .cache import', 'TranslationCache', 'from .html_translate', '_LegacyV1Cache', 'import requests'):
        assert name not in source


def test_applied_memory_skips_the_provider_and_is_recorded_as_unknown_producer(env):
    memory = PlanMemory(TranslationMemoryMatch('Good night', 'Boa noite', 0.99, True))
    provider = Provider()
    result = env.translate(document(cue('a', 'Good night', 1)), provider, translator_options={'memory': memory, 'retrieval_policy': RetrievalPolicy(legacy_cache_enabled=True)})
    assert provider.requests == [] and result.units[0].source is UnitSource.APPLIED_MEMORY and result.units[0].text == 'BOA NOITE'
    assert [query.semantic_fingerprint for query in memory.calls] == [plan().semantic_fingerprint]
    record = next(node.record for node in env.records() if node.record.request.operation == 'reuse_legacy_cache')
    assert record.request.source_kind is SourceKind.CACHE
    assert record.request.producer.provider == UNKNOWN and not record.request.producer.fully_identified


def test_a_memory_suggestion_is_distinct_from_applied_memory(env):
    memory = PlanMemory(TranslationMemoryMatch('Good night', 'Boa noite', 0.6, False))
    result = env.translate(document(cue('a', 'Good night', 1)), translator_options={'memory': memory, 'retrieval_policy': RetrievalPolicy(legacy_cache_enabled=True)})
    assert result.units[0].source is UnitSource.PROVIDER and result.units[0].memory_suggested


@pytest.mark.parametrize('options,source', [({'cache_only': CacheOnlyRequest.resolve_compatible()}, UnitSource.CACHE_ONLY_MISSING),
                                            ({'dry_run': True}, UnitSource.DRY_RUN)], ids=['cache-only', 'dry-run'])
def test_gates_call_no_provider_and_publish_nothing(env, options, source):
    provider = Provider()
    result = env.translate(provider=provider, **options)
    assert provider.requests == [] and result.artifact_id is None and not result.complete
    assert [unit.source for unit in result.units] == [source] * 2 and result.units[0].text == ' Hello there '
    assert not (env.root / 'ready').exists() and env.records() == ()


def test_a_blank_unit_touches_nothing_and_survives_replay(env):
    subject = document(cue('a', '   ', 1), cue('b', 'Hi', 2))
    provider = Provider()
    result = env.translate(subject, provider)
    assert result.units[0].source is UnitSource.BLANK and result.units[0].text == '   ' and len(provider.requests) == 1
    replayed = env.translate(subject, subject_plan=plan(glossary_ref=GLOSSARY_B))
    assert replayed.units[0].source is UnitSource.BLANK and replayed.units[1].source is UnitSource.ARTIFACT_REPLAY


def test_chunks_are_sent_in_order_with_cancellation_points_between_them(env):
    text = 'First sentence here. Second sentence here. Third sentence here.'
    provider = Provider()
    result = env.translate(document(cue('a', text, 1)), provider, subject_plan=plan(chunk_limit=25), glossary=Glossed)
    assert ''.join(request.text for request in provider.requests) == text and len(provider.requests) > 1
    assert result.units[0].text == ''.join(f'[{request.text}]' for request in provider.requests)
