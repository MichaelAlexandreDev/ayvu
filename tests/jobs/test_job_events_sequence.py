from datetime import timedelta
from uuid import UUID

import pytest

from ayvu.jobs.model import JobState
from ayvu.jobs.events import CancellationAcknowledgement, CancellationRequest, JobEventAcceptance, JobEventOrderError, JobEventSequence, JobProgress, decode_event, encode_event
from ayvu.kernel import JobWarning

from tests.jobs.job_events_support import JOB, NOW, OUTCOMES
from tests.jobs.job_events_support import event


def test_sequence_accepts_contiguous_events_and_tracks_position():
    sequence = JobEventSequence(JOB)
    for number in (1, 2, 3):
        assert sequence.accept(event(number)) is JobEventAcceptance.APPLIED
    assert sequence.last_sequence == 3


def test_replay_of_an_identical_event_is_idempotent():
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1))
    sequence.accept(event(2))
    assert sequence.accept(event(1)) is JobEventAcceptance.DUPLICATE
    assert sequence.accept(decode_event(encode_event(event(2)))) is JobEventAcceptance.DUPLICATE
    assert sequence.last_sequence == 2


@pytest.mark.parametrize('conflict', [
    event(1, at=NOW + timedelta(seconds=1)),
    event(2, event_id=str(UUID(int=1001))),
    event(1, JobWarning('other'), event_id=str(UUID(int=1001))),
    event(1, JobProgress('translate', 'documents', 1.0), event_id=str(UUID(int=1001))),
], ids=['time', 'sequence', 'payload', 'number-type'])
def test_conflicting_duplicate_is_explicit(conflict):
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1))
    with pytest.raises(JobEventOrderError, match='^conflicting_duplicate$'):
        sequence.accept(conflict)
    assert sequence.last_sequence == 1


@pytest.mark.parametrize('first,second,error', [
    (None, event(2), 'sequence_gap'),
    (event(1), event(3), 'sequence_gap'),
    (event(1), event(1, event_id=str(UUID(int=77))), 'sequence_regression'),
    (event(1), event(2, job_id=str(UUID(int=9))), 'foreign_job'),
    (event(1), event(2, JobWarning('w'), project=str(UUID(int=9))), 'correlation_changed'),
])
def test_ordering_violations_are_typed_and_change_nothing(first, second, error):
    sequence = JobEventSequence(JOB)
    if first is not None:
        sequence.accept(first)
    position = sequence.last_sequence
    with pytest.raises(JobEventOrderError, match=f'^{error}$'):
        sequence.accept(second)
    assert sequence.last_sequence == position and second.event_id not in sequence


def test_progress_is_monotonic_within_a_phase():
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1, JobProgress('translate', 'documents', 2, 5)))
    for payload in (JobProgress('translate', 'documents', 1, 5), JobProgress('translate', 'documents', 3)):
        with pytest.raises(JobEventOrderError, match='^progress_regression$'):
            sequence.accept(event(2, payload))
    with pytest.raises(JobEventOrderError, match='^progress_unit_changed$'):
        sequence.accept(event(2, JobProgress('translate', 'units', 3, 5)))
    assert sequence.accept(event(2, JobProgress('translate', 'documents', 2, 9))) is JobEventAcceptance.APPLIED


def test_unknown_total_may_become_known_and_equal_progress_is_allowed():
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1, JobProgress('translate', 'documents', 2)))
    sequence.accept(event(2, JobProgress('translate', 'documents', 2, 4)))
    assert sequence.last_sequence == 2


def test_new_phase_restarts_progress_but_finished_phase_cannot_return():
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1, JobProgress('translate', 'documents', 5, 5)))
    sequence.accept(event(2, JobProgress('validate', 'checks', 0, 2)))
    with pytest.raises(JobEventOrderError, match='^phase_regression$'):
        sequence.accept(event(3, JobProgress('translate', 'documents', 5, 5)))
    assert sequence.accept(event(3, JobWarning('w'))) is JobEventAcceptance.APPLIED
    assert sequence.accept(event(4, JobProgress('validate', 'checks', 1, 2))) is JobEventAcceptance.APPLIED


def test_nothing_follows_the_terminal_event_but_its_replay_is_idempotent():
    sequence = JobEventSequence(JOB)
    final = event(1, OUTCOMES[JobState.VALIDATED])
    sequence.accept(final)
    assert sequence.outcome is OUTCOMES[JobState.VALIDATED]
    assert sequence.accept(final) is JobEventAcceptance.DUPLICATE
    for later in (event(2, OUTCOMES[JobState.FAILED]), event(2, JobWarning('late')),
                  event(2, CancellationRequest('late', NOW))):
        with pytest.raises(JobEventOrderError, match='^event_after_terminal$'):
            sequence.accept(later)
    assert sequence.outcome is OUTCOMES[JobState.VALIDATED]


def test_cancellation_events_must_be_ordered_and_unique():
    request = CancellationRequest('user_requested', NOW)
    acknowledgement = CancellationAcknowledgement('user_requested', 'between_documents')
    sequence = JobEventSequence(JOB)
    with pytest.raises(JobEventOrderError, match='^cancellation_not_requested$'):
        sequence.accept(event(1, acknowledgement))
    with pytest.raises(JobEventOrderError, match='^cancellation_not_requested$'):
        sequence.accept(event(1, OUTCOMES[JobState.CANCELLED]))
    sequence.accept(event(1, request))
    with pytest.raises(JobEventOrderError, match='^duplicate_cancellation$'):
        sequence.accept(event(2, request))
    sequence.accept(event(2, acknowledgement))
    with pytest.raises(JobEventOrderError, match='^duplicate_cancellation$'):
        sequence.accept(event(3, acknowledgement))
    assert sequence.accept(event(3, OUTCOMES[JobState.CANCELLED])) is JobEventAcceptance.APPLIED


@pytest.mark.parametrize('state', [JobState.VALIDATED, JobState.FAILED, JobState.PARTIALLY_COMPLETED])
def test_acknowledged_cancellation_only_ends_as_cancelled(state):
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1, CancellationRequest('user_requested', NOW)))
    sequence.accept(event(2, CancellationAcknowledgement('user_requested', 'between_documents')))
    with pytest.raises(JobEventOrderError, match='^outcome_contradicts_cancellation$'):
        sequence.accept(event(3, OUTCOMES[state]))
    assert sequence.outcome is None


def test_unacknowledged_request_may_still_end_with_the_real_outcome():
    sequence = JobEventSequence(JOB)
    sequence.accept(event(1, CancellationRequest('user_requested', NOW)))
    assert sequence.accept(event(2, OUTCOMES[JobState.VALIDATED])) is JobEventAcceptance.APPLIED
