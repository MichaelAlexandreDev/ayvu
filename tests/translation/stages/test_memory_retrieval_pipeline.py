from contextlib import closing
from dataclasses import replace
import json
import logging
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
import pytest
from ayvu.knowledge.memory.approved.model import Candidate, MemorySource, MemoryState, MemoryValidationError, PipelineStage, RetrievalMode, ValueKind
from ayvu.content.model import ProtectedPart
from ayvu.jobs.events import encode_event
from ayvu.translation.stages.memory_retrieval import LegacyCacheCompatibility, MemoryRetrieval, RetrievalPolicy, RetrievalSource, plan_memory_pin, similarity, structural_compatibility
from ayvu.providers.ports import ProviderRoute
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import ExecutionContext, UnitSource
from tests.support.approved_memory_fixtures import NOW, PAIR, PIN, PROJECT_SCOPE, SCOPES, final, source
from tests.support.content_fixtures import epub_document, unit
from ayvu.kernel import TranslationMemoryOptions
from tests.translation.stages.memory_retrieval_support import Pipeline, approved, doc, env, query


def test_production_exact_cache_then_approved_then_provider_and_glossary_once(env, tmp_path):
    pipeline = Pipeline(env, tmp_path)
    document = doc('  Hello  ', 'Goodnight')
    approved_revision = pipeline.approve(document)
    first = pipeline.run(document)
    assert [u.source for u in first.units] == [UnitSource.APPROVED_MEMORY, UnitSource.PROVIDER]
    assert [u.text for u in first.units] == ['  revista!  ', 'máquina!']
    assert len(pipeline.provider.requests) == 1
    assert first.units[0].retrieval_decision.revision_id == approved_revision.revision_id
    assert first.units[0].value_kind is ValueKind.LINGUISTIC_FINAL
    assert first.units[1].value_kind is ValueKind.GLOSSARY_DERIVED
    assert first.units[1].value_stage is PipelineStage.TERMINOLOGY_APPLIED
    assert not first.units[1].retrieval_decision.reviewed
    second = pipeline.run(document)
    assert [u.source for u in second.units] == [UnitSource.APPROVED_MEMORY, UnitSource.EXACT_CACHE]
    assert [u.text for u in second.units] == ['  revista!  ', 'máquina!']
    # Exact v2 cache is stronger even when the memory has a matching approved value.
    pipeline.approve(document, 1, 'reviewed night!')
    third = pipeline.run(document)
    assert third.units[1].source is UnitSource.EXACT_CACHE
    assert third.units[1].retrieval_decision.source is RetrievalSource.EXACT_CACHE
    assert not third.units[1].retrieval_decision.reviewed
    path = tmp_path / 'artifacts' / 'ready' / (third.artifact_id + '.bin')
    payload = json.loads(path.read_bytes())
    assert payload['units'][0]['decision']['value_stage'] == 'linguistic_final'
    assert payload['units'][1]['decision']['dataset_eligible'] is False
    assert payload['units'][1]['decision']['output_kind'] == 'glossary_derived'
    assert payload['units'][1]['decision']['approval_inherited_after_transformation'] is False


def test_changed_glossary_pin_only_suggests_and_never_changes_stored_approval(env, tmp_path):
    pipeline = Pipeline(env, tmp_path)
    document = doc('Hello')
    stored = pipeline.approve(document)
    pipeline.plan = replace(pipeline.plan, glossary_ref='glossary:' + 'e' * 64)
    pipeline.provider.plan = pipeline.plan
    result = pipeline.run(document)
    assert result.units[0].source is UnitSource.PROVIDER and result.units[0].text == 'máquina!'
    suggestion = result.units[0].retrieval_suggestions[0]
    assert suggestion.decision.reason == 'pipeline_pin_mismatch' and not suggestion.decision.reviewed
    assert not suggestion.decision.auto_applied and not suggestion.decision.dataset_eligible
    assert env.repo.get(stored.revision_id, scopes=(PROJECT_SCOPE,)) == stored
    assert any(event.payload.code == 'memory_review_suggestions' for event in pipeline.events.replay() if hasattr(event.payload, 'code'))


def test_approved_fuzzy_default_suggestion_explicit_conservative_auto_at_exact_boundary(env, tmp_path):
    document = doc('Hello ' + 'a' * 95 + 'x', 'Hello ' + 'a' * 95 + 'y')
    score = similarity(document.units[0].source_text, document.units[1].source_text)
    pipeline = Pipeline(env, tmp_path)
    pipeline.approve(document)
    result = pipeline.run(document)
    assert result.units[1].source is UnitSource.PROVIDER and result.units[1].memory_suggested
    # Clear only cache rows; approved memory stays separate.
    with closing(sqlite3.connect(tmp_path / 'cache-v2.sqlite')) as db:
        db.execute('DELETE FROM cache_entries_v2')
        db.commit()
    pipeline.policy = RetrievalPolicy(fuzzy_auto_apply=True, apply_threshold=score)
    result = pipeline.run(document)
    assert result.units[1].source is UnitSource.APPROVED_MEMORY
    assert result.units[1].retrieval_decision.score == score and result.units[1].retrieval_decision.auto_applied
    assert result.units[1].text == 'revista!'


