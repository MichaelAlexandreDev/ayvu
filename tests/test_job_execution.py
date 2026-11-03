from concurrent.futures import ThreadPoolExecutor
import ast
from datetime import UTC, datetime
import logging
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from ayvu.job import ALLOWED_TRANSITIONS, JobRequest, JobState, JobTerminalSummary, new_job
from ayvu.job_events import (
    CancellationRequest, JobCorrelation, JobEvent, JobEventAcceptance, JobEventError, JobEventKind, JobEventOrderError,
    JobFailure, JobFailureFamily, JobOperationError, JobOutcome, JobProgress, JobWarning, encode_event,
)
from ayvu.job_execution import (
    CancellationSource, CancellationStatus, InMemoryJobEventLog, JobCancellationError,
)
from job_contract_fakes import SAFE_POINTS, FakeOperation, OutcomeAudit, ProgressView

JOB = str(UUID(int=2))
PROJECT = str(UUID(int=3))
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
VALIDATED = JobOutcome(JobState.VALIDATED, JobTerminalSummary('validated'))
CANCELLED = JobOutcome(JobState.CANCELLED, JobTerminalSummary('user_requested'))
FAILED = JobOutcome(JobState.FAILED, JobTerminalSummary('timeout'), JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout'))


def job():
    return new_job(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'a' * 64), JOB, NOW)


def event(sequence, payload=None, event_id=None):
    return JobEvent(event_id or str(UUID(int=1000 + sequence)), JOB, sequence, NOW, JobCorrelation(PROJECT),
                    payload or JobWarning('w'))


def harness(**options):
    log = InMemoryJobEventLog(JOB)
    source = CancellationSource(SAFE_POINTS)
    return log, source, FakeOperation(job(), log, source, **options)


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


def test_repeated_request_is_idempotent_and_keeps_the_first_reason_and_time():
    source = CancellationSource(SAFE_POINTS)
    first = source.request('user_requested', NOW)
    later = datetime(2026, 11, 4, tzinfo=UTC)
    second = source.request('shutdown', later)
    assert first.status is CancellationStatus.ACCEPTED
    assert second.status is CancellationStatus.ALREADY_REQUESTED
    assert second.request == first.request == CancellationRequest('user_requested', NOW)
    assert source.requested == first.request


def test_request_is_invisible_until_a_declared_safe_point():
    source = CancellationSource(SAFE_POINTS)
    token = source.token
    assert token.checkpoint('before_document') is None
    source.request('user_requested', NOW)
    assert source.acknowledgement is None
    public = {name for name in dir(token) if not name.startswith('_')}
    assert public == {'checkpoint'}
    acknowledgement = token.checkpoint('after_document')
    assert (acknowledgement.reason_code, acknowledgement.safe_point) == ('user_requested', 'after_document')
    assert token.checkpoint('before_document') is acknowledgement
    assert source.acknowledgement is acknowledgement


@pytest.mark.parametrize('point', ['inside_http_call', '', None, 7])
def test_undeclared_safe_point_is_an_error_even_with_no_request(point):
    with pytest.raises(JobCancellationError, match='^undeclared_safe_point$'):
        CancellationSource(SAFE_POINTS).token.checkpoint(point)


@pytest.mark.parametrize('points', [(), ['a'], ('a', 'a'), ('Bad Name',), 'abc'])
def test_safe_points_must_be_declared_as_unique_slugs(points):
    with pytest.raises(JobCancellationError, match='^invalid_safe_points$'):
        CancellationSource(points)


def test_request_after_the_outcome_is_too_late_and_rewrites_nothing():
    source = CancellationSource(SAFE_POINTS)
    source.settle(VALIDATED)
    result = source.request('user_requested', NOW)
    assert result.status is CancellationStatus.TOO_LATE and result.request is None
    assert source.requested is None and source.outcome is VALIDATED
    assert source.token.checkpoint('before_document') is None


