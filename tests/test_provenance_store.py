from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import logging
import sqlite3
from threading import Barrier
from uuid import UUID

import pytest

from ayvu.provenance import (
    ActorKind, ProvenanceConflictError, ProvenanceLimitError, ProvenanceNotFoundError, ProvenanceRecord,
    ProvenanceReferenceError, ProvenanceStoreError, ProvenanceValidationError, SourceKind, encode_record,
)
from ayvu.project_schema import PROJECT_MIGRATIONS
from ayvu.provenance_store import MAX_LIST_LIMIT, MAX_TRAVERSAL_DEPTH, MAX_TRAVERSAL_RECORDS, SqliteProvenanceRepository
from ayvu.sqlite_migrations import SqliteMigrationRunner
from provenance_fakes import INPUT_FINGERPRINT, MACHINE, PROJECT, START, SyntheticLineage, subject

MISSING = str(UUID(int=98))
OTHER_PROJECT = str(UUID(int=99))
BIG = dict(max_depth=64, max_records=1024)


@pytest.fixture
def env(tmp_path):
    return SyntheticLineage(tmp_path)


def raw(env, sql, *parameters, foreign_keys=False):
    with closing(sqlite3.connect(env.database)) as connection:
        if foreign_keys:
            connection.execute('PRAGMA foreign_keys=ON')
        rows = connection.execute(sql, parameters).fetchall()
        connection.commit()
        return rows


def counts(env):
    return tuple(raw(env, f'SELECT COUNT(*) FROM {table}')[0][0]
                 for table in ('provenance_records', 'provenance_parents', 'provenance_artifacts'))


def foreign_record(env, record_id=MISSING):
    """A row of another project, which only a damaged or tampered database can contain."""
    record = ProvenanceRecord(record_id, START, env.request(project_id=OTHER_PROJECT))
    raw(env, 'INSERT INTO provenance_records (record_id, schema_version, project_id, job_id, recorded_at, payload) '
             'VALUES (?, 1, ?, ?, ?, ?)', record_id, OTHER_PROJECT, env.job.job_id, START.isoformat(), encode_record(record))
    return record


def insert_cyclic_pair(env):
    first, second = str(UUID(int=71)), str(UUID(int=72))
    for record_id, parent in ((first, second), (second, first)):
        record = ProvenanceRecord(record_id, START, env.request(parents=(parent,)))
        raw(env, 'INSERT INTO provenance_records (record_id, schema_version, project_id, job_id, recorded_at, payload) '
                 'VALUES (?, 1, ?, ?, ?, ?)', record_id, PROJECT, env.job.job_id, START.isoformat(), encode_record(record))
    return first


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


def test_mixed_graph_traces_a_final_artifact_back_to_the_input(env):
    records = env.build_mixed_graph()
    reopened = env.open()
    trace = reopened.trace_artifact(env.artifact.artifact_id, **BIG)
    assert trace.producers == (records['publish'].record_id,) and not trace.graph.truncated
    found = {node.record.record_id: node.record for node in trace.graph.nodes}
    assert set(found) == {record.record_id for record in records.values()}
    assert {record.request.source_kind for record in found.values()} == {
        SourceKind.PIPELINE, SourceKind.CACHE, SourceKind.TRANSLATION_MEMORY, SourceKind.PROVIDER,
        SourceKind.HUMAN, SourceKind.VALIDATION}
    assert [(item.job_id, item.input_fingerprint) for item in trace.jobs] == [(env.job.job_id, INPUT_FINGERPRINT)]
    outcomes = {record.request.operation: record.request.outcome_code for record in found.values()}
    assert outcomes['human_review'] == 'approved' and outcomes['validate'] == 'passed'
    assert found[records['review'].record_id].request.actor_kind is ActorKind.HUMAN
    assert found[records['review'].record_id].request.subject == subject('unit-3')
    assert found[records['extract'].record_id].request.parents == ()


