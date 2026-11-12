from datetime import UTC, datetime

import pytest

from ayvu.jobs.events import CancellationRequest
from ayvu.jobs.execution import CancellationSource, CancellationStatus
from tests.support.job_contract_fakes import SAFE_POINTS
from ayvu.kernel import JobEventError, JobCancellationError

from tests.jobs.job_execution_support import CANCELLED, FAILED, NOW
from tests.jobs.job_execution_support import VALIDATED


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