def test_too_late_after_an_unobserved_request_reports_the_original_request():
    source = CancellationSource(SAFE_POINTS)
    first = source.request('user_requested', NOW).request
    source.settle(VALIDATED)
    result = source.request('shutdown', NOW)
    assert result.status is CancellationStatus.TOO_LATE and result.request is first
    assert source.outcome is VALIDATED


def test_only_the_first_settlement_counts_and_exactly_one_caller_is_told_to_emit():
    source = CancellationSource(SAFE_POINTS)
    first = source.settle(VALIDATED)
    second = source.settle(FAILED)
    assert first.first and not second.first
    assert first.outcome is second.outcome is VALIDATED


def test_cancelled_outcome_requires_a_request():
    source = CancellationSource(SAFE_POINTS)
    with pytest.raises(JobCancellationError, match='^cancellation_not_requested$'):
        source.settle(CANCELLED)
    assert source.outcome is None


@pytest.mark.parametrize('outcome', [VALIDATED, FAILED])
def test_acknowledged_cancellation_cannot_settle_as_anything_else(outcome):
    source = CancellationSource(SAFE_POINTS)
    source.request('user_requested', NOW)
    source.token.checkpoint('before_document')
    with pytest.raises(JobCancellationError, match='^outcome_contradicts_cancellation$'):
        source.settle(outcome)
    assert source.outcome is None
    assert source.settle(CANCELLED).first


def test_settle_rejects_values_that_are_not_outcomes():
    with pytest.raises(JobEventError, match='^invalid_outcome$'):
        CancellationSource(SAFE_POINTS).settle('validated')


def test_invalid_reason_is_rejected_without_recording_a_request():
    source = CancellationSource(SAFE_POINTS)
    with pytest.raises(JobEventError):
        source.request('Private Canary Text', NOW)
    assert source.requested is None


def test_uninterrupted_operation_validates_and_both_consumers_agree():
    log, source, operation = harness()
    outcome = operation.run()
    assert outcome.state is JobState.VALIDATED and outcome.matches(operation.job)
    view, audit = ProgressView(log), OutcomeAudit(log)
    view.pull()
    assert audit.outcome() == outcome == log.outcome == source.outcome
    assert audit.sequences() == list(range(1, len(log.replay()) + 1))
    assert view.position == audit.sequences()[-1]
    assert view.latest['translate'].completed == view.latest['translate'].total == 3
    assert audit.warnings() == ['glossary_term_skipped']


def test_consumers_pull_at_their_own_pace_and_see_the_same_stream():
    log, _, operation = harness()
    seen = []
    view = ProgressView(log)

    def pull_midway(safe_point, document):
        view.pull()
        seen.append(view.position)

    operation.on_safe_point = pull_midway
    outcome = operation.run()
    view.pull()
    audit = OutcomeAudit(log)
    assert seen == sorted(seen) and seen[-1] < audit.sequences()[-1]
    assert view.position == audit.sequences()[-1] and audit.outcome() == outcome
    # A late consumer reconstructs exactly what an early one saw.
    late = ProgressView(log)
    late.pull()
    assert late.latest == view.latest


def test_failing_consumer_does_not_disturb_the_producer_or_another_consumer():
    log, _, operation = harness()

    class Broken(ProgressView):
        def pull(self):
            raise RuntimeError('consumer failure')

    broken = Broken(log)
    with pytest.raises(RuntimeError):
        broken.pull()
    assert operation.run().state is JobState.VALIDATED
    assert OutcomeAudit(log).outcome().state is JobState.VALIDATED


