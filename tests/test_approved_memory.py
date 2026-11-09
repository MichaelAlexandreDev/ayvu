from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
import logging
import sqlite3
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from ayvu.approved_memory import (
    MAX_VALUE_BYTES, NORMALIZATION_VERSION, SEARCH_VERSION, TRANSITIONS, Candidate, MemoryConflictError,
    MemoryNotFoundError, MemoryProvenanceError, MemoryScope, MemoryScopeError, MemorySource, MemoryState,
    MemoryStorageError, MemoryTransitionError, MemoryValidationError, MemoryValueError, PipelineStage, ScopeKind,
    TargetValue, ValueKind, require_transition,
)
from ayvu.approved_memory_service import CreateCandidate, ReviewRevision
from ayvu.approved_memory_store import MEMORY_MIGRATIONS, SqliteApprovedMemoryRepository, memory_migration_runner
from ayvu.cache import CacheKey, TranslationCache
from ayvu.domain import LanguagePair
from ayvu.provenance import ActorKind, ConfigurationRef
from approved_memory_fixtures import (
    IMPORT_SCOPE, NOW, PAIR, PIN, PROJECT, PROJECT_SCOPE, SCOPES, USER_SCOPE, WORK_SCOPE,
    MemoryEnvironment, document, final, source,
)


@pytest.fixture
def env(tmp_path):
    return MemoryEnvironment(tmp_path)


@pytest.mark.parametrize('previous', list(MemoryState))
@pytest.mark.parametrize('following', list(MemoryState))
def test_closed_transition_table_every_pair(previous, following):
    # Independent specification: do not derive the expected outcome from production's table.
    allowed = {
        'machine_candidate': {'human_edited', 'approved', 'rejected', 'deprecated'},
        'human_edited': {'approved', 'rejected', 'deprecated'},
        'approved': {'deprecated', 'superseded'},
        'rejected': set(), 'deprecated': set(), 'superseded': set(),
    }
    if following.value in allowed[previous.value]:
        require_transition(previous, following)
    else:
        with pytest.raises(MemoryTransitionError, match='^invalid_transition$'):
            require_transition(previous, following)
    assert set(TRANSITIONS) == set(MemoryState)


def test_create_reopen_query_records_distinct_fields_and_no_consent(env):
    command = env.creation()
    created = env.app.create_candidate(command)
    reopened = env.open().get(created.revision_id, scopes=(PROJECT_SCOPE,))
    assert reopened == created and created.value == command.value
    assert created.state is MemoryState.MACHINE_CANDIDATE
    assert created.event.recorded_at == NOW
    assert created.value.source.unit_id.startswith('unit:v1:')
    assert created.value.source.content_id.startswith('scd:v1:')
    assert created.value.source.canonical_text == 'Café  Hello'
    assert created.value.source.search_key == 'café hello'
    assert created.value.source.normalization_version == NORMALIZATION_VERSION
    assert created.value.source.search_version == SEARCH_VERSION
    assert env.repo.list_entries(scopes=(PROJECT_SCOPE,)).revisions == (created,)
    columns = {row[1] for row in env.raw('PRAGMA table_info(tm_revisions)')}
    assert {'unit_id', 'content_id', 'canonical_source', 'search_key', 'normalization_version', 'search_version',
            'source_lang', 'target_lang', 'target_value', 'value_kind', 'pipeline_stage', 'scope_kind', 'scope_owner',
            'document_id', 'document_revision', 'source_project', 'provenance_id', 'import_origin'} <= columns
    assert not any('consent' in name or 'dataset' in name or 'training' in name for name in columns)
    assert not any('consent' in field.name or 'dataset' in field.name for field in fields(Candidate))


def test_explicit_human_approves_exact_machine_value_and_evidence_is_bound(env):
    created = env.create()
    command = env.review(created)
    approved = env.app.approve(**{name: getattr(command, name) for name in (
        'operation_id', 'revision_id', 'expected_version', 'scope', 'provenance_id', 'reviewed_value', 'expected_approval')})
    assert approved.state is MemoryState.APPROVED and approved.value == created.value
    event = env.provenance.get(approved.event.provenance_id).record
    assert event.request.actor_kind is ActorKind.HUMAN and event.request.operation == 'tm_approve'
    assert event.request.operation_id == command.operation_id and event.recorded_at == approved.event.recorded_at
    assert event.request.subject.value == created.value.source.unit_id
    assert set(event.request.configuration_refs) == set(PIN.references)
    assert event.request.parents == (created.event.provenance_id,)
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved


@pytest.mark.parametrize('actor', [ActorKind.SYSTEM, ActorKind.AI, ActorKind.PROVIDER])
def test_approval_never_inferred_from_nonhuman_actor(env, actor):
    created = env.create()
    with pytest.raises(MemoryProvenanceError, match='^incompatible_provenance$'):
        env.repo.review(env.review(created, actor=actor))
    assert env.repo.get(created.revision_id, scopes=(PROJECT_SCOPE,)) == created
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) is None


