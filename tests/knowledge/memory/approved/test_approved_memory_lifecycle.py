from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest

from ayvu.knowledge.memory.approved.model import NORMALIZATION_VERSION, SEARCH_VERSION, TRANSITIONS, Candidate, MemoryConflictError, MemoryNotFoundError, MemoryProvenanceError, MemoryState, MemoryStorageError, MemoryTransitionError, MemoryValueError, PipelineStage, TargetValue, ValueKind, require_transition
from ayvu.provenance.model import ActorKind, ConfigurationRef
from tests.support.approved_memory_fixtures import NOW, PAIR, PIN, PROJECT_SCOPE, WORK_SCOPE, final, source

from tests.knowledge.memory.approved.approved_memory_support import env


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