@pytest.mark.parametrize('stop_point,stop_document,done', [
    ('before_document', 0, 0), ('after_document', 0, 1),
    ('before_document', 1, 1), ('after_document', 1, 2),
    ('before_document', 2, 2), ('after_document', 2, 3),
])
def test_operation_stops_only_at_the_safe_point_after_the_request(stop_point, stop_document, done):
    def request_there(safe_point, document):
        if (safe_point, document) == (stop_point, stop_document):
            source.request('user_requested', NOW)

    log, source, operation = harness(on_safe_point=request_there)
    outcome = operation.run()
    assert operation.stopped_at == (stop_point, stop_document) and operation.documents_done == done
    assert outcome.state is JobState.CANCELLED and outcome.matches(operation.job)
    kinds = [item.kind for item in log.replay()]
    assert kinds[-3:] == [JobEventKind.CANCELLATION_REQUESTED, JobEventKind.CANCELLATION_ACKNOWLEDGED, JobEventKind.OUTCOME]
    assert OutcomeAudit(log).outcome() == outcome


def test_request_during_work_is_not_observed_before_the_next_safe_point():
    observed = []

    def request_then_look(safe_point, document):
        if (safe_point, document) == ('before_document', 1):
            source.request('user_requested', NOW)
            observed.append(source.acknowledgement)

    _, source, operation = harness(on_safe_point=request_then_look)
    operation.run()
    assert observed == [None] and source.acknowledgement.safe_point == 'before_document'


def test_request_after_the_last_safe_point_leaves_the_real_outcome():
    log, source, operation = harness()
    original = source.settle

    def request_just_before_settling(outcome):
        source.request('user_requested', NOW)
        return original(outcome)

    source.settle = request_just_before_settling
    outcome = operation.run()
    assert outcome.state is JobState.VALIDATED and outcome.matches(operation.job)
    assert source.acknowledgement is None
    assert source.request('again', NOW).status is CancellationStatus.TOO_LATE
    assert [item.kind for item in log.replay()].count(JobEventKind.OUTCOME) == 1
    assert JobEventKind.CANCELLATION_REQUESTED not in [item.kind for item in log.replay()]


@pytest.mark.parametrize('attempt', range(25))
def test_racing_request_and_settlement_always_end_consistently(attempt):
    source = CancellationSource(SAFE_POINTS)
    barrier = Barrier(2)

    def request():
        barrier.wait(timeout=5)
        return source.request('user_requested', NOW)

    def settle():
        barrier.wait(timeout=5)
        return source.settle(VALIDATED)

    with ThreadPoolExecutor(2) as pool:
        requested, settled = pool.submit(request), pool.submit(settle)
        result, settlement = requested.result(timeout=5), settled.result(timeout=5)
    assert settlement.first and source.outcome is VALIDATED
    assert source.token.checkpoint('before_document') is None
    if result.status is CancellationStatus.TOO_LATE:
        assert source.requested is None and result.request is None
    else:
        assert result.status is CancellationStatus.ACCEPTED and source.requested is result.request


@pytest.mark.parametrize('attempt', range(25))
def test_racing_safe_point_and_settlement_never_contradict(attempt):
    source = CancellationSource(SAFE_POINTS)
    source.request('user_requested', NOW)
    barrier = Barrier(2)

    def observe():
        barrier.wait(timeout=5)
        return source.token.checkpoint('before_document')

    def settle():
        barrier.wait(timeout=5)
        try:
            return source.settle(VALIDATED)
        except JobCancellationError:
            return None

    with ThreadPoolExecutor(2) as pool:
        observed, settled = pool.submit(observe), pool.submit(settle)
        acknowledgement, settlement = observed.result(timeout=5), settled.result(timeout=5)
    # Exactly one side won: the operation stopped, or the outcome stood.
    assert (acknowledgement is None) == (settlement is not None)
    if settlement is None:
        assert source.outcome is None and source.settle(CANCELLED).first
    else:
        assert source.outcome is VALIDATED and source.acknowledgement is None


