from contextlib import closing
import sqlite3
from uuid import UUID

import pytest

from ayvu.jobs.model import JobLeaseError, JobState, JobStoreError, JobTerminalSummary
from ayvu.jobs.store import SqliteJobRepository
from ayvu.projects.schema import PROJECT_MIGRATIONS, open_job_repository
from ayvu.kernel import JobValidationError
from ayvu.kernel.sqlite import SqliteMigrationRunner

from tests.jobs.job_store_support import MINUTE, MISSING, PROJECT
from tests.jobs.job_store_support import Store, request, store


@pytest.mark.parametrize('state', [JobState.VALIDATED, JobState.PARTIALLY_COMPLETED, JobState.FAILED, JobState.CANCELLED])
def test_database_itself_refuses_to_change_a_terminal_job(store, state):
    job = store.advance_to(store.repository.create(request()), state)
    with pytest.raises(sqlite3.IntegrityError, match='immutable_job'):
        store.raw("UPDATE translation_jobs SET state = 'queued' WHERE job_id = ?", job.job_id)
    assert store.repository.get(job.job_id).job == job


@pytest.mark.parametrize('assignment', [
    "input_fingerprint = 'sha256:" + 'f' * 64 + "'", "project_id = 'x'", "command_type = 'x'", 'command_version = 9',
    "output_roles = '[]'", "created_at = 'x'", "job_id = 'x'", 'revision = 0',
])
def test_database_itself_refuses_to_change_immutable_columns(store, assignment):
    job = store.repository.create(request())
    with pytest.raises(sqlite3.IntegrityError):
        store.raw(f'UPDATE translation_jobs SET {assignment} WHERE job_id = ?', job.job_id)
    assert store.repository.get(job.job_id).job == job


@pytest.mark.parametrize('assignment', [
    "state = 'bogus'", "terminal_summary = '{}'", "lease_owner_token = 'x'", "continuation_of = job_id",
])
def test_database_constraints_reject_invalid_shapes(store, assignment):
    job = store.repository.create(request())
    with pytest.raises(sqlite3.IntegrityError):
        store.raw(f'UPDATE translation_jobs SET {assignment} WHERE job_id = ?', job.job_id)


def test_database_refuses_a_dangling_continuation_and_deleting_a_linked_job(store):
    terminal = store.advance_to(store.repository.create(request()), JobState.CANCELLED)
    retry = store.repository.create_continuation(terminal.job_id, request())
    with pytest.raises(sqlite3.IntegrityError):
        store.raw('DELETE FROM translation_jobs WHERE job_id = ?', terminal.job_id)
    assert store.repository.get(retry.job_id).job == retry
    other = store.repository.create(request())
    with pytest.raises(sqlite3.IntegrityError):
        store.raw('INSERT INTO translation_jobs SELECT ?, project_id, command_type, command_version, '
                  'input_fingerprint, output_roles, state, revision, created_at, updated_at, NULL, NULL, NULL, NULL, ? '
                  'FROM translation_jobs WHERE job_id = ?', MISSING, str(UUID(int=97)), other.job_id)


def test_duplicate_generated_identity_fails_without_mutation(tmp_path):
    store = Store(tmp_path)
    fixed = open_job_repository(store.database, store.backups, PROJECT, clock=store.clock,
                                new_id=lambda: str(UUID(int=5)))
    first = fixed.create(request())
    before = store.bytes()
    with pytest.raises(JobStoreError, match='^sql_failure$'):
        fixed.create(request())
    assert store.bytes() == before and fixed.get(first.job_id).job == first


@pytest.mark.parametrize('assignment', [
    "state = 'queued', output_roles = 'not-json'", "output_roles = '{\"role\": 1}'",
    "updated_at = '2026-11-03T12:00:00'", "lease_owner_token = 'PRIVATE-CANARY', "
    "lease_acquired_at = 'x', lease_expires_at = 'y'",
])
def test_corrupt_rows_fail_closed_without_exposing_stored_data(store, assignment):
    job = store.repository.create(request())
    with closing(sqlite3.connect(store.database)) as connection:
        connection.execute('DROP TRIGGER translation_jobs_immutable')
        connection.execute(f'UPDATE translation_jobs SET {assignment} WHERE job_id = ?', (job.job_id,))
        connection.commit()
    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS)
    with pytest.raises(JobStoreError, match='^invalid_job_record$'):
        SqliteJobRepository(runner, store.database, PROJECT, clock=store.clock).get(job.job_id)


