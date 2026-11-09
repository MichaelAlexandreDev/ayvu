from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from itertools import product
from uuid import UUID

import pytest

from ayvu.job import (
    ALLOWED_TRANSITIONS, MAX_LEASE_DURATION, TERMINAL_STATES, JobContinuationError, JobLease, JobLeaseError,
    JobRequest, JobRevisionConflictError, JobState, JobTerminalSummary, JobTransitionError, JobValidationError,
    TranslationJob, acquire_lease, new_job, release_lease, renew_lease, requires_reconciliation, transition_job,
)

PROJECT = str(UUID(int=1))
JOB = str(UUID(int=2))
FINGERPRINT = 'sha256:' + 'a' * 64
TOKEN = 'b' * 64
OTHER_TOKEN = 'c' * 64
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
MINUTE = timedelta(minutes=1)

# Written out independently of production so a table edit cannot pass unnoticed.
EXPECTED_EDGES = {
    ('queued', 'preflight'), ('queued', 'cancelled'),
    ('preflight', 'running'), ('preflight', 'failed'),
    ('running', 'pausing'), ('running', 'cancelling'), ('running', 'partially_completed'),
    ('running', 'failed'), ('running', 'completed'),
    ('pausing', 'paused'), ('paused', 'queued'), ('cancelling', 'cancelled'),
    ('completed', 'validating'), ('validating', 'validated'), ('validating', 'partially_completed'),
}
EXPECTED_STATES = {
    'queued', 'preflight', 'running', 'pausing', 'paused', 'cancelling', 'cancelled',
    'partially_completed', 'failed', 'completed', 'validating', 'validated',
}
EXPECTED_TERMINAL = {'validated', 'partially_completed', 'failed', 'cancelled'}


def request(**changes):
    return JobRequest(**{'project_id': PROJECT, 'command_type': 'translate_epub', 'command_version': 1,
                         'input_fingerprint': FINGERPRINT, 'output_roles': ('translated_epub',), **changes})


def job_in(state, *, revision=3, lease=None, summary=None):
    return TranslationJob(job_id=JOB, request=request(), state=state, revision=revision, created_at=NOW,
                          updated_at=NOW, lease=lease, terminal_summary=summary)


def test_serialized_states_and_terminal_set_are_exact():
    assert {state.value for state in JobState} == EXPECTED_STATES
    assert {state.value for state in TERMINAL_STATES} == EXPECTED_TERMINAL
    assert set(ALLOWED_TRANSITIONS) == set(JobState)


def test_transition_table_is_exact():
    actual = {(source.value, target.value) for source, targets in ALLOWED_TRANSITIONS.items() for target in targets}
    assert actual == EXPECTED_EDGES


@pytest.mark.parametrize('source,target', sorted(product(JobState, JobState)))
def test_every_edge_is_either_allowed_idempotent_or_forbidden(source, target):
    job = job_in(source)
    if source is target:
        assert transition_job(job, target, expected_revision=3, now=LATER) is job
    elif (source.value, target.value) in EXPECTED_EDGES:
        updated = transition_job(job, target, expected_revision=3, now=LATER)
        assert (updated.state, updated.revision, updated.updated_at) == (target, 4, LATER)
        assert updated.request == job.request and updated.created_at == NOW
    else:
        with pytest.raises(JobTransitionError, match='^forbidden_transition$'):
            transition_job(job, target, expected_revision=3, now=LATER)


@pytest.mark.parametrize('source,target', sorted(product(JobState, JobState)))
def test_stale_revision_conflicts_before_any_edge_is_evaluated(source, target):
    with pytest.raises(JobRevisionConflictError, match='^stale_revision$'):
        transition_job(job_in(source), target, expected_revision=2, now=LATER)


@pytest.mark.parametrize('state', sorted(TERMINAL_STATES))
def test_terminal_states_have_no_exit(state):
    assert ALLOWED_TRANSITIONS[state] == frozenset()
    for target in JobState:
        if target is not state:
            with pytest.raises(JobTransitionError):
                transition_job(job_in(state), target, expected_revision=3, now=LATER)


def test_new_job_starts_queued_at_revision_one():
    job = new_job(request(), JOB, NOW)
    assert (job.state, job.revision, job.created_at, job.updated_at) == (JobState.QUEUED, 1, NOW, NOW)
    assert job.lease is None and job.terminal_summary is None and job.continuation_of is None


def test_transition_never_moves_recorded_time_backwards():
    updated = transition_job(job_in(JobState.QUEUED), JobState.PREFLIGHT, expected_revision=3,
                             now=NOW - timedelta(days=1))
    assert updated.updated_at == NOW


