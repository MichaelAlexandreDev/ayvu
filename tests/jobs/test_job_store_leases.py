from datetime import timedelta

import pytest

from ayvu.jobs.model import JobContinuationError, JobLeaseError, JobNotFoundError, JobState
from ayvu.kernel import JobValidationError

from tests.jobs.job_store_support import MINUTE, MISSING, OTHER_PROJECT, PROJECT
from tests.jobs.job_store_support import request, store


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
