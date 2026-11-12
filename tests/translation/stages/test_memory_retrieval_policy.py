from dataclasses import replace
import json
import pytest
from ayvu.knowledge.memory.approved.model import MemoryState, MemoryValidationError, RetrievalMode, ValueKind
from ayvu.knowledge.cache import CacheAuthorizationPolicy, TRANSLATION_STAGE_AGGREGATE
from ayvu.translation.stages.memory_retrieval import LegacyCacheCompatibility, MemoryRetrieval, RetrievalPolicy, RetrievalSource, similarity, structural_compatibility
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import ExecutionContext, UnitSource
from tests.support.approved_memory_fixtures import NOW, PAIR, PIN, PROJECT_SCOPE
from tests.support.content_fixtures import PROJECT, epub_document, unit
from ayvu.kernel import TranslationMemoryOptions, TranslationOptions
from tests.translation.stages.memory_retrieval_support import Pipeline, approved, doc, env, query


def test_fuzzy_ranking_puts_the_higher_score_first_within_scope(env):
    approved(env, text='Café  Hello a')
    approved(env, text='Café  Hello abc')
    matches = MemoryRetrieval(env.repo).query(query(mode=RetrievalMode.FUZZY))
    scores = [match.decision.score for match in matches]
    assert len(scores) == 2 and scores[0] > scores[1]
    assert scores == sorted(scores, reverse=True)


def test_legacy_query_n_plus_one_fails_before_repository():
    from ayvu.knowledge.cache.compatible_memory import CompatibleMemoryQuery
    class Forbidden:
        def legacy_candidates(self, *args):
            pytest.fail('invalid legacy query reached repository')
    legacy = LegacyCacheCompatibility(Forbidden(), RetrievalPolicy(legacy_cache_enabled=True))
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    for query_ in (CompatibleMemoryQuery(scope, TRANSLATION_STAGE_AGGREGATE, 'sha256:' + 'a' * 64, 'Hello', 21),
                   CompatibleMemoryQuery(scope, TRANSLATION_STAGE_AGGREGATE, 'sha256:' + 'a' * 64, 'x' * 65537, 20)):
        with pytest.raises(MemoryValidationError, match='^invalid_memory_query$'):
            legacy.lookup(query_)


@pytest.mark.parametrize('cached', [False, True])
def test_raw_artifact_replay_preserves_machine_origin_and_applies_glossary_once(env, tmp_path, cached):
    pipeline = Pipeline(env, tmp_path)
    first = doc('Hello')
    pipeline.run(first, memory=False)
    document = first
    if cached:
        document = epub_document(logical_key='cache-origin.xhtml', units=[unit('body/p[0]', 'Hello')])
        assert pipeline.run(document, memory=False).units[0].source is UnitSource.EXACT_CACHE
    translator = DocumentTranslator(pipeline.plan, pipeline.provider, pipeline.glossary, pipeline.artifacts,
        env.provenance, pipeline.events, exact_cache=pipeline.cache, clock=lambda: NOW)
    result = translator.translate(document, ExecutionContext(pipeline.job, pipeline.job.lease.owner_token,
        first_sequence=pipeline.sequence, progress_phase='artifact_replay'))
    item = result.units[0]
    assert item.source is UnitSource.ARTIFACT_REPLAY and item.text == 'máquina!'
    assert item.retrieval_decision.source is (RetrievalSource.EXACT_CACHE if cached else RetrievalSource.PROVIDER)
    assert item.value_kind is ValueKind.GLOSSARY_DERIVED and not item.retrieval_decision.reviewed
    assert len(pipeline.provider.requests) == 1


def test_chunk_and_unit_order_with_approved_blank_and_provider_sources(env, tmp_path):
    pipeline = Pipeline(env, tmp_path)
    pipeline.plan = replace(pipeline.plan, chunk_limit=8)
    pipeline.provider.plan = pipeline.plan
    pipeline.provider.respond = lambda text: text
    document = doc('First', '  ', 'Alpha. Beta. Gamma.')
    pipeline.approve(document)
    result = pipeline.run(document)
    assert [item.source for item in result.units] == [UnitSource.APPROVED_MEMORY, UnitSource.BLANK, UnitSource.PROVIDER]
    assert [item.text for item in result.units] == ['revista!', '  ', 'Alpha. Beta. Gamma.!']
    assert [request.text for request in pipeline.provider.requests] == ['Alpha.', ' Beta.', ' Gamma.']
    assert [item.unit_id for item in result.units] == [item.unit_id for item in document.units]


