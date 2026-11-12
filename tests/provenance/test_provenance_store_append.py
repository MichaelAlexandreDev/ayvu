from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier
from uuid import UUID

import pytest

from ayvu.provenance.model import ProvenanceConflictError, ProvenanceNotFoundError, ProvenanceRecord, ProvenanceReferenceError, ProvenanceStoreError, ProvenanceValidationError, SourceKind, encode_record
from tests.support.provenance_fakes import PROJECT, START, subject

from tests.provenance.provenance_store_support import BIG, MISSING, OTHER_PROJECT
from tests.provenance.provenance_store_support import counts, env, foreign_record, insert_cyclic_pair
from tests.provenance.provenance_store_support import raw


def test_appended_record_survives_reopening_exactly(env):
    record = env.append('translate', subject=subject('unit-1'), job_sequence=3, correlation_id=str(UUID(int=5)))
    node = env.open().get(record.record_id)
    assert node.record == record and node.current and node.superseded_by is None
    assert node.record.request.job_sequence == 3 and node.record.request.correlation_id == str(UUID(int=5))
    assert node.record.recorded_at > START


def test_unknown_record_and_invalid_identity_are_typed(env):
    with pytest.raises(ProvenanceNotFoundError, match='^record_not_found$'):
        env.repository.get(MISSING)
    with pytest.raises(ProvenanceValidationError, match='^invalid_identity$'):
        env.repository.get('../../etc/passwd')


def test_repeating_an_idempotency_key_with_the_same_content_returns_the_stored_record(env):
    first = env.append('translate', idempotency_key='step-1', job_sequence=1)
    before = counts(env)
    assert env.open().append(env.request('translate', idempotency_key='step-1', job_sequence=1)) == first
    assert counts(env) == before


@pytest.mark.parametrize('changes', [
    {'operation': 'other'}, {'job_sequence': 2}, {'outcome_code': 'x'}, {'subject': subject('unit-1')},
    {'source_kind': SourceKind.CACHE},
], ids=lambda changes: next(iter(changes)))
def test_reusing_a_key_with_different_content_fails_and_writes_nothing(env, changes):
    env.append('translate', idempotency_key='step-1', job_sequence=1)
    before = counts(env)
    with pytest.raises(ProvenanceConflictError, match='^idempotency_conflict$'):
        env.append(**{'operation': 'translate', 'job_sequence': 1, 'idempotency_key': 'step-1', **changes})
    assert counts(env) == before


def test_records_without_a_key_are_distinct_facts(env):
    assert env.append().record_id != env.append().record_id


def test_racing_idempotent_appends_store_one_record(env):
    barrier = Barrier(4)

    def attempt(_):
        barrier.wait(timeout=5)
        return env.open().append(env.request('translate', idempotency_key='step-1'))

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert len({record.record_id for record in results}) == 1 and counts(env)[0] == 1


@pytest.mark.parametrize('field,error', [
    ('parents', 'missing_parent'), ('supersedes', 'missing_superseded_record'), ('artifacts', 'missing_artifact'),
])
def test_missing_references_fail_without_a_partial_write(env, field, error):
    parent = env.append()
    before = counts(env)
    changes = {'supersedes': MISSING} if field == 'supersedes' else {field: (parent.record_id, MISSING)
                                                                     if field == 'parents' else (MISSING,)}
    with pytest.raises(ProvenanceReferenceError, match=f'^{error}$'):
        env.append('next', **changes)
    assert counts(env) == before


def test_missing_job_fails_without_a_write(env):
    before = counts(env)
    with pytest.raises(ProvenanceReferenceError, match='^missing_job$'):
        env.append(job_id=MISSING)
    assert counts(env) == before


def test_a_new_record_cannot_reference_itself_so_the_write_path_cannot_form_a_cycle(env):
    upcoming = str(UUID(int=env.next_id + 1))
    before = counts(env)
    with pytest.raises(ProvenanceReferenceError, match='^missing_parent$'):
        env.append(parents=(upcoming,))
    with pytest.raises(ProvenanceReferenceError, match='^missing_superseded_record$'):
        env.append(supersedes=upcoming)
    assert counts(env) == before


def test_cycle_in_a_tampered_database_is_detected_by_every_traversal(env):
    start = insert_cyclic_pair(env)
    with pytest.raises(ProvenanceStoreError, match='^lineage_cycle$'):
        env.repository.lineage(start, **BIG)


def test_cross_project_references_fail_without_a_partial_write(env):
    foreign = foreign_record(env)
    before = counts(env)
    with pytest.raises(ProvenanceReferenceError, match='^cross_project_reference$'):
        env.append(parents=(foreign.record_id,))
    with pytest.raises(ProvenanceReferenceError, match='^cross_project_reference$'):
        env.append(supersedes=foreign.record_id)
    with pytest.raises(ProvenanceReferenceError, match='^cross_project_reference$'):
        env.append(project_id=OTHER_PROJECT)
    assert counts(env) == before


