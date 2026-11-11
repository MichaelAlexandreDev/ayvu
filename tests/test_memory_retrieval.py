"""Real governed-store queries and production segment integration; provider is external fake."""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json
import logging
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
import pytest

from ayvu.knowledge.memory.approved.model import (
    Candidate, MAX_VALUE_BYTES, MemoryQuery, MemoryScope, MemoryScopeError, MemorySource, MemoryState,
    MemoryValidationError, PipelineStage, RetrievalLimits, RetrievalMode, ScopeKind, TargetValue, ValueKind,
)
from ayvu.artifacts.service import ArtifactService
from ayvu.artifacts.store import LocalArtifactPayloads, SqliteArtifactRepository
from ayvu.knowledge.cache import (CacheAuthorizationPolicy, CacheProvenanceMetadata, ExactCacheEntry,
                                  TRANSLATION_STAGE_AGGREGATE, exact_entry_key)
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.content.model import ProtectedPart
from ayvu.jobs.model import JobState
from ayvu.jobs.events import encode_event
from ayvu.jobs.execution import InMemoryJobEventLog
from ayvu.translation.stages.memory_retrieval import (
    LegacyCacheCompatibility, MemoryRetrieval, RetrievalPolicy, RetrievalSource,
    plan_memory_pin, similarity, snippet, structural_compatibility,
)
from ayvu.projects.schema import project_migration_runner
from ayvu.providers.ports import ProviderRoute
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from approved_memory_fixtures import (
    MemoryEnvironment, NOW, PAIR, PIN, PROJECT_SCOPE, SCOPES, WORK_SCOPE, final, source,
)
from content_fixtures import PROJECT, epub_document, unit
from test_translation_cache_v2 import Provider, make_plan
from ayvu.kernel import LanguagePair, TranslationMemoryOptions, TranslationOptions


@pytest.fixture
def env(tmp_path):
    return MemoryEnvironment(tmp_path)


def query(**changes):
    return MemoryQuery(**dict(text='Café  Hello', languages=PAIR, scopes=(PROJECT_SCOPE,), pin=PIN) | changes)


def approved(env, text='Café  Hello', target='Olá', **kwargs):
    return env.approve(env.create(src=source(text), target=final(target), **kwargs))


def test_exact_uses_canonical_value_not_unit_identity_or_search_key(env):
    revision = approved(env)
    service = MemoryRetrieval(env.repo)
    assert service.query(query(text='Cafe\u0301  Hello'))[0].decision.revision_id == revision.revision_id
    assert service.query(query(text='café hello')) == ()
    assert service.query(query(text='Changed value with same unit identity')) == ()
    assert service.query(query(languages=LanguagePair('en', 'pt'))) == ()
    assert service.query(query(scopes=(WORK_SCOPE,))) == ()
    hit = service.query(query(), compatible=lambda _: True)[0]
    assert hit.decision.auto_applied and hit.decision.reviewed and not hit.decision.dataset_eligible
    assert hit.decision.scope == PROJECT_SCOPE and hit.decision.provenance_id == revision.event.provenance_id


@pytest.mark.parametrize('state', list(MemoryState))
def test_only_current_approved_lifecycle_is_retrieved(env, state):
    created = env.create()
    if state is MemoryState.HUMAN_EDITED:
        env.repo.derive(env.derivation(created, human=True))
    elif state in (MemoryState.REJECTED, MemoryState.DEPRECATED):
        env.repo.review(env.review(created, state))
    elif state in (MemoryState.APPROVED, MemoryState.SUPERSEDED):
        old = env.approve(created)
        if state is MemoryState.SUPERSEDED:
            newer = env.repo.derive(env.derivation(old))
            current = env.approve(newer, prior=old)
            assert env.repo.get(old.revision_id, scopes=(PROJECT_SCOPE,)).state is state
            assert [r.revision_id for r in env.repo.retrieve(query())] == [current.revision_id]
            return
    hits = MemoryRetrieval(env.repo).query(query(), compatible=lambda _: True)
    assert bool(hits) == (state is MemoryState.APPROVED)


@pytest.mark.parametrize('kind,stage', [(ValueKind.PROVIDER_RESPONSE, PipelineStage.PROVIDER),
    (ValueKind.RESTORED_VISIBLE, PipelineStage.PROTECTED_RESTORED),
    (ValueKind.GLOSSARY_DERIVED, PipelineStage.TERMINOLOGY_APPLIED)])