def test_missing_or_unmigrated_database_is_refused_without_creating_anything(tmp_path):
    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS)
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises(JobStoreError, match='^io$'):
        SqliteJobRepository(runner, missing, PROJECT).get(MISSING)
    assert not missing.exists()

    older = tmp_path / 'older.sqlite'
    assert SqliteMigrationRunner().migrate(older, tmp_path / 'backups').ok
    before = older.read_bytes()
    with pytest.raises(JobStoreError, match='^version_gap$'):
        SqliteJobRepository(runner, older, PROJECT).create(request())
    assert older.read_bytes() == before


def test_changed_ledger_is_detected_on_every_connection(store):
    job = store.repository.create(request())
    store.raw("UPDATE ayvu_schema_migrations SET checksum = 'tampered' WHERE version = 2")
    with pytest.raises(JobStoreError, match='^checksum_mismatch$'):
        store.repository.get(job.job_id)


def test_nothing_private_reaches_errors_repr_or_summary_storage(store):
    canary = 'private-canary-text'
    job = store.repository.create(request())
    for attempt in (lambda: store.repository.get(canary),
                    lambda: store.repository.renew_lease(job.job_id, canary, MINUTE),
                    lambda: store.repository.create(request(command_type=canary))):
        with pytest.raises((JobValidationError, JobLeaseError)) as caught:
            attempt()
        assert canary not in str(caught.value)
    assert str(store.database) not in repr(store.repository)
    with pytest.raises(JobValidationError):
        JobTerminalSummary(canary)


def test_jobs_leave_legacy_resume_state_and_other_files_untouched(tmp_path):
    resume_state = tmp_path / 'book.ayvu-state.json'
    resume_state.write_bytes(b'{"version": 1, "status": "running"}')
    store = Store(tmp_path)
    job = store.advance_to(store.repository.create(request()), JobState.RUNNING)
    store.repository.acquire_lease(job.job_id, MINUTE)
    store.repository.transition(job.job_id, JobState.FAILED, expected_revision=job.revision)
    assert resume_state.read_bytes() == b'{"version": 1, "status": "running"}'
    assert {path.name for path in tmp_path.iterdir()} == {
        'book.ayvu-state.json', 'project.sqlite', '.project.sqlite.migrate.lock',
    }


def test_reads_use_a_read_only_connection_and_writes_enforce_foreign_keys(store):
    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS)
    with closing(runner.connect(store.database, readonly=True)) as reader:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("DELETE FROM translation_jobs")
    with closing(runner.connect(store.database, readonly=False)) as writer:
        assert writer.execute('PRAGMA foreign_keys').fetchone() == (1,)
    seen = []
    original = store.repository._runner.connect

    def spy(database, *, readonly):
        seen.append(readonly)
        return original(database, readonly=readonly)

    store.repository._runner = type('Spy', (), {'connect': staticmethod(spy)})()
    job = store.repository.create(request())
    assert seen == [False]
    store.repository.get(job.job_id)
    store.repository.list_jobs(PROJECT)
    store.repository.get_continuation(job.job_id)
    assert seen == [False, True, True, True]


def test_write_lock_is_held_before_the_job_is_read_and_decided(store):
    job = store.repository.create(request())
    attempts = []

    def clock_observing_the_lock():
        # The clock runs after the row is read, while the domain rule is decided.
        with closing(sqlite3.connect(store.database, timeout=0, isolation_level=None)) as other:
            try:
                other.execute('BEGIN IMMEDIATE')
                other.execute('ROLLBACK')
                attempts.append('free')
            except sqlite3.OperationalError:
                attempts.append('locked')
        return store.clock()

    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS)
    observed = SqliteJobRepository(runner, store.database, PROJECT, clock=clock_observing_the_lock)
    observed.transition(job.job_id, JobState.PREFLIGHT, expected_revision=1)
    assert attempts == ['locked']
