from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
import sqlite3
from threading import Barrier
from uuid import UUID

import pytest

from ayvu.job import (
    JobContinuationError, JobLeaseError, JobNotFoundError, JobRequest, JobRevisionConflictError, JobState,
    JobStoreError, JobTerminalSummary, JobTransitionError, JobValidationError,
)
from ayvu.job_store import MAX_LIST_LIMIT, SqliteJobRepository
from ayvu.project_schema import PROJECT_MIGRATIONS, open_job_repository
from ayvu.sqlite_migrations import SqliteMigrationRunner

PROJECT = str(UUID(int=1))
OTHER_PROJECT = str(UUID(int=99))
MISSING = str(UUID(int=98))
START = datetime(2026, 11, 3, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)

# The shortest route to each state, so every serialized value can be persisted.
ROUTES = {
    JobState.QUEUED: (),
    JobState.PREFLIGHT: (JobState.PREFLIGHT,),
    JobState.RUNNING: (JobState.PREFLIGHT, JobState.RUNNING),
    JobState.PAUSING: (JobState.PREFLIGHT, JobState.RUNNING, JobState.PAUSING),
    JobState.PAUSED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.PAUSING, JobState.PAUSED),
    JobState.CANCELLING: (JobState.PREFLIGHT, JobState.RUNNING, JobState.CANCELLING),
    JobState.CANCELLED: (JobState.CANCELLED,),
    JobState.FAILED: (JobState.PREFLIGHT, JobState.FAILED),
    JobState.PARTIALLY_COMPLETED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.PARTIALLY_COMPLETED),
    JobState.COMPLETED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED),
    JobState.VALIDATING: (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING),
    JobState.VALIDATED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING,
                         JobState.VALIDATED),
}


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, delta=timedelta(seconds=1)):
        self.now += delta


class Sequence:
    def __init__(self, render):
        self.render = render
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.render(self.count)


class Store:
    def __init__(self, tmp_path):
        self.database = tmp_path / 'project.sqlite'
        self.backups = tmp_path / 'backups' / 'schema'
        self.clock = Clock()
        self.ids = Sequence(lambda n: str(UUID(int=1000 + n)))
        self.tokens = Sequence(lambda n: f'{n:064x}')
        self.repository = self.open()

    def open(self):
        return open_job_repository(self.database, self.backups, PROJECT, clock=self.clock,
                                   new_id=self.ids, new_token=self.tokens)

    def raw(self, sql, *parameters):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            rows = connection.execute(sql, parameters).fetchall()
            connection.commit()
            return rows

    def row(self, job_id):
        return self.raw('SELECT * FROM translation_jobs WHERE job_id = ?', job_id)

    def bytes(self):
        return self.database.read_bytes()

    def advance_to(self, job, state):
        for target in ROUTES[state]:
            self.clock.advance()
            job = self.repository.transition(job.job_id, target, expected_revision=job.revision)
        return job


def request(**changes):
    return JobRequest(**{'project_id': PROJECT, 'command_type': 'translate_epub', 'command_version': 1,
                         'input_fingerprint': 'sha256:' + 'a' * 64,
                         'output_roles': ('translated_epub', 'report'), **changes})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_create_is_deterministic_and_get_returns_the_same_job(store):
    job = store.repository.create(request())
    assert job.job_id == str(UUID(int=1001))
    assert (job.state, job.revision, job.created_at, job.updated_at) == (JobState.QUEUED, 1, START, START)
    snapshot = store.repository.get(job.job_id)
    assert snapshot.job == job and not snapshot.requires_reconciliation


def test_get_unknown_job_and_invalid_identity_are_typed(store):
    with pytest.raises(JobNotFoundError, match='^job_not_found$'):
        store.repository.get(MISSING)
    with pytest.raises(JobValidationError, match='^invalid_job_identity$'):
        store.repository.get('../../etc/passwd')


def test_create_rejects_a_foreign_project(store):
    before = store.bytes()
    with pytest.raises(JobValidationError, match='^project_mismatch$'):
        store.repository.create(request(project_id=OTHER_PROJECT))
    assert store.bytes() == before


@pytest.mark.parametrize('state', sorted(JobState))
def test_every_state_round_trips_exactly_across_reopen(store, state):
    job = store.advance_to(store.repository.create(request()), state)
    reopened = store.open().get(job.job_id).job
    assert reopened == job
    assert store.row(job.job_id)[0][6] == state.value


def test_full_record_round_trips_across_reopen(store):
    job = store.advance_to(store.repository.create(request()), JobState.RUNNING)
    leased = store.repository.acquire_lease(job.job_id, MINUTE)
    assert store.open().get(job.job_id).job == leased
    summary = JobTerminalSummary('provider_unavailable', (('z_last', 2), ('a_first', 0)))
    failed = store.repository.transition(job.job_id, JobState.FAILED, expected_revision=job.revision, summary=summary)
    reopened = store.open().get(job.job_id).job
    assert reopened == failed
    assert reopened.terminal_summary.counters == (('z_last', 2), ('a_first', 0))
    assert reopened.request.output_roles == ('translated_epub', 'report')
    assert reopened.request.input_fingerprint == 'sha256:' + 'a' * 64