def test_even_corrupt_approval_pointer_cannot_admit_wrong_kind_or_stage(env, kind, stage):
    revision = env.create(target=TargetValue('Olá', kind, stage, PIN))
    # Simulate an invalid store: retrieval must filter before materializing it.
    env.raw('INSERT INTO tm_approvals VALUES (?, ?)', ('bad-slot', revision.revision_id))
    env.raw('INSERT INTO tm_events VALUES (?, ?, ?, ?, ?)',
        (revision.revision_id, 2, 'approved', revision.event.provenance_id, NOW.isoformat()))
    assert env.repo.retrieve(query()) == ()


def test_scope_precedence_and_revision_selection_are_stable(env):
    work = approved(env, scope=WORK_SCOPE)
    project = approved(env)
    app = MemoryRetrieval(env.repo)
    first = app.query(query(scopes=(WORK_SCOPE, PROJECT_SCOPE)), compatible=lambda _: True)
    assert [m.decision.revision_id for m in first] == [work.revision_id, project.revision_id]
    second = app.query(query(scopes=(PROJECT_SCOPE, WORK_SCOPE)), compatible=lambda _: True)
    assert [m.decision.revision_id for m in second] == [project.revision_id, work.revision_id]
    assert second == app.query(query(scopes=(PROJECT_SCOPE, WORK_SCOPE)), compatible=lambda _: True)
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        app.query(query(scopes=(MemoryScope(ScopeKind.PROJECT, str(uuid4())),)))


def test_concordance_is_scored_bounded_and_always_a_suggestion(env):
    for index in range(24):
        approved(env, text=f'Café  Hello {index:02}', target='Olá ' + str(index))
    matches = MemoryRetrieval(env.repo).query(query(mode=RetrievalMode.CONCORDANCE), compatible=lambda _: True)
    assert len(matches) == 20
    assert all(m.decision.score > 0 and not m.decision.auto_applied and m.decision.provenance_id for m in matches)
    assert matches == MemoryRetrieval(env.repo).query(query(mode=RetrievalMode.CONCORDANCE), compatible=lambda _: True)


@pytest.mark.parametrize('name,maximum', [('candidates', 64), ('aggregate_bytes', 1048576),
    ('source_bytes', 65536), ('target_bytes', 65536), ('snippet_bytes', 512)])
def test_each_query_limit_accepts_n_rejects_n_plus_one_before_repository(name, maximum):
    assert getattr(RetrievalLimits(**{name: maximum}), name) == maximum
    with pytest.raises(MemoryValidationError, match='^invalid_retrieval_limit$'):
        query(limits=RetrievalLimits(**{name: maximum + 1}))
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(MemoryValidationError, match='^invalid_retrieval_limit$'):
            RetrievalLimits(**{name: invalid})


def test_defaults_and_count_materialization_boundary(env, monkeypatch):
    assert RetrievalLimits() == RetrievalLimits(20, 262144, 65536, 65536, 512)
    for index in range(65):
        approved(env, text=f'Café  Hello {index:02}', target='Olá')
    loaded = []
    original = env.repo._load
    def load(*args, **kwargs):
        loaded.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(env.repo, '_load', load)
    assert len(env.repo.retrieve(query(mode=RetrievalMode.FUZZY))) == len(loaded) == 20
    loaded.clear()
    assert len(env.repo.retrieve(query(mode=RetrievalMode.FUZZY, limits=RetrievalLimits(candidates=64)))) == len(loaded) == 64
    with closing(sqlite3.connect(env.database)) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT revision_id FROM tm_revisions INDEXED BY tm_retrieval_search "
            "WHERE scope_kind=? AND scope_owner=? AND source_lang=? AND target_lang=? AND search_key>=? "
            "AND search_key<? ORDER BY search_key LIMIT 64", ('project', PROJECT, 'en', 'pt-br', 'c', 'd')).fetchall()
    assert any('SEARCH' in row[3] and 'tm_retrieval_search' in row[3] for row in plan)


