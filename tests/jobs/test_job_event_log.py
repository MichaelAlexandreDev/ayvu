from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ayvu.jobs.events import JobEventAcceptance, JobEventOrderError
from ayvu.jobs.execution import InMemoryJobEventLog
from ayvu.kernel import JobEventError

from tests.jobs.job_execution_support import JOB
from tests.jobs.job_execution_support import event


def test_log_applies_rules_and_replays_from_any_position():
    log = InMemoryJobEventLog(JOB)
    assert log.emit(event(1)) is JobEventAcceptance.APPLIED
    assert log.emit(event(2)) is JobEventAcceptance.APPLIED
    assert log.emit(event(1)) is JobEventAcceptance.DUPLICATE
    assert [item.sequence for item in log.replay()] == [1, 2]
    assert [item.sequence for item in log.replay(1)] == [2]
    assert log.replay(2) == () and log.replay(99) == ()
    with pytest.raises(JobEventOrderError, match='^sequence_gap$'):
        log.emit(event(4))
    assert [item.sequence for item in log.replay()] == [1, 2]


@pytest.mark.parametrize('position', [-1, True, '1', None])
def test_replay_rejects_invalid_positions(position):
    with pytest.raises(JobEventError, match='^invalid_sequence$'):
        InMemoryJobEventLog(JOB).replay(position)


def test_full_log_fails_explicitly_and_never_drops_an_event():
    log = InMemoryJobEventLog(JOB, capacity=2)
    log.emit(event(1))
    log.emit(event(2))
    with pytest.raises(JobEventOrderError, match='^event_log_full$'):
        log.emit(event(3))
    assert log.emit(event(2)) is JobEventAcceptance.DUPLICATE
    assert [item.sequence for item in log.replay()] == [1, 2]


@pytest.mark.parametrize('capacity', [0, -1, True, '1'])
def test_log_capacity_is_validated(capacity):
    with pytest.raises(JobEventError, match='^invalid_capacity$'):
        InMemoryJobEventLog(JOB, capacity=capacity)


def test_emit_rejects_values_that_are_not_events():
    with pytest.raises(JobEventError, match='^invalid_event$'):
        InMemoryJobEventLog(JOB).emit('text')


def test_replay_returns_a_snapshot_the_consumer_cannot_alter():
    log = InMemoryJobEventLog(JOB)
    log.emit(event(1))
    snapshot = log.replay()
    log.emit(event(2))
    assert isinstance(snapshot, tuple) and len(snapshot) == 1 and len(log.replay()) == 2


def test_concurrent_identical_emits_apply_once():
    log = InMemoryJobEventLog(JOB)
    barrier = Barrier(8)

    def emit(_):
        barrier.wait(timeout=5)
        return log.emit(event(1))

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(emit, range(8)))
    assert results.count(JobEventAcceptance.APPLIED) == 1
    assert results.count(JobEventAcceptance.DUPLICATE) == 7
    assert len(log.replay()) == 1