def test_traversal_is_deterministic_and_starts_from_the_requested_record(env):
    records = env.build_mixed_graph()
    first = env.repository.lineage(records['publish'].record_id, **BIG)
    second = env.open().lineage(records['publish'].record_id, **BIG)
    assert [node.record.record_id for node in first.nodes] == [node.record.record_id for node in second.nodes]
    assert first.nodes[0].record == records['publish']
    assert env.repository.lineage(records['extract'].record_id, **BIG).nodes[0].record == records['extract']
    assert len(env.repository.lineage(records['extract'].record_id, **BIG).nodes) == 1


def test_diamond_ancestry_is_not_mistaken_for_a_cycle(env):
    root = env.append('root')
    left, right = env.append('left', parents=(root.record_id,)), env.append('right', parents=(root.record_id,))
    join = env.append('join', parents=(left.record_id, right.record_id))
    graph = env.repository.lineage(join.record_id, **BIG)
    assert len(graph.nodes) == 4 and not graph.truncated


def chain(env, length):
    record = env.append('step')
    for _ in range(length - 1):
        record = env.append('step', parents=(record.record_id,))
    return record


def test_depth_bound_is_exact_and_reported(env):
    tip = chain(env, 4)
    assert not env.repository.lineage(tip.record_id, max_depth=3, max_records=100).truncated
    graph = env.repository.lineage(tip.record_id, max_depth=2, max_records=100)
    assert graph.truncated and len(graph.nodes) == 3
    assert env.repository.lineage(tip.record_id, max_depth=1, max_records=100).truncated


def test_record_bound_is_exact_and_reported(env):
    tip = chain(env, 4)
    assert not env.repository.lineage(tip.record_id, max_depth=10, max_records=4).truncated
    graph = env.repository.lineage(tip.record_id, max_depth=10, max_records=3)
    assert graph.truncated and len(graph.nodes) == 3
    assert [node.record.record_id for node in graph.nodes] == [
        node.record.record_id for node in env.open().lineage(tip.record_id, max_depth=10, max_records=3).nodes]


@pytest.mark.parametrize('bounds', [
    dict(max_depth=None, max_records=10), dict(max_depth=10, max_records=None), dict(max_depth=0, max_records=10),
    dict(max_depth=10, max_records=0), dict(max_depth=True, max_records=10), dict(max_depth=10, max_records='10'),
    dict(max_depth=MAX_TRAVERSAL_DEPTH + 1, max_records=10), dict(max_depth=10, max_records=MAX_TRAVERSAL_RECORDS + 1),
], ids=str)
def test_unbounded_traversal_requests_fail(env, bounds):
    record = env.append()
    artifact = env.publish_artifact()
    with pytest.raises(ProvenanceLimitError, match='^invalid_traversal_limit$'):
        env.repository.lineage(record.record_id, **bounds)
    with pytest.raises(ProvenanceLimitError, match='^invalid_traversal_limit$'):
        env.repository.trace_artifact(artifact.artifact_id, **bounds)


def test_bounds_are_required_arguments(env):
    record = env.append()
    with pytest.raises(TypeError):
        env.repository.lineage(record.record_id)
    with pytest.raises(TypeError):
        env.repository.list_by_job(env.job.job_id)


@pytest.mark.parametrize('limit', [None, 0, -1, True, '1', MAX_LIST_LIMIT + 1])
def test_list_limits_are_validated(env, limit):
    for call in (lambda: env.repository.list_by_job(env.job.job_id, limit=limit),
                 lambda: env.repository.list_by_subject(subject('unit-1'), limit=limit),
                 lambda: env.repository.list_by_artifact(MISSING, limit=limit)):
        with pytest.raises(ProvenanceLimitError, match='^invalid_limit$'):
            call()