@pytest.mark.parametrize('side', ['source', 'target'])
def test_per_value_stored_boundary_is_filtered_before_load(env, monkeypatch, side):
    revision = approved(env, text='x' * MAX_VALUE_BYTES if side == 'source' else 'Café  Hello',
                        target='x' * MAX_VALUE_BYTES if side == 'target' else 'Olá')
    q = query(text=revision.value.source.canonical_text)
    assert len(env.repo.retrieve(q)) == 1
    if side == 'target':
        assert env.repo.retrieve(replace(q, limits=RetrievalLimits(target_bytes=MAX_VALUE_BYTES - 1))) == ()
    else:
        with pytest.raises(MemoryValidationError, match='^retrieval_source_limit$'):
            replace(q, limits=RetrievalLimits(source_bytes=MAX_VALUE_BYTES - 1))
    with closing(sqlite3.connect(env.database)) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('DROP TRIGGER tm_revisions_no_update')
        column = 'canonical_source' if side == 'source' else 'target_value'
        db.execute(f'UPDATE tm_revisions SET {column}=? WHERE revision_id=?', ('x' * (MAX_VALUE_BYTES + 1), revision.revision_id))
        db.commit()
    def forbidden(*args, **kwargs):
        pytest.fail('oversized stored value was materialized')
    monkeypatch.setattr(env.repo, '_load', forbidden)
    assert env.repo.retrieve(replace(q, mode=RetrievalMode.FUZZY)) == ()


def test_aggregate_boundary_and_utf8_snippets(env):
    revision = approved(env, text='á' * 100, target='界' * 100)
    q = query(text=revision.value.source.canonical_text, limits=RetrievalLimits(aggregate_bytes=500))
    assert len(env.repo.retrieve(q)) == 1
    assert env.repo.retrieve(replace(q, limits=RetrievalLimits(aggregate_bytes=499))) == ()
    assert len(snippet('é' * 256).encode('utf-8')) == 512
    assert len(snippet('é' * 257).encode('utf-8')) == 512
    assert len(snippet('界' * 171).encode('utf-8')) == 510
    with pytest.raises(MemoryValidationError, match='^invalid_snippet_limit$'):
        snippet('x', 513)


def test_broken_port_cannot_bypass_count_or_aggregate(env):
    revision = approved(env)
    class Broken:
        def retrieve(self, query):
            for _ in range(1000):
                yield revision
    with pytest.raises(MemoryValidationError, match='^repository_candidate_limit$'):
        MemoryRetrieval(Broken()).query(query(limits=RetrievalLimits(candidates=1)))
    with pytest.raises(MemoryValidationError, match='^repository_byte_limit$'):
        MemoryRetrieval(Broken()).query(query(limits=RetrievalLimits(aggregate_bytes=1)))


def test_unicode_threshold_and_fuzzy_auto_policy(env):
    assert similarity('Cafe\u0301  HELLO', 'Café hello') == 1
    revision = approved(env, text='Café  Hello there!')
    q = query(text='Café  Hello there?', mode=RetrievalMode.FUZZY)
    score = similarity(q.canonical, revision.value.source.canonical_text)
    assert 0.8 < score < 0.98
    assert not MemoryRetrieval(env.repo).query(q, compatible=lambda _: True)[0].decision.auto_applied
    assert MemoryRetrieval(env.repo, RetrievalPolicy(suggest_threshold=score)).query(q)
    assert not MemoryRetrieval(env.repo, RetrievalPolicy(suggest_threshold=score + 0.00001)).query(q)
    with pytest.raises(MemoryValidationError, match='^invalid_retrieval_policy$'):
        RetrievalPolicy(fuzzy_auto_apply=True, apply_threshold=0.979999)


