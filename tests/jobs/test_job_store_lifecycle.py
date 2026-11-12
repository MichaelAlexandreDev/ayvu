from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier
from uuid import UUID

import pytest

from ayvu.jobs.model import JobNotFoundError, JobRevisionConflictError, JobState, JobStoreError, JobTerminalSummary, JobTransitionError
from ayvu.jobs.store import MAX_LIST_LIMIT, SqliteJobRepository
from ayvu.projects.schema import PROJECT_MIGRATIONS
from ayvu.kernel import JobValidationError
from ayvu.kernel.sqlite import SqliteMigrationRunner

from tests.jobs.job_store_support import MINUTE, MISSING, OTHER_PROJECT, PROJECT, START
from tests.jobs.job_store_support import Store, request, store


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