def test_queries_by_job_subject_and_artifact_are_ordered_and_report_truncation(env):
    records = env.build_mixed_graph()
    other_job = env.running_job()
    other = env.append('translate', job_id=other_job.job_id, subject=subject('unit-3'))
    by_job = env.repository.list_by_job(env.job.job_id, limit=MAX_LIST_LIMIT)
    assert [node.record for node in by_job.nodes] == list(records.values()) and not by_job.truncated
    first_two = env.repository.list_by_job(env.job.job_id, limit=2)
    assert len(first_two.nodes) == 2 and first_two.truncated
    assert [node.record for node in env.repository.list_by_job(env.job.job_id, limit=len(records)).nodes] == list(records.values())
    assert not env.repository.list_by_job(env.job.job_id, limit=len(records)).truncated
    by_subject = env.repository.list_by_subject(subject('unit-3'), limit=10)
    assert [node.record for node in by_subject.nodes] == [records['machine'], records['review'], other]
    assert env.repository.list_by_subject(subject('unit-9'), limit=10).nodes == ()
    by_artifact = env.repository.list_by_artifact(env.artifact.artifact_id, limit=10)
    assert [node.record for node in by_artifact.nodes] == [records['publish']]
    assert env.repository.list_by_artifact(MISSING, limit=10).nodes == ()


def test_subject_query_distinguishes_namespace_and_version(env):
    from ayvu.provenance import SubjectRef
    env.append(subject=SubjectRef('content_unit', 1, 'unit-1'))
    assert env.repository.list_by_subject(SubjectRef('content_unit', 2, 'unit-1'), limit=5).nodes == ()
    assert env.repository.list_by_subject(SubjectRef('other', 1, 'unit-1'), limit=5).nodes == ()
    with pytest.raises(ProvenanceValidationError, match='^invalid_subject$'):
        env.repository.list_by_subject('unit-1', limit=5)


def test_trace_of_an_artifact_without_records_is_empty_and_of_an_unknown_one_is_an_error(env):
    artifact = env.publish_artifact()
    trace = env.repository.trace_artifact(artifact.artifact_id, **BIG)
    assert trace.producers == () and trace.graph.nodes == () and trace.jobs == ()
    with pytest.raises(ProvenanceNotFoundError, match='^artifact_not_found$'):
        env.repository.trace_artifact(MISSING, **BIG)


def test_trace_reports_truncation_when_there_are_more_producers_than_the_bound(env):
    artifact = env.publish_artifact()
    for _ in range(3):
        env.append('publish', artifacts=(artifact.artifact_id,))
    trace = env.repository.trace_artifact(artifact.artifact_id, max_depth=5, max_records=2)
    assert trace.graph.truncated and len(trace.producers) == 2 and len(trace.graph.nodes) == 2


def test_trace_crosses_a_continuation_to_each_job_and_its_fingerprint(env):
    first = env.append('translate')
    other_job = env.running_job()
    artifact = env.publish_artifact()
    tip = env.append('publish', job_id=other_job.job_id, parents=(first.record_id,), artifacts=(artifact.artifact_id,))
    trace = env.repository.trace_artifact(artifact.artifact_id, **BIG)
    assert trace.producers == (tip.record_id,)
    assert {item.job_id for item in trace.jobs} == {env.job.job_id, other_job.job_id}


def test_explicit_unknowns_survive_persistence_and_are_not_identified(env):
    record = env.append('translate', producer=MACHINE)
    stored = env.open().get(record.record_id).record.request.producer
    assert stored.model == 'unknown' and stored.fully_identified is False


def test_review_style_identifier_round_trips_as_an_opaque_value(env):
    from ayvu.provenance import SubjectRef
    value = SubjectRef('legacy_review_position', 1, 'c0001-s0001')
    record = env.append(subject=value)
    assert env.open().get(record.record_id).record.request.subject == value
    assert [node.record for node in env.repository.list_by_subject(value, limit=5).nodes] == [record]
    assert env.repository.list_by_subject(SubjectRef('legacy_review_position', 1, 'c0001-s0002'), limit=5).nodes == ()