@pytest.mark.parametrize('kind,stage', [
    (ValueKind.PROVIDER_RESPONSE, PipelineStage.PROVIDER),
    (ValueKind.RESTORED_VISIBLE, PipelineStage.PROTECTED_RESTORED),
    (ValueKind.GLOSSARY_DERIVED, PipelineStage.TERMINOLOGY_APPLIED),
])
def test_earlier_value_cannot_be_approved_even_with_equal_target_string(env, kind, stage):
    created = env.create(target=TargetValue('Olá', kind, stage, PIN))
    with pytest.raises(MemoryValueError, match='^approval_requires_linguistic_final$'):
        env.approve(created)
    assert env.repo.get(created.revision_id, scopes=(PROJECT_SCOPE,)) == created


@pytest.mark.parametrize('kind', list(ValueKind))
@pytest.mark.parametrize('stage', list(PipelineStage))
def test_value_stage_contract_is_closed(kind, stage):
    matching = {'provider_response': 'provider', 'restored_visible': 'protected_restored',
                'glossary_derived': 'terminology_applied', 'linguistic_final': 'linguistic_final'}
    if matching[kind.value] == stage.value:
        TargetValue('same', kind, stage, PIN)
    else:
        with pytest.raises(MemoryValueError, match='^incompatible_value_stage$'):
            TargetValue('same', kind, stage, PIN)


@pytest.mark.parametrize('changed', [
    final('Outro'),
    final(pin=replace(PIN, glossary=ConfigurationRef('glossary', 'glossary:' + 'f' * 64))),
    final(pin=replace(PIN, profile=ConfigurationRef('profile', 'profile:' + 'f' * 64))),
    final(pin=replace(PIN, pipeline=ConfigurationRef('pipeline', 'pipeline:' + 'f' * 64))),
])
def test_exact_review_includes_target_and_all_pinned_revisions(env, changed):
    created = env.create()
    with pytest.raises(MemoryValueError, match='^reviewed_value_mismatch$'):
        env.repo.review(env.review(created, reviewed_value=changed))
    assert env.repo.get(created.revision_id, scopes=(PROJECT_SCOPE,)).state is MemoryState.MACHINE_CANDIDATE


def test_human_edit_creates_revision_and_approval_chain_with_real_provenance(env):
    original = env.create()
    command = env.derivation(original, target=final('Olá, mundo'), human=True)
    edited = env.app.record_human_edit(operation_id=command.operation_id, parent_revision=command.parent_revision,
                                      expected_version=command.expected_version, value=command.value)
    assert edited.entry_id == original.entry_id and edited.revision_number == original.revision_number + 1
    assert edited.parent_revision == original.revision_id and edited.state is MemoryState.HUMAN_EDITED
    assert env.repo.get(original.revision_id, scopes=(PROJECT_SCOPE,)) == original
    approved = env.approve(edited)
    history = env.repo.history(edited.revision_id, scopes=(PROJECT_SCOPE,))
    assert [event.state for event in history] == [MemoryState.MACHINE_CANDIDATE, MemoryState.HUMAN_EDITED,
                                                 MemoryState.APPROVED]
    lineage = env.provenance.lineage(approved.event.provenance_id, max_depth=10, max_records=20)
    assert {node.record.record_id for node in lineage.nodes} == {
        original.event.provenance_id, edited.event.provenance_id, approved.event.provenance_id}
    assert env.repo.history(edited.revision_id, scopes=(PROJECT_SCOPE,), after=1, limit=1) == (history[1],)


@pytest.mark.parametrize('change', ['target', 'glossary', 'scope', 'stage'])
def test_approved_values_never_mutated_and_derivations_do_not_inherit_approval(env, change):
    approved = env.approve(env.create())
    target, scope = approved.value.target, PROJECT_SCOPE
    if change == 'target':
        target = final('changed')
    elif change == 'glossary':
        target = TargetValue('terminology', ValueKind.GLOSSARY_DERIVED, PipelineStage.TERMINOLOGY_APPLIED,
                             replace(PIN, glossary=ConfigurationRef('glossary', 'glossary:' + 'f' * 64)))
    elif change == 'scope':
        scope = WORK_SCOPE
    else:
        target = TargetValue('Olá', ValueKind.PROVIDER_RESPONSE, PipelineStage.PROVIDER, PIN)
    command = env.derivation(approved, target=target, scope=scope)
    derived = env.app.create_revision(command)
    assert derived.state is MemoryState.MACHINE_CANDIDATE and derived.parent_revision == approved.revision_id
    assert derived.value.provenance_id != approved.value.provenance_id
    assert env.repo.get(approved.revision_id, scopes=(PROJECT_SCOPE,)) == approved
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved
    with pytest.raises(FrozenInstanceError):
        approved.value.target.text = 'mutated'
    with pytest.raises(sqlite3.IntegrityError, match='^immutable_memory$'):
        env.raw('UPDATE tm_revisions SET target_value = ? WHERE revision_id = ?', ('changed', approved.revision_id))