def test_terminal_transition_keeps_summary_and_clears_lease():
    lease = JobLease(TOKEN, NOW, LATER)
    summary = JobTerminalSummary('provider_unavailable', (('documents_done', 2),))
    updated = transition_job(job_in(JobState.RUNNING, lease=lease), JobState.FAILED, expected_revision=3,
                             now=LATER, summary=summary)
    assert updated.lease is None and updated.terminal_summary == summary


def test_summary_is_rejected_on_nonterminal_transition():
    with pytest.raises(JobValidationError, match='^summary_requires_terminal_state$'):
        transition_job(job_in(JobState.QUEUED), JobState.PREFLIGHT, expected_revision=3, now=LATER,
                       summary=JobTerminalSummary('done'))


def test_values_are_frozen():
    job = job_in(JobState.QUEUED)
    with pytest.raises(FrozenInstanceError):
        job.state = JobState.RUNNING
    with pytest.raises(FrozenInstanceError):
        job.request.input_fingerprint = 'sha256:' + 'f' * 64


@pytest.mark.parametrize('transition_target', ['running', None, 1])
def test_unknown_target_is_rejected(transition_target):
    with pytest.raises(JobValidationError, match='^unknown_state$'):
        transition_job(job_in(JobState.QUEUED), transition_target, expected_revision=3, now=LATER)


@pytest.mark.parametrize('revision', [0, -1, True, '3', 2**63])
def test_invalid_expected_revision_is_rejected(revision):
    with pytest.raises(JobValidationError, match='^invalid_revision$'):
        transition_job(job_in(JobState.QUEUED), JobState.PREFLIGHT, expected_revision=revision, now=LATER)


def test_acquire_sets_lease_without_touching_lifecycle_history():
    job = job_in(JobState.QUEUED)
    updated = acquire_lease(job, TOKEN, LATER, MINUTE)
    assert updated.lease == JobLease(TOKEN, LATER, LATER + MINUTE)
    assert (updated.revision, updated.updated_at, updated.state) == (job.revision, job.updated_at, job.state)


def test_live_lease_blocks_another_owner():
    job = acquire_lease(job_in(JobState.QUEUED), TOKEN, NOW, MINUTE)
    with pytest.raises(JobLeaseError, match='^lease_held$'):
        acquire_lease(job, OTHER_TOKEN, NOW, MINUTE)


def test_expired_lease_cannot_be_silently_taken_over_renewed_or_released():
    job = acquire_lease(job_in(JobState.RUNNING), TOKEN, NOW, MINUTE)
    expired = NOW + MINUTE
    with pytest.raises(JobLeaseError, match='^lease_requires_reconciliation$'):
        acquire_lease(job, OTHER_TOKEN, expired, MINUTE)
    with pytest.raises(JobLeaseError, match='^lease_requires_reconciliation$'):
        renew_lease(job, TOKEN, expired, MINUTE)
    with pytest.raises(JobLeaseError, match='^lease_requires_reconciliation$'):
        release_lease(job, TOKEN, expired)


def test_expired_lease_does_not_block_a_lifecycle_exit():
    job = acquire_lease(job_in(JobState.RUNNING), TOKEN, NOW, MINUTE)
    assert transition_job(job, JobState.FAILED, expected_revision=3, now=NOW + MINUTE).lease is None


def test_renew_extends_and_release_clears_only_for_the_owner():
    job = acquire_lease(job_in(JobState.RUNNING), TOKEN, NOW, MINUTE)
    renewed = renew_lease(job, TOKEN, NOW + timedelta(seconds=30), MINUTE)
    assert renewed.lease.expires_at == NOW + timedelta(seconds=90)
    assert renewed.lease.acquired_at == NOW and renewed.revision == job.revision
    assert release_lease(renewed, TOKEN, NOW + timedelta(seconds=31)).lease is None
    for change in (lambda: renew_lease(job, OTHER_TOKEN, NOW, MINUTE), lambda: release_lease(job, OTHER_TOKEN, NOW),
                   lambda: renew_lease(job_in(JobState.RUNNING), TOKEN, NOW, MINUTE),
                   lambda: release_lease(job_in(JobState.RUNNING), TOKEN, NOW)):
        with pytest.raises(JobLeaseError, match='^lease_not_held$'):
            change()


def test_terminal_job_cannot_acquire_a_lease():
    with pytest.raises(JobLeaseError, match='^job_terminal$'):
        acquire_lease(job_in(JobState.FAILED), TOKEN, NOW, MINUTE)


@pytest.mark.parametrize('duration', [timedelta(0), timedelta(seconds=-1), MAX_LEASE_DURATION + timedelta(seconds=1), 60])
def test_lease_duration_is_bounded(duration):
    with pytest.raises(JobLeaseError, match='^invalid_lease_duration$'):
        acquire_lease(job_in(JobState.QUEUED), TOKEN, NOW, duration)