def test_resume_serialization_preserves_only_explicit_new_policy_choices(tmp_path):
    from ayvu.v1.resume import TranslationResumeState
    for legacy, auto in ((False, False), (True, False), (False, True)):
        options = TranslationMemoryOptions(apply_threshold=0.98, conservative_auto_apply=auto,
                                          legacy_cache_compatibility=legacy)
        state = TranslationResumeState.create(tmp_path / 'in.epub', tmp_path / 'out.epub', tmp_path / 'cache.sqlite',
            'libretranslate', 'http://localhost:5000', None, TranslationOptions(language_pair=PAIR, translation_memory=options),
            False, 30, 0, execution_revision=2)
        restored = TranslationResumeState.from_dict(json.loads(json.dumps(state.to_dict()))).translation_memory_options()
        assert restored == options
        old = json.loads(json.dumps(state.to_dict()))
        assert ('translation_memory_conservative_auto_apply' in old) is auto
        assert ('translation_memory_legacy_cache_compatibility' in old) is legacy
        old.pop('translation_memory_conservative_auto_apply', None)
        old.pop('translation_memory_legacy_cache_compatibility', None)
        restored = TranslationResumeState.from_dict(old).translation_memory_options()
        assert not restored.legacy_cache_compatibility and not restored.conservative_auto_apply


def test_conservative_fuzzy_above_threshold_still_rejects_different_placeholders(env, tmp_path):
    prefix = 'Hello ' + 'a' * 200 + ' '
    document = doc(prefix + '{n0}', prefix + '{n1}')
    pipeline = Pipeline(env, tmp_path, policy=RetrievalPolicy(fuzzy_auto_apply=True))
    stored = pipeline.approve(document, 0, 'Olá {n0}')
    assert similarity(document.units[0].source_text, document.units[1].source_text) >= pipeline.policy.apply_threshold
    assert not structural_compatibility(document, document.units[1], stored)
    result = pipeline.run(document)
    assert result.units[0].source is UnitSource.APPROVED_MEMORY
    assert result.units[1].source is UnitSource.PROVIDER
    assert not result.units[1].retrieval_suggestions  # Exclusion was structural, not the suggestion threshold.
    assert '{n1}' in result.units[1].text and '{n0}' not in result.units[1].text


@pytest.mark.parametrize('dimension', ['pipeline', 'profile', 'glossary'])
def test_each_final_value_pin_must_match_exactly_before_automatic_reuse(env, dimension):
    from ayvu.provenance.model import ConfigurationRef
    stored = approved(env)
    changed = replace(PIN, **{dimension: ConfigurationRef(dimension, dimension + ':' + 'f' * 64)})
    match = MemoryRetrieval(env.repo).query(query(pin=changed), compatible=lambda _: True)[0]
    assert match.decision.reason == 'pipeline_pin_mismatch'
    assert not match.decision.auto_applied and not match.decision.reviewed
    assert match.decision.lifecycle is MemoryState.APPROVED  # Stored fact remains visible, without inherited trust.
    assert env.repo.get(stored.revision_id, scopes=(PROJECT_SCOPE,)) == stored


def test_ambiguous_stored_stage_is_excluded_before_materialization_and_provider_runs(env, tmp_path, monkeypatch):
    pipeline = Pipeline(env, tmp_path)
    document = doc('Hello')
    stored = pipeline.approve(document)
    env.raw('DROP TRIGGER tm_revisions_no_update')
    env.raw('UPDATE tm_revisions SET pipeline_stage=? WHERE revision_id=?', ('unknown', stored.revision_id))
    def forbidden(*args, **kwargs):
        pytest.fail('ambiguous stage reached materialization')
    monkeypatch.setattr(env.repo, '_load', forbidden)
    result = pipeline.run(document)
    assert result.units[0].source is UnitSource.PROVIDER and not result.units[0].retrieval_suggestions
    assert result.units[0].text == 'máquina!'


def test_real_non_idempotent_glossary_rules_over_production_stores(env, tmp_path):
    from ayvu.knowledge.glossary.rules import Glossary, apply_glossary_with_usage, effective_glossary_reference
    rules = Glossary({'máquina': 'máquina máquina', 'revista': 'revista revista'})
    pipeline = Pipeline(env, tmp_path, glossary=lambda value: apply_glossary_with_usage(value, rules))
    pipeline.plan = replace(pipeline.plan, glossary_ref=effective_glossary_reference(rules))
    pipeline.provider.plan = pipeline.plan
    document = doc('Hello', 'Good night')
    stored = pipeline.approve(document, 0, 'revista revista')
    first = pipeline.run(document)
    assert [item.text for item in first.units] == ['revista revista', 'máquina máquina']
    second = pipeline.run(document)
    assert [item.source for item in second.units] == [UnitSource.APPROVED_MEMORY, UnitSource.EXACT_CACHE]
    assert [item.text for item in second.units] == ['revista revista', 'máquina máquina']
    assert len(pipeline.provider.requests) == 1
    changed = Glossary({'máquina': 'máquina máquina máquina', 'revista': 'revista revista revista'})
    pipeline.glossary = lambda value: apply_glossary_with_usage(value, changed)
    pipeline.plan = replace(pipeline.plan, glossary_ref=effective_glossary_reference(changed))
    pipeline.provider.plan = pipeline.plan
    third = pipeline.run(document)
    assert third.units[0].source is UnitSource.PROVIDER
    assert [item.text for item in third.units] == ['máquina máquina máquina', 'máquina máquina máquina']
    assert third.units[0].retrieval_suggestions[0].decision.reason == 'pipeline_pin_mismatch'
    assert not third.units[0].retrieval_suggestions[0].decision.reviewed
    assert env.repo.get(stored.revision_id, scopes=(PROJECT_SCOPE,)) == stored