def test_inline_and_placeholder_incompatibility_never_auto_apply(env, tmp_path):
    pipeline = Pipeline(env, tmp_path, glossary=lambda text: SimpleNamespace(text=text, usage=None))
    document = doc('Hi {name}', 'Hi {other}')
    pipeline.approve(document)
    result = pipeline.run(document)
    assert result.units[1].source is UnitSource.PROVIDER
    marked = epub_document(units=[unit('body/p[0]', 'Hi <b>there</b>', protected_parts=(
        ProtectedPart(3, 6, 'markup'), ProtectedPart(11, 15, 'markup')))])
    # Provider echoes tokens, proving markup is reconstructed in order.
    pipeline.provider.respond = lambda text: text
    output = pipeline.run(marked)
    assert output.units[0].source is UnitSource.PROVIDER and output.units[0].text == marked.units[0].source_text


def test_legacy_requires_explicit_opt_in_and_remains_machine_without_any_repository_write(env, tmp_path):
    pipeline = Pipeline(env, tmp_path)
    old, new = doc('Hello neighbour!'), doc('Hello neighbours!')
    pipeline.run(old, memory=False)
    disabled = LegacyCacheCompatibility.__new__(LegacyCacheCompatibility)
    def forbidden(*args):
        pytest.fail('legacy port was called without opt-in')
    disabled.lookup = forbidden
    result = pipeline.run(new, legacy=disabled)
    assert result.units[0].source is UnitSource.PROVIDER
    with closing(sqlite3.connect(tmp_path / 'cache-v2.sqlite')) as db:
        db.execute('DELETE FROM cache_entries_v2 WHERE source_text=?', (new.units[0].source_text,))
        db.commit()
    before = env.repo.list_entries(scopes=SCOPES)
    pipeline.policy = RetrievalPolicy(legacy_cache_enabled=True)
    legacy = LegacyCacheCompatibility(pipeline.cache, pipeline.policy)
    result = pipeline.run(new, legacy=legacy)
    decision = result.units[0].retrieval_decision
    assert result.units[0].source is UnitSource.APPLIED_MEMORY
    assert decision.source is RetrievalSource.LEGACY_CACHE and decision.lifecycle is MemoryState.MACHINE_CANDIDATE
    assert decision.score >= 0.95 and not decision.reviewed and not decision.dataset_eligible
    assert env.repo.list_entries(scopes=SCOPES) == before
    assert result.units[0].text == 'máquina!'


def test_routine_diagnostics_are_content_free_but_decisions_are_available(env, tmp_path, caplog):
    pipeline = Pipeline(env, tmp_path)
    document = doc('PRIVATE_BOOK_CANARY https://private.example/secret')
    pipeline.provider.respond = lambda text: text
    with caplog.at_level(logging.DEBUG):
        result = pipeline.run(document)
    encoded = ''.join(json.dumps(encode_event(event)) for event in pipeline.events.replay())
    diagnostics = caplog.text + encoded + repr(result.units[0]) + repr(result.units[0].retrieval_decision)
    assert 'PRIVATE_BOOK_CANARY' not in diagnostics and 'private.example' not in diagnostics
    assert result.units[0].retrieval_decision.source is RetrievalSource.PROVIDER


def test_compatible_fuzzy_is_stronger_than_pin_mismatch_exact_and_legacy(env, tmp_path):
    document = doc('Hello ' + 'a' * 95 + 'x', 'Hello ' + 'a' * 95 + 'y')
    pipeline = Pipeline(env, tmp_path, policy=RetrievalPolicy(fuzzy_auto_apply=True, legacy_cache_enabled=True))
    pipeline.approve(document, 1)
    pin = replace(plan_memory_pin(pipeline.plan), glossary=PIN.glossary)
    value = Candidate(MemorySource.from_document(document, document.units[0].unit_id), PAIR,
        final('obsolete!', pin=pin), PROJECT_SCOPE, str(uuid4()))
    env.approve(env.create(value=value))
    class Weak:
        def lookup(self, query):
            pytest.fail('legacy shadowed a compatible approved fuzzy match')
    result = pipeline.run(document, legacy=Weak())
    assert result.units[0].source is UnitSource.APPROVED_MEMORY
    assert result.units[0].text == 'revista!'
    assert result.units[0].retrieval_decision.reason == 'approved_fuzzy'


def test_cross_document_and_changed_semantic_dimensions_are_review_only(env, tmp_path):
    pipeline = Pipeline(env, tmp_path)
    document = doc('Hello')
    stored = pipeline.approve(document)
    other = epub_document(logical_key='another.xhtml', units=[unit('body/p[0]', 'Hello')])
    assert not structural_compatibility(other, other.units[0], stored)
    result = pipeline.run(other)
    assert result.units[0].source is UnitSource.PROVIDER
    assert result.units[0].retrieval_suggestions[0].decision.reason == 'structure_unproven'