def test_racing_settlements_produce_one_terminal_event():
    log = InMemoryJobEventLog(JOB)
    source = CancellationSource(SAFE_POINTS)
    barrier = Barrier(4)

    def finish(index):
        barrier.wait(timeout=5)
        outcome = (VALIDATED, FAILED)[index % 2]
        settlement = source.settle(outcome)
        if settlement.first:
            log.emit(event(1, settlement.outcome))
        return settlement

    with ThreadPoolExecutor(4) as pool:
        settlements = list(pool.map(finish, range(4)))
    assert sum(item.first for item in settlements) == 1
    assert len({id(item.outcome) for item in settlements}) == 1
    assert OutcomeAudit(log).outcome() is source.outcome


def test_failure_becomes_a_consistent_failed_outcome_with_a_normalized_error():
    envelope = JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout', True)
    log, _, operation = harness(fail_at=1, error=JobOperationError(envelope))
    outcome = operation.run()
    assert outcome.state is JobState.FAILED and outcome.failure is envelope and outcome.matches(operation.job)
    assert OutcomeAudit(log).failure() == envelope


CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub',
            'Translate this prompt', 'https://user:hunter2@example.test/translate', 'sk-secret-token')


@pytest.mark.parametrize('canary', CANARIES)
def test_canaries_never_reach_events_repr_outcome_or_logs(canary, caplog):
    caplog.set_level(logging.DEBUG)
    log, source, operation = harness(fail_at=1, error=RuntimeError(canary))
    outcome = operation.run()
    assert outcome.failure.code == 'unexpected_error'
    texts = [encode_event(item) for item in log.replay()]
    texts += [repr(item) for item in log.replay()]
    texts += [repr(value) for value in (outcome, outcome.failure, log, source, source.token, operation.job)]
    texts.append(caplog.text)
    assert not any(canary in text for text in texts)


def test_late_request_never_changes_a_recorded_failure():
    log, source, operation = harness(fail_at=0, error=RuntimeError('boom'))
    outcome = operation.run()
    assert source.request('user_requested', NOW).status is CancellationStatus.TOO_LATE
    assert OutcomeAudit(log).outcome() == outcome and operation.job.state is JobState.FAILED


def test_nonterminal_recovery_condition_has_no_outcome_and_blocks_nothing():
    log = InMemoryJobEventLog(JOB)
    log.emit(event(1, JobProgress('translate', 'documents', 1, 3)))
    assert log.outcome is None and OutcomeAudit(log).outcome() is None


def test_contract_modules_import_no_presentation_or_infrastructure():
    root = Path(__file__).parents[1] / 'src' / 'ayvu'
    forbidden = {'typer', 'rich', 'requests', 'sqlite3', 'PySide6', 'PyQt6', 'bs4', 'ebooklib', 'lxml', 'os', 'pathlib'}
    for name in ('job_events.py', 'job_execution.py'):
        tree = ast.parse((root / name).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots.add(node.module.split('.')[0])
        assert not roots & forbidden, name


@pytest.mark.parametrize('state,route', [
    (JobState.QUEUED, (JobState.CANCELLED,)),
    (JobState.PREFLIGHT, (JobState.RUNNING, JobState.CANCELLING, JobState.CANCELLED)),
    (JobState.RUNNING, (JobState.CANCELLING, JobState.CANCELLED)),
    (JobState.CANCELLING, (JobState.CANCELLED,)),
    (JobState.PAUSING, (JobState.PAUSED, JobState.QUEUED, JobState.CANCELLED)),
    (JobState.PAUSED, (JobState.QUEUED, JobState.CANCELLED)),
])
def test_documented_cancellation_routes_exist_in_the_transition_table(state, route):
    for source, target in zip((state, *route), route):
        assert target in ALLOWED_TRANSITIONS[source]


@pytest.mark.parametrize('state', [JobState.COMPLETED, JobState.VALIDATING])
def test_completed_work_has_no_route_to_cancelled(state):
    seen, frontier = {state}, [state]
    while frontier:
        for target in ALLOWED_TRANSITIONS[frontier.pop()] - seen:
            seen.add(target)
            frontier.append(target)
    assert JobState.CANCELLED not in seen
