
import pytest

from ayvu.jobs.model import JobRequest, JobState, JobTerminalSummary, new_job, transition_job
from ayvu.jobs.events import MAX_EVENT_BYTES, JobOutcome, JobProgress, encode_event
from ayvu.kernel import DIAGNOSTIC_KEYS, JobEventError, JobFailure, JobFailureFamily, JobWarning, job_diagnostics

from tests.jobs.job_events_support import JOB, NOW, OUTCOMES
from tests.jobs.job_events_support import PROJECT, event


@pytest.mark.parametrize('completed,total,error', [
    (float('nan'), None, 'invalid_progress_metric'), (float('inf'), None, 'invalid_progress_metric'),
    (-1, None, 'invalid_progress_metric'), (-0.0, None, 'invalid_progress_metric'),
    (True, None, 'invalid_progress_metric'), ('1', None, 'invalid_progress_metric'),
    (2**63, None, 'invalid_progress_metric'), (0, float('nan'), 'invalid_progress_metric'),
    (0, -1, 'invalid_progress_metric'), (0, False, 'invalid_progress_metric'),
    (4, 3, 'progress_exceeds_total'), (3.5, 3, 'progress_exceeds_total'),
])
def test_progress_metrics_are_bounded(completed, total, error):
    with pytest.raises(JobEventError, match=f'^{error}$'):
        JobProgress('translate', 'documents', completed, total)


def test_indeterminate_and_complete_progress_are_valid():
    assert JobProgress('translate', 'documents', 5).total is None
    assert JobProgress('translate', 'documents', 3, 3).completed == 3
    assert JobProgress('translate', 'documents', 0, 0).total == 0


@pytest.mark.parametrize('items,error', [
    ((('book_title', 1),), 'unknown_diagnostic_key'), (((1, 1),), 'unknown_diagnostic_key'),
    (('attempt',), 'invalid_diagnostics'), ((('attempt', 'text'),), 'invalid_diagnostics'),
    ((('attempt', None),), 'invalid_diagnostics'), ((('attempt', -1),), 'invalid_diagnostics'),
    ((('attempt', float('nan')),), 'invalid_diagnostics'),
    ((('units_done', 1), ('attempt', 1)), 'invalid_diagnostics'),
    ((('attempt', 1), ('attempt', 2)), 'invalid_diagnostics'), ([('attempt', 1)], 'invalid_diagnostics'),
    (tuple((f'k{i}', i) for i in range(17)), 'invalid_diagnostics'),
])
def test_diagnostics_are_allowlisted_and_bounded(items, error):
    with pytest.raises(JobEventError, match=f'^{error}$'):
        JobWarning('code', items)


def test_full_allowlist_fits_the_size_limits():
    full = job_diagnostics(**{key: 2**63 - 1 for key in DIAGNOSTIC_KEYS})
    assert len(encode_event(event(payload=JobWarning('w', full)))) <= MAX_EVENT_BYTES


def test_serialized_diagnostics_have_a_byte_limit(monkeypatch):
    monkeypatch.setattr('ayvu.kernel.failures.MAX_DIAGNOSTICS_BYTES', 8)
    with pytest.raises(JobEventError, match='^invalid_diagnostics$'):
        JobWarning('code', job_diagnostics(attempt=1))


def test_encoded_size_limit_is_enforced(monkeypatch):
    monkeypatch.setattr('ayvu.jobs.events.MAX_EVENT_BYTES', 16)
    with pytest.raises(JobEventError, match='^event_too_large$'):
        encode_event(event())


@pytest.mark.parametrize('state', [state for state in JobState if state not in OUTCOMES])
def test_nonterminal_states_cannot_be_outcomes(state):
    with pytest.raises(JobEventError, match='^outcome_requires_terminal_state$'):
        JobOutcome(state, JobTerminalSummary('done'))


@pytest.mark.parametrize('state,summary,failure', [
    (JobState.FAILED, JobTerminalSummary('timeout'), None),
    (JobState.FAILED, JobTerminalSummary('other'), JobFailure(JobFailureFamily.RESOURCE, 'timeout')),
    (JobState.FAILED, JobTerminalSummary('stop'), JobFailure(JobFailureFamily.CANCELLED, 'stop')),
    (JobState.VALIDATED, JobTerminalSummary('timeout'), JobFailure(JobFailureFamily.RESOURCE, 'timeout')),
    (JobState.CANCELLED, JobTerminalSummary('timeout'), JobFailure(JobFailureFamily.RESOURCE, 'timeout')),
    (JobState.VALIDATED, None, None),
])
def test_outcome_rejects_contradictory_payloads(state, summary, failure):
    with pytest.raises(JobEventError, match='^invalid_outcome$'):
        JobOutcome(state, summary, failure)


@pytest.mark.parametrize('state,route', [
    (JobState.VALIDATED, (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING)),
    (JobState.PARTIALLY_COMPLETED, (JobState.PREFLIGHT, JobState.RUNNING)),
    (JobState.FAILED, (JobState.PREFLIGHT,)),
    (JobState.CANCELLED, ()),
])
def test_each_terminal_outcome_pairs_exactly_with_its_job_state(state, route):
    job = new_job(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'a' * 64), JOB, NOW)
    for step in route:
        job = transition_job(job, step, expected_revision=job.revision, now=NOW)
    outcome = OUTCOMES[state]
    finished = transition_job(job, state, expected_revision=job.revision, now=NOW, summary=outcome.summary)
    assert outcome.matches(finished)
    assert not outcome.matches(job)
    for other in OUTCOMES.values():
        if other is not outcome:
            assert not other.matches(finished)


def test_expired_lease_is_not_a_terminal_outcome():
    with pytest.raises(JobEventError, match='^outcome_requires_terminal_state$'):
        JobOutcome(JobState.RUNNING, JobTerminalSummary('requires_reconciliation'))