def test_atomic_replacement_supersedes_previous_and_failed_write_rolls_back(env):
    prior = env.approve(env.create())
    candidate = env.repo.derive(env.derivation(prior, target=final('replacement')))
    command = env.review(candidate, prior=prior)
    # Fail after the supersession event, before the replacement approval event.
    env.raw("CREATE TRIGGER fail_review BEFORE INSERT ON tm_events WHEN NEW.state = 'approved' "
            "BEGIN SELECT RAISE(ABORT, 'test_failure'); END")
    with pytest.raises(MemoryStorageError, match='^sql_failure$'):
        env.repo.review(command)
    assert env.repo.get(prior.revision_id, scopes=(PROJECT_SCOPE,)) == prior
    assert env.repo.get(candidate.revision_id, scopes=(PROJECT_SCOPE,)) == candidate
    assert env.raw('SELECT COUNT(*) FROM tm_operations WHERE operation_id = ?', (command.operation_id,)) == [(0,)]
    env.raw('DROP TRIGGER fail_review')
    replacement = env.repo.review(command)
    superseded = env.repo.get(prior.revision_id, scopes=(PROJECT_SCOPE,))
    assert superseded.state is MemoryState.SUPERSEDED and superseded.value == prior.value
    assert superseded.event.provenance_id == replacement.event.provenance_id
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == replacement
    assert env.repo.list_entries(scopes=(PROJECT_SCOPE,), state=MemoryState.APPROVED).revisions == (replacement,)
    assert env.raw('SELECT COUNT(*) FROM tm_approvals') == [(1,)]


@pytest.mark.parametrize('different_candidates', [False, True])
def test_two_competing_approvals_have_one_winner_and_both_histories(env, different_candidates):
    prior = env.approve(env.create())
    first = env.repo.derive(env.derivation(prior, target=final('one')))
    second = env.repo.derive(env.derivation(prior, target=final('two'))) if different_candidates else first
    commands = [env.review(candidate, prior=prior) for candidate in (first, second)]
    barrier = Barrier(2)

    def approve(command):
        repository = env.open()
        barrier.wait(timeout=5)
        try:
            return repository.review(command)
        except MemoryConflictError as error:
            return str(error)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(approve, commands))
    winners = [result for result in results if not isinstance(result, str)]
    assert len(winners) == 1
    assert [result for result in results if isinstance(result, str)] == [
        'stale_approval' if different_candidates else 'stale_revision']
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == winners[0]
    assert env.repo.get(prior.revision_id, scopes=(PROJECT_SCOPE,)).state is MemoryState.SUPERSEDED
    if different_candidates:
        loser = second if winners[0].revision_id == first.revision_id else first
        assert env.repo.get(loser.revision_id, scopes=(PROJECT_SCOPE,)) == loser
    assert env.raw('SELECT COUNT(*) FROM tm_approvals') == [(1,)]
    for command in commands:
        assert env.provenance.get(command.provenance_id).record.request.operation == 'tm_approve'


def test_repeated_commands_return_original_snapshot_and_conflicting_replay_fails(env):
    create = env.creation()
    candidate = env.repo.create(create)
    assert env.open().create(create) == candidate
    edited_command = env.derivation(candidate, target=final('edited'), human=True)
    edited = env.repo.derive(edited_command)
    assert env.open().derive(edited_command) == edited
    approve = env.review(edited)
    approved = env.repo.review(approve)
    assert env.open().review(approve) == approved
    deprecate = env.review(approved, MemoryState.DEPRECATED)
    deprecated = env.repo.review(deprecate)
    assert env.open().review(deprecate) == deprecated
    assert env.repo.create(create) == candidate and env.repo.review(approve) == approved
    with pytest.raises(MemoryConflictError, match='^operation_conflict$'):
        env.repo.create(replace(create, value=replace(create.value, target=final('different'))))
    with pytest.raises(MemoryConflictError, match='^operation_conflict$'):
        env.repo.review(replace(approve, expected_version=99))


@pytest.mark.parametrize('state', [MemoryState.MACHINE_CANDIDATE, MemoryState.HUMAN_EDITED, MemoryState.APPROVED])
@pytest.mark.parametrize('following', [MemoryState.REJECTED, MemoryState.DEPRECATED])
def test_rejection_deprecation_behavior_and_terminal_history(env, state, following):
    revision = env.create()
    if state is MemoryState.HUMAN_EDITED:
        revision = env.repo.derive(env.derivation(revision, human=True))
    elif state is MemoryState.APPROVED:
        revision = env.approve(revision)
    command = env.review(revision, following)
    if state is MemoryState.APPROVED and following is MemoryState.REJECTED:
        with pytest.raises(MemoryTransitionError, match='^invalid_transition$'):
            env.repo.review(command)
        assert env.repo.get(revision.revision_id, scopes=(PROJECT_SCOPE,)) == revision
        return
    result = env.repo.review(command)
    assert result.state is following and env.repo.review(command) == result
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) is None
    for target_state in (MemoryState.APPROVED, MemoryState.REJECTED, MemoryState.DEPRECATED):
        with pytest.raises(MemoryTransitionError, match='^invalid_transition$'):
            env.repo.review(env.review(result, target_state))
    later = env.repo.derive(env.derivation(result))
    assert later.state is MemoryState.MACHINE_CANDIDATE
    assert env.repo.get(result.revision_id, scopes=(PROJECT_SCOPE,)) == result