def test_provenance_modules_do_not_know_the_review_numbering_scheme():
    from pathlib import Path
    root = Path(__file__).parents[1] / 'src' / 'ayvu'
    for name in ('provenance.py', 'provenance_service.py', 'provenance_store.py'):
        text = (root / name).read_text()
        assert 'review_export' not in text and 'parse_segment_id' not in text and 'c####' not in text


def test_duplicate_generated_identity_fails_without_partial_edges(tmp_path):
    env = SyntheticLineage(tmp_path)
    parent = env.append()
    fixed = SqliteProvenanceRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS), env.database, PROJECT, clock=env.clock,
                                       new_id=lambda: parent.record_id)
    before = counts(env)
    with pytest.raises(ProvenanceStoreError, match='^sql_failure$'):
        fixed.append(env.request())
    assert counts(env) == before


def test_failure_after_the_record_insert_rolls_everything_back(env):
    parent = env.append()
    artifact = env.publish_artifact()
    before = counts(env)
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute("CREATE TRIGGER fail_edges BEFORE INSERT ON provenance_artifacts "
                           "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        connection.commit()
    with pytest.raises(ProvenanceStoreError):
        env.append(parents=(parent.record_id,), artifacts=(artifact.artifact_id,))
    assert counts(env) == before


def test_write_holds_the_lock_while_it_checks_references(env):
    attempts = []
    parent = env.append()

    def observing_clock():
        with closing(sqlite3.connect(env.database, timeout=0, isolation_level=None)) as other:
            try:
                other.execute('BEGIN IMMEDIATE')
                other.execute('ROLLBACK')
                attempts.append('free')
            except sqlite3.OperationalError:
                attempts.append('locked')
        return START

    repository = SqliteProvenanceRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS), env.database, PROJECT,
                                            clock=observing_clock, new_id=env.new_id)
    repository.append(env.request(parents=(parent.record_id,)))
    assert attempts == ['locked']


def test_reads_use_a_read_only_connection(env):
    record = env.append()
    seen = []
    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS)
    original = runner.connect

    def spy(database, *, readonly):
        seen.append(readonly)
        return original(database, readonly=readonly)

    runner.connect = spy
    repository = SqliteProvenanceRepository(runner, env.database, PROJECT, clock=env.clock)
    before = env.database.read_bytes()
    repository.get(record.record_id)
    repository.list_by_job(env.job.job_id, limit=5)
    repository.lineage(record.record_id, **BIG)
    assert seen == [True, True, True] and env.database.read_bytes() == before


def test_busy_store_is_typed_and_writes_nothing(env):
    repository = SqliteProvenanceRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS, busy_timeout=0.01), env.database,
                                            PROJECT, clock=env.clock, new_id=env.new_id)
    before = counts(env)
    with closing(sqlite3.connect(env.database, isolation_level=None)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(ProvenanceStoreError, match='^busy$'):
            repository.append(env.request())
        blocker.execute('ROLLBACK')
    assert counts(env) == before


def test_unmigrated_database_is_refused(tmp_path):
    older = tmp_path / 'older.sqlite'
    assert SqliteMigrationRunner().migrate(older, tmp_path / 'backups').ok
    repository = SqliteProvenanceRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS), older, PROJECT)
    with pytest.raises(ProvenanceStoreError, match='^version_gap$'):
        repository.get(MISSING)
    with pytest.raises(ProvenanceStoreError, match='^io$'):
        SqliteProvenanceRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS), tmp_path / 'absent', PROJECT).get(MISSING)
    assert not (tmp_path / 'absent').exists()


def _replace_payload(env, record_id, payload):
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER provenance_records_no_update')
        connection.execute('UPDATE provenance_records SET payload = ? WHERE record_id = ?', (payload, record_id))
        connection.commit()


@pytest.mark.parametrize('payload', ['private-canary-text', '{}'])
def test_corrupt_payload_fails_closed_without_exposing_it(env, payload):
    record = env.append()
    _replace_payload(env, record.record_id, payload)
    with pytest.raises(ProvenanceStoreError, match='^invalid_provenance_record$') as caught:
        env.repository.get(record.record_id)
    assert 'canary' not in str(caught.value) and caught.value.__cause__ is None


