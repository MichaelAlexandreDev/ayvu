from contextlib import closing
import logging
import sqlite3

import pytest

from ayvu.provenance.model import ProvenanceStoreError, ProvenanceValidationError, encode_record
from ayvu.projects.schema import PROJECT_MIGRATIONS
from ayvu.provenance.store import SqliteProvenanceRepository
from tests.support.provenance_fakes import PROJECT, START, SyntheticLineage
from ayvu.kernel.sqlite import SqliteMigrationRunner

from tests.provenance.provenance_store_support import BIG, CANARIES, MISSING, _replace_payload
from tests.provenance.provenance_store_support import counts, env


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