def test_legacy_bad_scope_kind_fingerprint_and_stage_are_filtered(env, tmp_path):
    pipeline = Pipeline(env, tmp_path, policy=RetrievalPolicy(legacy_cache_enabled=True))
    pipeline.run(doc('Hello neighbour!'), memory=False)
    pipeline.plan = replace(pipeline.plan, route=ProviderRoute('en', 'pt'), route_hops=None)
    pipeline.provider.plan = pipeline.plan
    result = pipeline.run(doc('Hello neighbours!'), legacy=LegacyCacheCompatibility(pipeline.cache, pipeline.policy))
    assert result.units[0].source is UnitSource.PROVIDER
    assert not result.units[0].retrieval_suggestions


@pytest.mark.parametrize('state', [MemoryState.MACHINE_CANDIDATE, MemoryState.REJECTED, MemoryState.DEPRECATED])
def test_repository_lifecycle_filter_acts_even_with_invalid_current_index(env, state):
    revision = env.create()
    if state is not MemoryState.MACHINE_CANDIDATE:
        revision = env.repo.review(env.review(revision, state))
    env.raw('INSERT INTO tm_approvals VALUES (?, ?)', ('corrupt-slot', revision.revision_id))
    assert env.repo.retrieve(query()) == ()


def test_application_rechecks_unreviewed_port_results(env):
    revision = env.create()
    class Broken:
        def retrieve(self, query):
            return (revision,)
    assert MemoryRetrieval(Broken()).query(query(), compatible=lambda _: True) == ()


def test_each_retrieval_source_enforces_unreviewed_trust():
    from ayvu.translation.stages.memory_retrieval import RetrievalDecision
    for source in (RetrievalSource.LEGACY_CACHE, RetrievalSource.EXACT_CACHE, RetrievalSource.PROVIDER):
        with pytest.raises(MemoryValidationError, match='^unreviewed_source_required$'):
            RetrievalDecision(source, True, 'no_eligible_reuse', reviewed=True)
        with pytest.raises(MemoryValidationError, match='^unreviewed_source_required$'):
            RetrievalDecision(source, True, 'no_eligible_reuse', lifecycle=MemoryState.APPROVED)


def test_production_exact_stops_before_weaker_queries(env, tmp_path, monkeypatch):
    pipeline = Pipeline(env, tmp_path, policy=RetrievalPolicy(legacy_cache_enabled=True))
    document = doc('Hello')
    pipeline.approve(document)
    modes = []
    real = env.repo.retrieve
    def retrieve(q):
        modes.append(q.mode)
        return real(q)
    monkeypatch.setattr(env.repo, 'retrieve', retrieve)
    class Weak:
        def lookup(self, query):
            pytest.fail('weaker legacy source shadowed approved exact')
    pipeline.run(document, legacy=Weak())
    assert modes == [RetrievalMode.EXACT] and pipeline.provider.requests == []


def test_approved_reuse_is_revalidated_after_deprecation_even_with_raw_replay_requested(env, tmp_path):
    pipeline = Pipeline(env, tmp_path)
    document = doc('Hello')
    stored = pipeline.approve(document)
    assert pipeline.run(document).units[0].source is UnitSource.APPROVED_MEMORY
    env.repo.review(env.review(stored, MemoryState.DEPRECATED))
    translator = DocumentTranslator(pipeline.plan, pipeline.provider, pipeline.glossary, pipeline.artifacts,
        env.provenance, pipeline.events, exact_cache=pipeline.cache, approved_memory=MemoryRetrieval(env.repo), clock=lambda: NOW)
    result = translator.translate(document, ExecutionContext(pipeline.job, pipeline.job.lease.owner_token,
        first_sequence=pipeline.sequence, progress_phase='after_deprecation'), replay_raw_results=True)
    assert result.units[0].source is UnitSource.PROVIDER


def test_known_structural_incompatibility_is_excluded_unknown_is_review_only(env):
    approved(env)
    app = MemoryRetrieval(env.repo)
    assert app.query(query(), compatible=lambda _: False) == ()
    assert not app.query(query(), compatible=lambda _: None)[0].decision.auto_applied


def test_production_and_cli_policy_choose_the_same_conservative_defaults():
    from ayvu.composition.builders.rich_options import memory_policy
    assert memory_policy(TranslationMemoryOptions()) == RetrievalPolicy()
    assert not memory_policy(TranslationMemoryOptions()).legacy_cache_enabled
    assert memory_policy(TranslationMemoryOptions(conservative_auto_apply=True, apply_threshold=0.98)).fuzzy_auto_apply
    assert memory_policy(TranslationMemoryOptions(legacy_cache_compatibility=True)).legacy_cache_enabled