class Pipeline:
    """All production local adapters, including artifacts/provenance/jobs/events."""
    def __init__(self, env, tmp_path, *, policy=RetrievalPolicy(), glossary=None):
        self.env, self.plan = env, make_plan(route=ProviderRoute('en', 'pt-br'))
        self.cache = SQLiteExactCache(tmp_path / 'cache-v2.sqlite', clock=lambda: NOW)
        self.artifacts = ArtifactService(SqliteArtifactRepository(project_migration_runner(), env.project_database,
            PROJECT, clock=lambda: NOW), LocalArtifactPayloads(tmp_path / 'artifacts'))
        job = env.job
        for state in (JobState.PREFLIGHT, JobState.RUNNING):
            job = env.jobs.transition(job.job_id, state, expected_revision=job.revision)
        self.job = env.jobs.acquire_lease(job.job_id, timedelta(minutes=30))
        self.events = InMemoryJobEventLog(job.job_id)
        self.policy, self.glossary = policy, glossary or (lambda text: SimpleNamespace(text=text + '!', usage=None))
        self.provider = Provider(self.plan, respond=lambda text: 'máquina')
        self.sequence, self.runs = 1, 0

    def approve(self, document, index=0, target='revista!', **changes):
        value = Candidate(MemorySource.from_document(document, document.units[index].unit_id), PAIR,
            final(target, pin=plan_memory_pin(self.plan)), PROJECT_SCOPE, str(uuid4()))
        return self.env.approve(self.env.create(value=replace(value, **changes)))

    def run(self, document, *, memory=True, legacy=None):
        self.runs += 1
        translator = DocumentTranslator(self.plan, self.provider, self.glossary, self.artifacts,
            self.env.provenance, self.events, exact_cache=self.cache,
            approved_memory=MemoryRetrieval(self.env.repo, self.policy) if memory else None,
            memory=legacy, retrieval_policy=self.policy, clock=lambda: NOW)
        context = ExecutionContext(self.job, self.job.lease.owner_token, first_sequence=self.sequence,
            progress_phase=f'run_{self.runs}')
        result = translator.translate(document, context, replay_raw_results=False)
        self.sequence = result.next_sequence
        return result


def doc(*texts):
    return epub_document(units=[unit(f'body/p[{i}]', text, order=(i,)) for i, text in enumerate(texts)])


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



def test_exact_aggregate_default_and_hard_maximum_materialize_only_budget(env, monkeypatch):
    text, target = 'x' * 32768, 'z' * 32768
    for index in range(17):
        env.approve(env.create(src=source(text, logical_key=f'chapter-{index}'), target=final(target)))
    loaded = []
    real_load = env.repo._load
    def load(*args, **kwargs):
        loaded.append(args[1])
        return real_load(*args, **kwargs)
    monkeypatch.setattr(env.repo, '_load', load)
    q = query(text=text, limits=RetrievalLimits(candidates=64))
    assert len(env.repo.retrieve(q)) == len(loaded) == 4  # 256 KiB exactly
    loaded.clear()
    assert len(env.repo.retrieve(replace(q, limits=RetrievalLimits(candidates=64, aggregate_bytes=1048576)))) == len(loaded) == 16
    loaded.clear()
    assert len(env.repo.retrieve(replace(q, limits=RetrievalLimits(candidates=64, aggregate_bytes=1048575)))) == len(loaded) == 15


def test_default_fuzzy_ties_use_scope_score_revision_then_stable_ids(env):
    first = approved(env, text='Café  Hello a')
    second = approved(env, text='Café  Hello b')
    service = MemoryRetrieval(env.repo)
    q = query(mode=RetrievalMode.FUZZY)
    result = service.query(q)
    assert len(result) == 2 and result[0].decision.score == result[1].decision.score
    assert [m.decision.entry_id for m in result] == sorted([first.entry_id, second.entry_id])
    assert result == service.query(q)


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


def test_oversized_source_query_is_rejected_before_repository():
    class Forbidden:
        def retrieve(self, query):
            pytest.fail('invalid query reached repository')
    for size in (MAX_VALUE_BYTES + 1,):
        from ayvu.knowledge.memory.approved.model import MemoryValueError
        with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
            MemoryRetrieval(Forbidden()).query(query(text='x' * size))


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
    from ayvu.composition.builders.translate_project import memory_policy
    assert memory_policy(TranslationMemoryOptions()) == RetrievalPolicy()
    assert not memory_policy(TranslationMemoryOptions()).legacy_cache_enabled
    assert memory_policy(TranslationMemoryOptions(conservative_auto_apply=True, apply_threshold=0.98)).fuzzy_auto_apply
    assert memory_policy(TranslationMemoryOptions(legacy_cache_compatibility=True)).legacy_cache_enabled



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



def test_normalization_cannot_expand_a_query_past_the_value_limit(env, tmp_path):
    from ayvu.knowledge.memory.approved.model import MemoryValueError
    text = '\u0344' * 16385  # NFC expands this composition-excluded character from 2 to 4 UTF-8 bytes.
    assert len(text.encode('utf-8')) < 65536
    with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
        query(text=text)
    pipeline = Pipeline(env, tmp_path)
    result = pipeline.run(doc(text))
    assert result.units[0].source is UnitSource.PROVIDER and pipeline.provider.requests



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
