from dataclasses import replace
import logging
import sqlite3
from uuid import UUID, uuid4

import pytest

from ayvu.knowledge.memory.approved.model import MemoryConflictError, MemoryProvenanceError, MemoryState, MemoryStorageError, MemoryValidationError
from ayvu.knowledge.memory.approved.service import CreateCandidate
from ayvu.provenance.model import ActorKind
from tests.support.approved_memory_fixtures import IMPORT_SCOPE, PAIR, PIN, PROJECT_SCOPE, SCOPES, final, source

from tests.knowledge.memory.approved.approved_memory_support import env


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


def test_storage_size_failure_rolls_back_new_revision(env, monkeypatch):
    import ayvu.knowledge.memory.approved.store as store
    command = env.creation()
    monkeypatch.setattr(store, 'MAX_DATABASE_BYTES', 1)
    with pytest.raises(MemoryStorageError, match='^resource_limit$'):
        env.repo.create(command)
    assert env.repo.list_entries(scopes=SCOPES).revisions == ()
    assert env.raw('SELECT COUNT(*) FROM tm_operations') == [(0,)]


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
    from ayvu.provenance.model import ProvenanceStoreError
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