def test_list_filters_by_state_in_stable_order_and_respects_limit(store):
    first = store.repository.create(request())
    store.clock.advance()
    second = store.repository.create(request())
    store.clock.advance()
    third = store.advance_to(store.repository.create(request()), JobState.CANCELLED)

    def ids(**options):
        return [item.job.job_id for item in store.repository.list_jobs(PROJECT, **options)]

    assert ids() == [first.job_id, second.job_id, third.job_id]
    assert ids(states=(JobState.QUEUED,)) == [first.job_id, second.job_id]
    assert ids(states=(JobState.CANCELLED, JobState.QUEUED), limit=2) == [first.job_id, second.job_id]
    assert ids(states=(JobState.FAILED,)) == [] and ids(states=()) == []


@pytest.mark.parametrize('options,error', [
    ({'limit': 0}, 'invalid_limit'), ({'limit': MAX_LIST_LIMIT + 1}, 'invalid_limit'), ({'limit': True}, 'invalid_limit'),
    ({'states': ('queued',)}, 'unknown_state'), ({'states': [JobState.QUEUED]}, 'unknown_state'),
])
def test_list_rejects_invalid_options(store, options, error):
    with pytest.raises(JobValidationError, match=f'^{error}$'):
        store.repository.list_jobs(PROJECT, **options)


def test_list_rejects_a_foreign_project(store):
    with pytest.raises(JobValidationError, match='^project_mismatch$'):
        store.repository.list_jobs(OTHER_PROJECT)


def test_transition_persists_and_advances_revision_and_time(store):
    job = store.repository.create(request())
    store.clock.advance()
    updated = store.repository.transition(job.job_id, JobState.PREFLIGHT, expected_revision=1)
    assert (updated.revision, updated.updated_at, updated.created_at) == (2, store.clock.now, START)
    assert store.repository.get(job.job_id).job == updated


def test_forbidden_transition_changes_nothing(store):
    job = store.repository.create(request())
    before, row = store.bytes(), store.row(job.job_id)
    store.clock.advance()
    with pytest.raises(JobTransitionError, match='^forbidden_transition$'):
        store.repository.transition(job.job_id, JobState.COMPLETED, expected_revision=1)
    assert store.row(job.job_id) == row and store.bytes() == before


def test_stale_revision_conflicts_and_changes_nothing_even_for_current_state(store):
    job = store.repository.create(request())
    current = store.repository.transition(job.job_id, JobState.PREFLIGHT, expected_revision=1)
    before = store.bytes()
    store.clock.advance()
    for target in (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED):
        with pytest.raises(JobRevisionConflictError, match='^stale_revision$'):
            store.repository.transition(job.job_id, target, expected_revision=1)
    assert store.bytes() == before and store.repository.get(job.job_id).job == current


def test_repeating_the_current_state_with_current_revision_is_a_noop(store):
    job = store.repository.create(request())
    current = store.repository.transition(job.job_id, JobState.PREFLIGHT, expected_revision=1)
    before = store.bytes()
    store.clock.advance()
    assert store.repository.transition(job.job_id, JobState.PREFLIGHT, expected_revision=2) == current
    assert store.bytes() == before


def test_two_connections_cannot_both_win_the_same_revision(tmp_path):
    store = Store(tmp_path)
    job = store.repository.create(request())
    other = store.open()
    store.repository.transition(job.job_id, JobState.PREFLIGHT, expected_revision=1)
    with pytest.raises(JobRevisionConflictError):
        other.transition(job.job_id, JobState.CANCELLED, expected_revision=1)
    assert other.get(job.job_id).job.state is JobState.PREFLIGHT


def test_racing_writers_have_exactly_one_winner(store):
    job = store.repository.create(request())
    barrier = Barrier(2)

    def attempt(target):
        barrier.wait(timeout=5)
        try:
            return store.open().transition(job.job_id, target, expected_revision=1).state
        except JobRevisionConflictError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, (JobState.PREFLIGHT, JobState.CANCELLED)))
    assert sum(result is not None for result in results) == 1
    final = store.repository.get(job.job_id).job
    assert final.revision == 2 and final.state in results