def test_reconciliation_starts_exactly_at_expiry_and_ignores_terminal_or_unleased_jobs():
    job = acquire_lease(job_in(JobState.RUNNING), TOKEN, NOW, MINUTE)
    assert not requires_reconciliation(job, NOW + MINUTE - timedelta(microseconds=1))
    assert requires_reconciliation(job, NOW + MINUTE)
    assert not requires_reconciliation(job_in(JobState.RUNNING), LATER)
    assert not requires_reconciliation(job_in(JobState.FAILED), LATER)


@pytest.mark.parametrize('changes,error', [
    ({'project_id': 'not-a-uuid'}, 'invalid_job_identity'),
    ({'project_id': str(UUID(int=0xabc)).upper()}, 'invalid_job_identity'),
    ({'command_type': 'Translate EPUB'}, 'invalid_command_type'),
    ({'command_version': 0}, 'invalid_command_version'),
    ({'command_version': True}, 'invalid_command_version'),
    ({'input_fingerprint': 'sha256:short'}, 'invalid_fingerprint'),
    ({'input_fingerprint': '/home/user/private-canary.epub'}, 'invalid_fingerprint'),
    ({'output_roles': ['translated_epub']}, 'invalid_output_roles'),
    ({'output_roles': ('a', 'a')}, 'invalid_output_roles'),
    ({'output_roles': ('../escape',)}, 'invalid_output_roles'),
    ({'output_roles': tuple(f'r{i}' for i in range(17))}, 'invalid_output_roles'),
])
def test_invalid_requests_fail_with_bounded_codes(changes, error):
    with pytest.raises(JobValidationError, match=f'^{error}$'):
        request(**changes)


@pytest.mark.parametrize('reason,counters', [
    ('Private canary text', ()), ('/home/user/book.epub', ()), ('', ()), (None, ()),
    ('ok', [('a', 1)]), ('ok', (('a', -1),)), ('ok', (('a', True),)), ('ok', (('a', 1), ('a', 2))),
    ('ok', (('bad key', 1),)), ('ok', tuple((f'k{i}', i) for i in range(17))),
])
def test_terminal_summary_only_accepts_allowlisted_shapes(reason, counters):
    with pytest.raises(JobValidationError, match='^invalid_terminal_summary$'):
        JobTerminalSummary(reason, counters)


def test_lease_values_are_validated():
    for token in ('short', 'B' * 64, None):
        with pytest.raises(JobValidationError, match='^invalid_owner_token$'):
            JobLease(token, NOW, LATER)
    with pytest.raises(JobValidationError, match='^invalid_lease$'):
        JobLease(TOKEN, LATER, NOW)


@pytest.mark.parametrize('stamp', [datetime(2026, 11, 3), datetime(2026, 11, 3, tzinfo=timezone(timedelta(hours=1))), 'now'])
def test_timestamps_must_be_utc(stamp):
    with pytest.raises(JobValidationError, match='^invalid_timestamp$'):
        new_job(request(), JOB, stamp)


def test_job_invariants_are_enforced_on_construction():
    with pytest.raises(JobValidationError, match='^terminal_job_has_lease$'):
        job_in(JobState.FAILED, lease=JobLease(TOKEN, NOW, LATER))
    with pytest.raises(JobValidationError, match='^summary_requires_terminal_state$'):
        job_in(JobState.RUNNING, summary=JobTerminalSummary('done'))
    with pytest.raises(JobContinuationError, match='^self_continuation$'):
        new_job(request(), JOB, NOW, continuation_of=JOB)
    with pytest.raises(JobValidationError, match='^invalid_timestamp_order$'):
        replace(job_in(JobState.QUEUED), updated_at=NOW - MINUTE)


def test_repr_reveals_neither_token_nor_fingerprint_nor_counters():
    job = acquire_lease(job_in(JobState.RUNNING), TOKEN, NOW, MINUTE)
    summary = JobTerminalSummary('done', (('secret_counter', 7),))
    text = ' '.join(map(repr, (job, job.request, job.lease, summary)))
    assert TOKEN not in text and FINGERPRINT not in text and 'secret_counter' not in text


def test_every_nonterminal_state_can_reach_a_terminal_outcome():
    for state in JobState:
        seen, frontier = {state}, [state]
        while frontier:
            for target in ALLOWED_TRANSITIONS[frontier.pop()] - seen:
                seen.add(target)
                frontier.append(target)
        assert state in TERMINAL_STATES or seen & TERMINAL_STATES


def test_repeating_a_terminal_state_keeps_the_recorded_summary():
    job = job_in(JobState.FAILED, summary=JobTerminalSummary('first'))
    repeated = transition_job(job, JobState.FAILED, expected_revision=3, now=LATER, summary=JobTerminalSummary('second'))
    assert repeated is job and repeated.terminal_summary.reason_code == 'first'