def test_superseded_is_terminal_and_cannot_be_marked_manually(env):
    approved = env.approve(env.create())
    replacement = env.repo.derive(env.derivation(approved, target=final('next')))
    env.approve(replacement, prior=approved)
    old = env.repo.get(approved.revision_id, scopes=(PROJECT_SCOPE,))
    for state in (MemoryState.APPROVED, MemoryState.REJECTED, MemoryState.DEPRECATED):
        with pytest.raises(MemoryTransitionError, match='^invalid_transition$'):
            env.repo.review(env.review(old, state))
    with pytest.raises(MemoryTransitionError, match='^invalid_review_action$'):
        env.repo.review(replace(env.review(replacement), state=MemoryState.SUPERSEDED))


def test_stale_edit_stale_review_and_missing_entry_have_typed_outcomes(env):
    candidate = env.create()
    with pytest.raises(MemoryConflictError, match='^stale_revision$'):
        env.repo.derive(replace(env.derivation(candidate), expected_version=0))
    with pytest.raises(MemoryConflictError, match='^stale_revision$'):
        env.repo.review(replace(env.review(candidate), expected_version=True))
    with pytest.raises(MemoryNotFoundError, match='^revision_not_found$'):
        env.repo.get(str(uuid4()), scopes=(PROJECT_SCOPE,))
    with pytest.raises(MemoryConflictError, match='^source_identity_changed$'):
        command = env.derivation(candidate)
        env.repo.derive(replace(command, value=replace(command.value, source=source('changed'))))


@pytest.mark.parametrize('scope', SCOPES)
def test_project_work_user_and_import_scopes_require_explicit_allowance(env, scope):
    origin = env.evidence('tm_import', str(uuid4())).record_id if scope is IMPORT_SCOPE else None
    revision = env.approve(env.create(scope=scope, import_origin=origin))
    assert env.repo.get(revision.revision_id, scopes=(scope,)) == revision
    assert env.repo.current(env.source, PAIR, scopes=(scope,)) == revision
    other = USER_SCOPE if scope != USER_SCOPE else WORK_SCOPE
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        env.repo.get(revision.revision_id, scopes=(other,))
    assert env.repo.list_entries(scopes=(other,)).revisions == ()
    assert env.repo.current(env.source, PAIR, scopes=(other,)) is None
    restricted = env.open(allowed=frozenset({other}))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        restricted.list_entries(scopes=(scope,))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        restricted.create(env.creation(scope=scope, import_origin=origin))


def test_explicit_precedence_no_source_text_fallback_or_cross_project_leak(env):
    project = env.approve(env.create())
    work = env.approve(env.create(scope=WORK_SCOPE))
    assert env.repo.current(env.source, PAIR, scopes=(WORK_SCOPE, PROJECT_SCOPE)) == work
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE, WORK_SCOPE)) == project
    same_text_other_unit = source(logical_key='other-chapter')
    assert same_text_other_unit.canonical_text == env.source.canonical_text
    assert env.repo.current(same_text_other_unit, PAIR, scopes=SCOPES) is None
    other_project = replace(env.source, project_id=str(UUID(int=888)))
    assert env.repo.current(other_project, PAIR, scopes=SCOPES) is None
    assert env.repo.current(env.source, LanguagePair('en', 'es'), scopes=SCOPES) is None
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        replace(project.value, scope=MemoryScope(ScopeKind.PROJECT, str(UUID(int=999))))
    with pytest.raises(MemoryScopeError, match='^invalid_query_scopes$'):
        env.repo.list_entries(scopes=())


def test_listing_and_history_are_bounded_and_deterministic(env):
    revisions = [env.create(scope=PROJECT_SCOPE if index % 2 == 0 else WORK_SCOPE) for index in range(7)]
    found, cursor = [], 0
    while True:
        page = env.repo.list_entries(scopes=(PROJECT_SCOPE, WORK_SCOPE), limit=2, after=cursor)
        found.extend(page.revisions)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert found == revisions
    assert env.repo.list_entries(scopes=(PROJECT_SCOPE,), entry_id=revisions[0].entry_id).revisions == (revisions[0],)
    # Instrument the actual SQLite connection, including bounds in the SQL executed.
    statements = []
    real_connect = env.repo._runner.connect

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    env.repo._runner.connect = traced_connect
    assert len(env.repo.list_entries(scopes=SCOPES, limit=1).revisions) == 1
    assert any('ORDER BY r.sequence LIMIT 2' in sql for sql in statements)
    assert not any(sql.strip().upper() == 'SELECT * FROM TM_REVISIONS' for sql in statements)
    for limit, after in ((0, 0), (257, 0), (1, -1), (True, 0)):
        with pytest.raises(MemoryValidationError, match='^invalid_page$'):
            env.repo.list_entries(scopes=SCOPES, limit=limit, after=after)