def test_contended_writer_gets_a_typed_busy_error_without_mutation(store):
    job = store.repository.create(request())
    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS, busy_timeout=0.01)
    contended = SqliteJobRepository(runner, store.database, PROJECT, clock=store.clock)
    with closing(sqlite3.connect(store.database, isolation_level=None)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(JobStoreError, match='^busy$'):
            contended.transition(job.job_id, JobState.PREFLIGHT, expected_revision=1)
        blocker.execute('ROLLBACK')
    assert store.repository.get(job.job_id).job == job


def test_live_lease_survives_restart_without_reconciliation(store):
    job = store.advance_to(store.repository.create(request()), JobState.RUNNING)
    leased = store.repository.acquire_lease(job.job_id, MINUTE)
    assert leased.lease.owner_token == f'{1:064x}'
    store.clock.advance(MINUTE - timedelta(microseconds=1))
    snapshot = store.open().get(job.job_id)
    assert snapshot.job == leased and not snapshot.requires_reconciliation


@pytest.mark.parametrize('state', [JobState.QUEUED, JobState.RUNNING, JobState.PAUSED, JobState.VALIDATING])
def test_expired_lease_is_reported_and_reading_changes_nothing(store, state):
    job = store.advance_to(store.repository.create(request()), state)
    leased = store.repository.acquire_lease(job.job_id, MINUTE)
    before = store.bytes()
    store.clock.advance(MINUTE)
    reopened = store.open()
    snapshot = reopened.get(job.job_id)
    listed = reopened.list_jobs(PROJECT)
    assert snapshot.requires_reconciliation and listed[0].requires_reconciliation
    assert snapshot.job == leased and snapshot.job.state is state
    assert store.bytes() == before


def test_expired_lease_blocks_lease_changes_but_not_an_explicit_transition(store):
    job = store.advance_to(store.repository.create(request()), JobState.RUNNING)
    token = store.repository.acquire_lease(job.job_id, MINUTE).lease.owner_token
    store.clock.advance(MINUTE)
    before = store.bytes()
    for change in (lambda: store.repository.acquire_lease(job.job_id, MINUTE),
                   lambda: store.repository.renew_lease(job.job_id, token, MINUTE),
                   lambda: store.repository.release_lease(job.job_id, token)):
        with pytest.raises(JobLeaseError, match='^lease_requires_reconciliation$'):
            change()
    assert store.bytes() == before
    failed = store.repository.transition(job.job_id, JobState.FAILED, expected_revision=job.revision)
    assert failed.lease is None
    assert not store.repository.get(job.job_id).requires_reconciliation


def test_lease_changes_do_not_conflict_with_a_lifecycle_request(store):
    job = store.advance_to(store.repository.create(request()), JobState.RUNNING)
    token = store.repository.acquire_lease(job.job_id, MINUTE).lease.owner_token
    store.clock.advance()
    renewed = store.repository.renew_lease(job.job_id, token, MINUTE)
    assert (renewed.revision, renewed.updated_at) == (job.revision, job.updated_at)
    pausing = store.repository.transition(job.job_id, JobState.PAUSING, expected_revision=job.revision)
    assert pausing.lease == renewed.lease
    released = store.repository.release_lease(job.job_id, token)
    assert released.lease is None and released.revision == pausing.revision


def test_second_owner_is_refused_while_the_lease_is_live(store):
    job = store.repository.create(request())
    store.repository.acquire_lease(job.job_id, MINUTE)
    with pytest.raises(JobLeaseError, match='^lease_held$'):
        store.open().acquire_lease(job.job_id, MINUTE)


def test_continuation_links_durably_and_leaves_the_terminal_record_unchanged(store):
    terminal = store.advance_to(store.repository.create(request()), JobState.FAILED)
    row = store.row(terminal.job_id)
    assert store.repository.get_continuation(terminal.job_id) is None
    store.clock.advance()
    retry = store.repository.create_continuation(terminal.job_id, request(input_fingerprint='sha256:' + 'b' * 64))
    assert (retry.state, retry.revision, retry.continuation_of) == (JobState.QUEUED, 1, terminal.job_id)
    assert retry.job_id != terminal.job_id
    assert store.row(terminal.job_id) == row

    reopened = store.open()
    assert reopened.get(terminal.job_id).job == terminal
    assert reopened.get(retry.job_id).job == retry
    assert reopened.get_continuation(terminal.job_id).job == retry
    assert reopened.get_continuation(retry.job_id) is None


def test_continuation_is_unique_and_requires_a_known_terminal_job(store):
    active = store.repository.create(request())
    terminal = store.advance_to(store.repository.create(request()), JobState.CANCELLED)
    store.repository.create_continuation(terminal.job_id, request())
    before = store.bytes()
    with pytest.raises(JobContinuationError, match='^continuation_exists$'):
        store.repository.create_continuation(terminal.job_id, request())
    with pytest.raises(JobContinuationError, match='^job_not_terminal$'):
        store.repository.create_continuation(active.job_id, request())
    with pytest.raises(JobNotFoundError):
        store.repository.create_continuation(MISSING, request())
    with pytest.raises(JobNotFoundError):
        store.repository.get_continuation(MISSING)
    with pytest.raises(JobValidationError, match='^project_mismatch$'):
        store.repository.create_continuation(terminal.job_id, request(project_id=OTHER_PROJECT))
    assert store.bytes() == before


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