def test_cross_project_artifact_and_job_are_refused(env):
    artifact = env.publish_artifact()
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute('UPDATE job_artifacts SET project_id = ?', (OTHER_PROJECT,))
        connection.commit()
    with pytest.raises(ProvenanceReferenceError, match='^cross_project_reference$'):
        env.append(artifacts=(artifact.artifact_id,))
    with pytest.raises(ProvenanceNotFoundError, match='^artifact_not_found$'):
        env.repository.trace_artifact(artifact.artifact_id, **BIG)
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER translation_jobs_immutable')
        connection.execute('UPDATE translation_jobs SET project_id = ?', (OTHER_PROJECT,))
        connection.commit()
    with pytest.raises(ProvenanceReferenceError, match='^cross_project_reference$'):
        env.append()


def test_no_query_returns_a_record_of_another_project(env):
    foreign = foreign_record(env)
    assert env.repository.list_by_job(env.job.job_id, limit=10).nodes == ()
    with pytest.raises(ProvenanceNotFoundError):
        env.repository.get(foreign.record_id)
    with pytest.raises(ProvenanceNotFoundError):
        env.repository.lineage(foreign.record_id, **BIG)


def test_lineage_stops_at_a_reference_into_another_project(env):
    foreign = foreign_record(env)
    tampered_id = str(UUID(int=73))
    record = ProvenanceRecord(tampered_id, START, env.request(parents=(foreign.record_id,)))
    raw(env, 'INSERT INTO provenance_records (record_id, schema_version, project_id, job_id, recorded_at, payload) '
             'VALUES (?, 1, ?, ?, ?, ?)', tampered_id, PROJECT, env.job.job_id, START.isoformat(), encode_record(record))
    with pytest.raises(ProvenanceStoreError, match='^invalid_provenance_record$'):
        env.repository.lineage(tampered_id, **BIG)


def test_correction_keeps_the_original_auditable_and_marks_it_historical(env):
    original = env.append('translate', subject=subject('unit-1'), outcome_code='first')
    before = raw(env, 'SELECT * FROM provenance_records WHERE record_id = ?', original.record_id)
    correction = env.append('translate', subject=subject('unit-1'), outcome_code='second', supersedes=original.record_id)
    assert raw(env, 'SELECT * FROM provenance_records WHERE record_id = ?', original.record_id) == before
    node = env.open().get(original.record_id)
    assert node.record == original and not node.current and node.superseded_by == correction.record_id
    assert env.repository.get(correction.record_id).current
    graph = env.repository.lineage(correction.record_id, **BIG)
    assert {item.record.record_id: item.current for item in graph.nodes} == {
        correction.record_id: True, original.record_id: False}


def test_a_record_is_corrected_at_most_once_and_the_current_one_is_corrected_next(env):
    original = env.append(subject=subject('unit-1'))
    second = env.append(subject=subject('unit-1'), supersedes=original.record_id)
    before = counts(env)
    with pytest.raises(ProvenanceConflictError, match='^already_superseded$'):
        env.append(subject=subject('unit-1'), supersedes=original.record_id)
    assert counts(env) == before
    third = env.append(subject=subject('unit-1'), supersedes=second.record_id)
    graph = env.repository.lineage(third.record_id, **BIG)
    assert [node.record.record_id for node in graph.nodes if node.current] == [third.record_id]
    assert len(graph.nodes) == 3


@pytest.mark.parametrize('first,second', [
    (subject('unit-1'), subject('unit-2')), (subject('unit-1'), None), (None, subject('unit-1')),
], ids=['other', 'dropped', 'added'])
def test_correction_must_keep_the_same_subject(env, first, second):
    original = env.append(subject=first)
    with pytest.raises(ProvenanceConflictError, match='^supersession_subject_mismatch$'):
        env.append(subject=second, supersedes=original.record_id)


def test_racing_corrections_have_exactly_one_winner(env):
    original = env.append(subject=subject('unit-1'))
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait(timeout=5)
        try:
            return env.open().append(env.request(subject=subject('unit-1'), supersedes=original.record_id, job_sequence=index + 1))
        except ProvenanceConflictError as error:
            return str(error)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count('already_superseded') == 1
    assert counts(env)[0] == 2


def test_repository_offers_no_way_to_change_a_record(env):
    public = {name for name in dir(env.repository) if not name.startswith('_')}
    assert public == {'append', 'get', 'list_by_job', 'list_by_subject', 'list_by_artifact', 'lineage', 'trace_artifact'}


@pytest.mark.parametrize('sql', [
    "UPDATE provenance_records SET payload = 'x'", "UPDATE provenance_records SET job_id = job_id",
    'DELETE FROM provenance_records', "UPDATE provenance_parents SET parent_id = parent_id",
    'DELETE FROM provenance_parents', "UPDATE provenance_artifacts SET artifact_id = artifact_id",
    'DELETE FROM provenance_artifacts',
])
def test_database_itself_refuses_to_change_or_prune_lineage(env, sql):
    env.build_mixed_graph()
    before = counts(env)
    with pytest.raises(sqlite3.IntegrityError, match='immutable_provenance'):
        raw(env, sql)
    assert counts(env) == before


def test_database_refuses_dangling_references_on_its_own(env):
    record = env.append()
    for sql, parameters in (
        ('INSERT INTO provenance_parents VALUES (?, ?)', (record.record_id, MISSING)),
        ('INSERT INTO provenance_parents VALUES (?, ?)', (record.record_id, record.record_id)),
        ('INSERT INTO provenance_artifacts VALUES (?, ?)', (record.record_id, MISSING)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            raw(env, sql, *parameters, foreign_keys=True)