def test_unicode_source_normalization_content_and_structural_identity_are_distinct():
    composed, decomposed = source('Café'), source('Cafe\u0301')
    assert composed.canonical_text == decomposed.canonical_text == 'Café'
    assert composed.search_key == decomposed.search_key == 'café'
    assert composed.content_id != decomposed.content_id and composed.unit_id != decomposed.unit_id
    spaced, cased = source('Straße\t\n café'), source('STRASSE CAFÉ')
    assert spaced.search_key == cased.search_key == 'strasse café'
    assert spaced.canonical_text != cased.canonical_text
    same_text_different_unit = source('Café', logical_key='different')
    assert composed.content_id == same_text_different_unit.content_id
    assert composed.unit_id != same_text_different_unit.unit_id


@pytest.mark.parametrize('field', ['source', 'target'])
@pytest.mark.parametrize('character', ['x', 'é', '🙂'])
@pytest.mark.parametrize('extra', [0, 1])
def test_utf8_64k_n_and_n_plus_one_rejected_never_truncated(env, field, character, extra):
    text = character * (MAX_VALUE_BYTES // len(character.encode())) + 'x' * extra
    assert len(text.encode()) == MAX_VALUE_BYTES + extra
    if extra:
        with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
            env.create(**({'src': source(text)} if field == 'source' else {'target': final(text)}))
        assert env.repo.list_entries(scopes=SCOPES).revisions == ()
    else:
        value = env.create(**({'src': source(text)} if field == 'source' else {'target': final(text)}))
        loaded = env.open().get(value.revision_id, scopes=SCOPES)
        assert (loaded.value.source.canonical_text if field == 'source' else loaded.value.target.text) == text


def test_oversized_source_rejected_before_normalization_and_keys_are_validated():
    # NFC would shrink this below the bound; input must still be refused.
    with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
        source('e\u0301' * 21846)
    with pytest.raises(MemoryValidationError, match='^invalid_search_policy$'):
        replace(source(), search_key='forged')
    with pytest.raises(MemoryValidationError, match='^invalid_search_policy$'):
        replace(source(), normalization_version='unversioned')
    for value in ('\ud800',):
        with pytest.raises(MemoryValueError, match='^invalid_linguistic_value$'):
            final(value)


@pytest.mark.parametrize('changes', [
    {'operation': 'tm_store'}, {'actor_kind': ActorKind.SYSTEM}, {'parents': ()},
    {'operation_id': str(UUID(int=99))}, {'configuration_refs': ()},
])
def test_provenance_must_match_action_exact_unit_pins_and_revision_chain(env, changes):
    created = env.create()
    review = env.review(created)
    evidence = env.provenance.get(review.provenance_id).record
    bad = env.provenance.append(replace(evidence.request, **changes))
    with pytest.raises(MemoryProvenanceError, match='^incompatible_provenance$'):
        env.repo.review(replace(review, provenance_id=bad.record_id))
    assert env.repo.get(created.revision_id, scopes=SCOPES) == created


def test_missing_and_wrong_unit_provenance_import_origin_and_human_edit_are_checked(env):
    create = env.creation()
    with pytest.raises(MemoryProvenanceError, match='^missing_provenance$'):
        env.repo.create(replace(create, value=replace(create.value, provenance_id=str(uuid4()))))
    created = env.repo.create(create)
    edit = env.derivation(created, human=True)
    system = env.evidence('tm_edit', edit.operation_id, edit.value, parents=(created.event.provenance_id,),
                          actor=ActorKind.SYSTEM)
    with pytest.raises(MemoryProvenanceError, match='^incompatible_provenance$'):
        env.repo.derive(replace(edit, value=replace(edit.value, provenance_id=system.record_id)))
    other = env.evidence('tm_create', str(uuid4()), replace(create.value, source=source('other')))
    with pytest.raises(MemoryProvenanceError, match='^incompatible_provenance$'):
        env.repo.create(CreateCandidate(other.request.operation_id, replace(create.value, provenance_id=other.record_id)))
    with pytest.raises(MemoryProvenanceError, match='^import_origin_required$'):
        replace(create.value, scope=IMPORT_SCOPE)
    nonimport = env.evidence('translate', str(uuid4()))
    with pytest.raises(MemoryProvenanceError, match='^incompatible_import_origin$'):
        env.create(scope=IMPORT_SCOPE, import_origin=nonimport.record_id)


def test_operational_cache_legacy_rows_and_governed_store_are_isolated(env):
    cache_path = env.root / 'cache.sqlite'
    key = CacheKey('Café  Hello', PAIR)
    with TranslationCache(cache_path) as cache:
        cache.set(key, 'legacy')
        cache.connection.execute("UPDATE translations SET created_at = '2000-01-01'")
        cache.connection.commit()
    before = cache_path.read_bytes()
    repository = env.open()
    assert repository.list_entries(scopes=SCOPES).revisions == ()
    assert cache_path.read_bytes() == before
    approved = env.approve(env.create())
    with TranslationCache(cache_path) as cache:
        cache.delete_entries(before='2001-01-01')
        assert cache.get(key) is None
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved
    cache_path.unlink()
    with TranslationCache(cache_path) as cache:
        cache.set(key, 'new operational value')
    env.repo.review(env.review(approved, MemoryState.DEPRECATED))
    with TranslationCache(cache_path) as cache:
        assert cache.get(key) == 'new operational value'
        cache_tables = {row[0] for row in cache.connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert 'translations' in cache_tables and not any(name.startswith('tm_') for name in cache_tables)
    memory_tables = {row[0] for row in env.raw("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert 'translations' not in memory_tables
    env.database.unlink()
    with TranslationCache(cache_path) as cache:
        assert cache.get(key) == 'new operational value'


def test_diagnostics_reprs_and_errors_omit_payload_paths_keys_and_credentials(env, caplog):
    canary = 'secret-token https://private.test /private/book.epub'
    src = source(canary)
    create = env.creation(src=src, target=final(canary))
    with caplog.at_level(logging.DEBUG):
        revision = env.repo.create(create)
        approved = env.approve(revision)
        representations = [repr(obj) for obj in (create, src, create.value, create.value.target, PIN, env.repo,
            revision, revision.event, env.repo.list_entries(scopes=SCOPES), env.repo.history(revision.revision_id,
            scopes=SCOPES), approved)]
        with pytest.raises(MemoryValidationError, match='^invalid_identity$') as error:
            env.repo.get(canary, scopes=SCOPES)
        representations.extend((str(error.value), repr(error.value)))
    diagnostic = ' '.join(representations) + caplog.text
    for value in (canary, src.search_key, src.content_id, str(env.root), 'secret-token', 'private.test'):
        assert value not in diagnostic


def test_storage_failures_and_database_immutability_are_typed(env):
    created = env.create()
    for table in ('tm_revisions', 'tm_events', 'tm_operations'):
        for action in ('UPDATE', 'DELETE'):
            sql = f'DELETE FROM {table}' if action == 'DELETE' else f'UPDATE {table} SET ' + {
                'tm_revisions': "target_value = 'changed'", 'tm_events': "state = 'approved'",
                'tm_operations': "command_hash = 'changed'"}[table]
            with pytest.raises(sqlite3.IntegrityError, match='^immutable_memory$'):
                env.raw(sql)
    env.raw('DROP TABLE tm_approvals')
    with pytest.raises(MemoryStorageError, match='^sql_failure$'):
        env.repo.current(env.source, PAIR, scopes=SCOPES)
    assert env.repo.get(created.revision_id, scopes=SCOPES) == created


def test_visible_source_omits_only_structural_markup_and_keeps_stable_content_reference():
    from ayvu.content import ProtectedPart
    from ayvu.html_structure import html_unit_template, review_text_from_template
    from content_fixtures import epub_document, unit
    text = 'Hello <em>world</em> __AYVU_TAG_0__'
    doc = epub_document(units=[unit('body', text, protected_parts=(
        ProtectedPart(6, 10, 'markup'), ProtectedPart(15, 20, 'markup'),
        ProtectedPart(21, len(text), 'placeholder')))])
    selected = doc.units[0]
    with pytest.raises(MemoryValidationError, match='^visible_source_required$'):
        MemorySource.from_document(doc, selected.unit_id)
    visible = review_text_from_template(html_unit_template(selected),
        [text[part.start:part.end] for part in selected.content.protected_parts])
    src = MemorySource.from_document(doc, selected.unit_id, visible_text=visible)
    assert src.canonical_text == 'Hello world __AYVU_TAG_0__'
    assert src.content_id == doc.units[0].source_digest
    assert '<em>' not in src.search_key and '__ayvu_tag_0__' in src.search_key


def test_format_projection_preserves_breaks_and_omits_hidden_inline_content():
    from ayvu.content import ProtectedPart
    from ayvu.html_structure import html_unit_template, review_text_from_template
    from content_fixtures import epub_document, unit
    text = 'Hello<br/><script>UNRELATED SECRET</script>world'
    start = text.index('<script>')
    end = text.index('world')
    doc = epub_document(units=[unit('body', text, protected_parts=(
        ProtectedPart(5, start, 'markup'), ProtectedPart(start, end, 'markup')))])
    selected = doc.units[0]
    visible = review_text_from_template(html_unit_template(selected),
        [text[part.start:part.end] for part in selected.content.protected_parts])
    assert visible == 'Hello\nworld'
    src = MemorySource.from_document(doc, selected.unit_id, visible_text=visible)
    assert src.canonical_text == 'Hello\nworld' and src.search_key == 'hello world'
    assert 'UNRELATED SECRET' not in src.canonical_text
    with pytest.raises(MemoryValidationError, match='^visible_source_mismatch$'):
        plain = document('actual')
        MemorySource.from_document(plain, plain.units[0].unit_id, visible_text='unrelated')


def test_provider_and_glossary_values_need_distinct_final_revisions_and_review(env):
    provider_value = TargetValue('Olá', ValueKind.PROVIDER_RESPONSE, PipelineStage.PROVIDER, PIN)
    provider = env.create(target=provider_value)
    with pytest.raises(MemoryValueError, match='^approval_requires_linguistic_final$'):
        env.approve(provider)
    final_revision = env.repo.derive(env.derivation(provider, target=final()))
    approved = env.approve(final_revision)
    glossary_pin = replace(PIN, glossary=ConfigurationRef('glossary', 'glossary:' + 'd' * 64))
    derived = env.repo.derive(env.derivation(approved, target=TargetValue(
        'Termo novo', ValueKind.GLOSSARY_DERIVED, PipelineStage.TERMINOLOGY_APPLIED, glossary_pin)))
    assert derived.state is MemoryState.MACHINE_CANDIDATE
    with pytest.raises(MemoryValueError, match='^approval_requires_linguistic_final$'):
        env.approve(derived, prior=approved)
    next_final = env.repo.derive(env.derivation(derived, target=final('Termo novo', glossary_pin)))
    replacement = env.approve(next_final, prior=approved)
    assert replacement.value.target.pin == glossary_pin
    assert env.repo.get(provider.revision_id, scopes=SCOPES).value.target == provider_value
    assert env.repo.get(approved.revision_id, scopes=SCOPES).state is MemoryState.SUPERSEDED


def test_concurrent_idempotent_replay_creates_one_candidate_and_approval(env):
    create = env.creation()
    barrier = Barrier(2)

    def attempt(command):
        barrier.wait(timeout=5)
        return env.open().create(command)

    with ThreadPoolExecutor(2) as pool:
        candidates = list(pool.map(attempt, (create, create)))
    assert candidates[0] == candidates[1]
    review = env.review(candidates[0])

    def approve(command):
        barrier.wait(timeout=5)
        return env.open().review(command)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(approve, (review, review)))
    assert results[0] == results[1]
    assert env.raw('SELECT COUNT(*) FROM tm_revisions') == [(1,)]
    assert env.raw('SELECT COUNT(*) FROM tm_events') == [(2,)]


def test_v2_cache_values_namespaces_clear_and_replacement_stay_independent(env):
    from ayvu.cache_repository import SQLiteExactCache
    from ayvu.cache_schema import participating_connection
    from test_cache_repository_v2 import entry
    path = env.root / 'cache-v2.sqlite'
    cache = SQLiteExactCache(path)
    item = entry(source='Café  Hello', value='Olá')
    cache.put_if_absent(item)
    assert env.open().list_entries(scopes=SCOPES).revisions == ()
    approved = env.approve(env.create())
    before = env.database.read_bytes()
    assert item.key.value_kind.value != approved.value.target.kind.value
    with participating_connection(path) as db:
        db.execute('DELETE FROM cache_entries_v2')
        db.commit()
    assert env.database.read_bytes() == before
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved
    path.unlink()
    cache = SQLiteExactCache(path)
    cache.put_if_absent(item)
    env.repo.review(env.review(approved, MemoryState.DEPRECATED))
    assert cache.lookup(item.key).value == 'Olá'


def test_snapshot_schema_history_is_frozen():
    assert [step.version for step in MEMORY_MIGRATIONS] == [1, 2]
    assert MEMORY_MIGRATIONS[1].checksum == 'aacfd30a61a1718759aa821466bec626d6bae62c17c172c4e395acc4bbfb6243'


def test_storage_size_failure_rolls_back_new_revision(env, monkeypatch):
    import ayvu.approved_memory_store as store
    command = env.creation()
    monkeypatch.setattr(store, 'MAX_DATABASE_BYTES', 1)
    with pytest.raises(MemoryStorageError, match='^resource_limit$'):
        env.repo.create(command)
    assert env.repo.list_entries(scopes=SCOPES).revisions == ()
    assert env.raw('SELECT COUNT(*) FROM tm_operations') == [(0,)]


def test_only_selected_unit_text_is_persisted_with_references_not_document_paths(env):
    from content_fixtures import epub_document, unit
    doc = epub_document(logical_key='private/source/book.xhtml', units=[
        unit('private/source/book.xhtml#p1', 'SELECTED UNIT', order=(1,)),
        unit('private/source/book.xhtml#p2', 'UNRELATED SECRET DOCUMENT PAYLOAD', order=(2,))])
    selected = MemorySource.from_document(doc, doc.units[0].unit_id)
    revision = env.create(src=selected)
    assert revision.value.source.canonical_text == 'SELECTED UNIT'
    for database in (env.database, env.project_database):
        raw = database.read_bytes()
        assert b'private/source/book.xhtml' not in raw
        assert b'UNRELATED SECRET DOCUMENT PAYLOAD' not in raw


def test_cross_project_read_and_write_require_explicit_scope_and_provenance_mapping(env):
    from ayvu.content import build_document
    from ayvu.job import JobRequest
    from ayvu.job_store import SqliteJobRepository
    from ayvu.project_schema import project_migration_runner
    from ayvu.provenance import ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef
    from ayvu.provenance_store import SqliteProvenanceRepository
    from content_fixtures import ENGLISH, EPUB, unit
    other_id = str(UUID(int=555))
    other_scope = MemoryScope(ScopeKind.PROJECT, other_id)
    doc = build_document(other_id, 'chapter', EPUB, ENGLISH, [unit('body', env.source.canonical_text)])
    src = MemorySource.from_document(doc, doc.units[0].unit_id)
    jobs = SqliteJobRepository(project_migration_runner(), env.project_database, other_id)
    job = jobs.create(JobRequest(other_id, 'translate_epub', 1, 'sha256:' + 'b' * 64))
    provenance = SqliteProvenanceRepository(project_migration_runner(), env.project_database, other_id)
    operation_id = str(uuid4())
    evidence = provenance.append(ProvenanceRequest(
        other_id, job.job_id, 'tm_create', ActorKind.SYSTEM, SourceKind.PIPELINE,
        ProducerIdentity('not_applicable', 'not_applicable', 'epub@1', 'linguistic@1'),
        subject=SubjectRef('content_unit', 1, src.unit_id), configuration_refs=PIN.references,
        operation_id=operation_id))
    command = CreateCandidate(operation_id, Candidate(src, PAIR, final(), other_scope, evidence.record_id))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        env.repo.create(command)
    broader = SqliteApprovedMemoryRepository(memory_migration_runner(), env.database,
        allowed_scopes=frozenset((*SCOPES, other_scope)), provenance={PROJECT: env.provenance, other_id: provenance})
    created = broader.create(command)
    assert broader.get(created.revision_id, scopes=(other_scope,)) == created
    assert broader.list_entries(scopes=(PROJECT_SCOPE,)).revisions == ()
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        broader.get(created.revision_id, scopes=(PROJECT_SCOPE,))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        env.repo.get(created.revision_id, scopes=(other_scope,))


def test_stale_approval_cannot_succeed_even_with_valid_current_provenance_chain(env):
    prior = env.approve(env.create())
    first = env.repo.derive(env.derivation(prior, target=final('first')))
    second = env.repo.derive(env.derivation(prior, target=final('second')))
    first_command = env.review(first, prior=prior)
    second_command = env.review(second, prior=prior)
    # A caller may know both human review events before either TM write. An
    # extra parent must never substitute for the expected current approval.
    request = env.provenance.get(second_command.provenance_id).record.request
    evidence = env.provenance.append(replace(request, parents=(*request.parents, first_command.provenance_id)))
    second_command = replace(second_command, provenance_id=evidence.record_id)
    approved = env.repo.review(first_command)
    with pytest.raises(MemoryConflictError, match='^stale_approval$'):
        env.repo.review(second_command)
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved
    assert env.repo.get(second.revision_id, scopes=(PROJECT_SCOPE,)) == second


def test_approval_lock_contention_is_typed_conflict_and_retry_keeps_exact_operation(env):
    from contextlib import closing
    candidate = env.create()
    command = env.review(candidate)
    with closing(sqlite3.connect(env.database, isolation_level=None)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(MemoryConflictError, match='^concurrent_write$'):
            env.repo.review(command)
        writer.execute('ROLLBACK')
    assert env.repo.get(candidate.revision_id, scopes=(PROJECT_SCOPE,)) == candidate
    assert env.raw('SELECT COUNT(*) FROM tm_operations WHERE operation_id = ?', (command.operation_id,)) == [(0,)]
    approved = env.repo.review(command)
    assert approved.state is MemoryState.APPROVED


@pytest.mark.parametrize('imported', [False, True])
def test_provenance_storage_failure_is_distinct_from_missing_evidence(env, monkeypatch, imported):
    from ayvu.provenance import ProvenanceStoreError
    origin = env.evidence('tm_import', str(uuid4())).record_id if imported else None
    command = env.creation(scope=IMPORT_SCOPE if imported else PROJECT_SCOPE, import_origin=origin)
    failed_id = origin if imported else command.value.provenance_id
    original_get = env.provenance.get
    reached = []

    def fail_selected_read(record_id):
        reached.append(record_id)
        if record_id == failed_id:
            raise ProvenanceStoreError('io')
        return original_get(record_id)

    monkeypatch.setattr(env.provenance, 'get', fail_selected_read)
    with pytest.raises(MemoryStorageError, match='^provenance_storage_failed$'):
        env.repo.create(command)
    assert reached[-1] == failed_id
    if imported:
        assert reached == [command.value.provenance_id, origin]
    assert env.repo.list_entries(scopes=SCOPES).revisions == ()
    assert env.raw('SELECT COUNT(*) FROM tm_operations') == [(0,)]
