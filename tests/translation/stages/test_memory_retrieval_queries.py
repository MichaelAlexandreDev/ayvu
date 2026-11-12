"""Real governed-store queries and production segment integration; provider is external fake."""
from contextlib import closing
from dataclasses import replace
import sqlite3
from uuid import uuid4
import pytest
from ayvu.knowledge.memory.approved.model import MAX_VALUE_BYTES, MemoryScope, MemoryScopeError, MemoryState, MemoryValidationError, PipelineStage, RetrievalLimits, RetrievalMode, ScopeKind, TargetValue, ValueKind
from ayvu.translation.stages.memory_retrieval import MemoryRetrieval, RetrievalPolicy, similarity, snippet
from ayvu.translation.document_contracts import UnitSource
from tests.support.approved_memory_fixtures import NOW, PIN, PROJECT_SCOPE, WORK_SCOPE, final, source
from tests.support.content_fixtures import PROJECT
from ayvu.kernel import LanguagePair
from tests.translation.stages.memory_retrieval_support import Pipeline, approved, doc, env, query


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


def test_oversized_source_query_is_rejected_before_repository():
    class Forbidden:
        def retrieve(self, query):
            pytest.fail('invalid query reached repository')
    for size in (MAX_VALUE_BYTES + 1,):
        from ayvu.knowledge.memory.approved.model import MemoryValueError
        with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
            MemoryRetrieval(Forbidden()).query(query(text='x' * size))


def test_normalization_cannot_expand_a_query_past_the_value_limit(env, tmp_path):
    from ayvu.knowledge.memory.approved.model import MemoryValueError
    text = '\u0344' * 16385  # NFC expands this composition-excluded character from 2 to 4 UTF-8 bytes.
    assert len(text.encode('utf-8')) < 65536
    with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
        query(text=text)
    pipeline = Pipeline(env, tmp_path)
    result = pipeline.run(doc(text))
    assert result.units[0].source is UnitSource.PROVIDER and pipeline.provider.requests