def test_payload_of_another_record_is_rejected(env):
    first, second = env.append(), env.append()
    _replace_payload(env, first.record_id, encode_record(second))
    with pytest.raises(ProvenanceStoreError, match='^invalid_provenance_record$'):
        env.repository.get(first.record_id)


def test_unsupported_stored_version_is_distinct(env):
    import json
    record = env.append()
    data = json.loads(encode_record(record))
    data['schema_version'] = 2
    _replace_payload(env, record.record_id, json.dumps(data, sort_keys=True, separators=(',', ':')))
    with pytest.raises(ProvenanceValidationError, match='^unsupported_provenance_version$'):
        env.repository.get(record.record_id)


def test_invalid_arguments_are_rejected_before_any_write(env):
    with pytest.raises(ProvenanceValidationError, match='^invalid_request$'):
        env.repository.append('text')
    with pytest.raises(ProvenanceValidationError, match='^invalid_identity$'):
        SqliteProvenanceRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS), env.database, 'not-a-uuid')
    assert counts(env) == (0, 0, 0)


CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub', 'Translate this prompt',
            'https://user:hunter2@example.test/translate', 'whale => baleia')


@pytest.mark.parametrize('canary', CANARIES)
def test_canaries_never_reach_storage_repr_errors_or_logs(env, canary, caplog):
    caplog.set_level(logging.DEBUG)
    records = env.build_mixed_graph()
    with pytest.raises(ProvenanceValidationError) as caught:
        env.repository.get(canary)
    texts = [str(caught.value), repr(caught.value), repr(env.repository), caplog.text]
    texts += [repr(record) for record in records.values()]
    texts += [encode_record(record) for record in records.values()]
    assert not any(canary in text for text in texts)
    dump = '\n'.join(sqlite3.connect(env.database).iterdump())
    assert canary not in dump and str(env.database.parent) not in dump


def test_truncation_among_siblings_follows_a_fixed_order(env):
    parents = [env.append('parent') for _ in range(6)]
    tip = env.append('join', parents=tuple(parent.record_id for parent in parents))
    graph = env.repository.lineage(tip.record_id, max_depth=5, max_records=4)
    assert graph.truncated
    assert [node.record.record_id for node in graph.nodes[1:]] == sorted(parent.record_id for parent in parents)[:3]
    complete = env.repository.lineage(tip.record_id, max_depth=5, max_records=7)
    assert [node.record.record_id for node in complete.nodes[1:]] == sorted(parent.record_id for parent in parents)


def test_artifact_queries_exclude_a_linked_record_of_another_project(env):
    artifact = env.publish_artifact()
    mine = env.append('publish', artifacts=(artifact.artifact_id,))
    foreign = foreign_record(env)
    raw(env, 'INSERT INTO provenance_artifacts (record_id, artifact_id) VALUES (?, ?)', foreign.record_id, artifact.artifact_id)
    assert [node.record for node in env.repository.list_by_artifact(artifact.artifact_id, limit=10).nodes] == [mine]
    assert env.repository.trace_artifact(artifact.artifact_id, **BIG).producers == (mine.record_id,)


def test_traversal_reads_one_snapshot_that_a_writer_cannot_change_midway(env):
    tip = chain(env, 3)
    observed = []

    class Observing(SqliteProvenanceRepository):
        def _load(self, connection, record_id):
            record = super()._load(connection, record_id)
            with closing(sqlite3.connect(env.database, timeout=0, isolation_level=None)) as writer:
                try:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('CREATE TABLE attempt_to_change(value)')
                    writer.execute('COMMIT')
                    observed.append('changed')
                except sqlite3.OperationalError:
                    observed.append('protected')
            return record

    repository = Observing(SqliteMigrationRunner(PROJECT_MIGRATIONS), env.database, PROJECT, clock=env.clock)
    repository.lineage(tip.record_id, **BIG)
    assert observed and set(observed) == {'protected'}
