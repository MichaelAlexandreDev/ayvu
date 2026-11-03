from datetime import UTC, datetime, timedelta, timezone
import json
from uuid import UUID

import pytest

from ayvu.job import JobRequest, JobState, JobTerminalSummary, new_job, transition_job
from ayvu.job_events import (
    DIAGNOSTIC_KEYS, MAX_EVENT_BYTES, CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent,
    JobEventAcceptance, JobEventError, JobEventKind, JobEventOrderError, JobEventSequence, JobFailure, JobFailureFamily,
    JobOperationError, JobOutcome, JobProgress, JobWarning, decode_event, encode_event, job_diagnostics,
    normalize_failure,
)

JOB = str(UUID(int=2))
PROJECT = str(UUID(int=3))
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub',
            'Translate this prompt', 'https://user:hunter2@example.test/translate', 'sk-secret-token')


def event(sequence=1, payload=None, *, event_id=None, job_id=JOB, project=PROJECT, at=NOW):
    return JobEvent(event_id or str(UUID(int=1000 + sequence)), job_id, sequence, at, JobCorrelation(project),
                    payload if payload is not None else JobProgress('translate', 'documents', sequence, None))


OUTCOMES = {
    JobState.VALIDATED: JobOutcome(JobState.VALIDATED, JobTerminalSummary('validated', (('z', 1), ('a', 2)))),
    JobState.PARTIALLY_COMPLETED: JobOutcome(JobState.PARTIALLY_COMPLETED, JobTerminalSummary('validation_findings')),
    JobState.FAILED: JobOutcome(JobState.FAILED, JobTerminalSummary('timeout'),
                                JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout', True,
                                           job_diagnostics(attempt=3, retry_after_ms=1500.5))),
    JobState.CANCELLED: JobOutcome(JobState.CANCELLED, JobTerminalSummary('user_requested')),
}
PAYLOADS = [
    JobProgress('translate', 'documents', 1, 3, job_diagnostics(units_done=4, partial_output_retained=True)),
    JobProgress('translate', 'documents', 0),
    JobProgress('validate', 'ratio', 0.5, 1.0),
    JobWarning('glossary_term_skipped', job_diagnostics(units_skipped=2)),
    CancellationRequest('user_requested', NOW),
    CancellationAcknowledgement('user_requested', 'between_documents'),
    *OUTCOMES.values(),
]


@pytest.mark.parametrize('payload', PAYLOADS, ids=repr)
def test_every_payload_round_trips_canonically(payload):
    original = event(payload=payload)
    text = encode_event(original)
    decoded = decode_event(text)
    assert decoded == original and encode_event(decoded) == text
    assert type(decoded.payload) is type(payload)
    data = json.loads(text)
    assert list(data) == sorted(data) and data['schema_version'] == 1 and data['kind'] == original.kind.value


def test_kind_is_derived_from_the_payload():
    assert {event(payload=payload).kind for payload in PAYLOADS} == set(JobEventKind)


def test_numeric_types_survive_the_round_trip():
    progress = decode_event(encode_event(event(payload=JobProgress(
        'translate', 'ratio', 1.0, 2, job_diagnostics(partial_output_retained=True, attempt=1))))).payload
    assert type(progress.completed) is float and type(progress.total) is int
    assert dict(progress.diagnostics) == {'attempt': 1, 'partial_output_retained': True}
    assert type(dict(progress.diagnostics)['partial_output_retained']) is bool


def test_outcome_counter_order_survives_the_round_trip():
    decoded = decode_event(encode_event(event(payload=OUTCOMES[JobState.VALIDATED])))
    assert decoded.payload.summary.counters == (('z', 1), ('a', 2))


@pytest.mark.parametrize('mutate', [
    lambda data: {**data, 'extra': 1},
    lambda data: {key: value for key, value in data.items() if key != 'sequence'},
    lambda data: {**data, 'kind': 'unknown'},
    lambda data: {**data, 'kind': 'warning'},
    lambda data: {**data, 'sequence': 0},
    lambda data: {**data, 'sequence': True},
    lambda data: {**data, 'occurred_at': '2026-11-03T12:00:00'},
    lambda data: {**data, 'occurred_at': '2026-11-03T12:00:00+00:00'},
    lambda data: {**data, 'payload': {**data['payload'], 'extra': 1}},
    lambda data: {**data, 'payload': {**data['payload'], 'phase': 'Private Canary Text'}},
    lambda data: {**data, 'payload': {**data['payload'], 'diagnostics': {'book_title': 1}}},
    lambda data: {**data, 'correlation': {'project_id': PROJECT}},
], ids=range(12))
def test_decode_rejects_anything_but_the_exact_contract(mutate):
    data = json.loads(encode_event(event(payload=JobProgress('translate', 'documents', 1, 3))))
    text = json.dumps(mutate(data), sort_keys=True, separators=(',', ':'))
    with pytest.raises(JobEventError, match='^invalid_event$') as caught:
        decode_event(text)
    assert 'Canary' not in str(caught.value)


@pytest.mark.parametrize('text', [
    '', 'not json', '[]', '{}', '{"a":1,"a":2}', '[' * 20000, None, 7,
    '{"completed":NaN}', 'x' * (MAX_EVENT_BYTES + 1),
])
def test_decode_rejects_malformed_text(text):
    with pytest.raises(JobEventError, match='^invalid_event$'):
        decode_event(text)


def test_decode_rejects_whitespace_and_key_order_variants():
    text = encode_event(event())
    for variant in (text + ' ', json.dumps(json.loads(text)), json.dumps(json.loads(text), sort_keys=True)):
        with pytest.raises(JobEventError, match='^invalid_event$'):
            decode_event(variant)


@pytest.mark.parametrize('version', [2, 0, 1.0, True, 'v1', None])
def test_other_schema_versions_are_reported_distinctly(version):
    data = json.loads(encode_event(event()))
    data['schema_version'] = version
    with pytest.raises(JobEventError, match='^unsupported_event_schema$'):
        decode_event(json.dumps(data, sort_keys=True, separators=(',', ':')))
    with pytest.raises(JobEventError, match='^unsupported_event_schema$'):
        JobEvent(str(UUID(int=1)), JOB, 1, NOW, JobCorrelation(PROJECT), JobWarning('w'), version)


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
    monkeypatch.setattr('ayvu.job_events.MAX_DIAGNOSTICS_BYTES', 8)
    with pytest.raises(JobEventError, match='^invalid_diagnostics$'):
        JobWarning('code', job_diagnostics(attempt=1))


def test_encoded_size_limit_is_enforced(monkeypatch):
    monkeypatch.setattr('ayvu.job_events.MAX_EVENT_BYTES', 16)
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


@pytest.mark.parametrize('stamp', [datetime(2026, 11, 3), datetime(2026, 11, 3, tzinfo=timezone(timedelta(hours=1)))])
def test_timestamps_must_be_utc(stamp):
    with pytest.raises(ValueError, match='^invalid_timestamp$'):
        event(at=stamp)
    with pytest.raises(ValueError, match='^invalid_timestamp$'):
        CancellationRequest('code', stamp)


@pytest.mark.parametrize('canary', CANARIES)
def test_private_text_cannot_enter_any_field_and_errors_do_not_quote_it(canary):
    for build in (lambda: JobProgress(canary, 'documents', 0), lambda: JobProgress('phase', canary, 0),
                  lambda: JobWarning(canary), lambda: JobFailure(JobFailureFamily.INPUT, canary),
                  lambda: CancellationRequest(canary, NOW), lambda: CancellationAcknowledgement('code', canary),
                  lambda: JobTerminalSummary(canary), lambda: JobWarning('code', ((canary, 1),)),
                  lambda: JobWarning('code', (('attempt', canary),)), lambda: JobCorrelation(canary)):
        with pytest.raises(ValueError) as caught:
            build()
        assert canary not in str(caught.value) and canary not in repr(caught.value)


@pytest.mark.parametrize('canary', CANARIES)
def test_unknown_exceptions_normalize_without_their_message(canary):
    failure = normalize_failure(RuntimeError(canary))
    assert (failure.family, failure.code, failure.retryable) == (JobFailureFamily.INTERNAL, 'unexpected_error', False)
    assert canary not in repr(failure)
    final = event(payload=JobOutcome(JobState.FAILED, JobTerminalSummary(failure.code), failure))
    assert canary not in encode_event(final) and canary not in repr(final)


def test_exception_subclasses_with_hostile_str_are_not_read():
    class Hostile(ValueError):
        def __str__(self):
            raise AssertionError('the message must not be read')

    assert normalize_failure(Hostile()).code == 'unexpected_error'


def test_known_errors_keep_their_bounded_code_and_envelopes_pass_through():
    envelope = JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, 'timeout', True)
    assert normalize_failure(JobOperationError(envelope)) is envelope
    assert normalize_failure(JobOperationError(envelope)).retryable
    assert normalize_failure(JobEventError('invalid_event')) == JobFailure(JobFailureFamily.INTERNAL, 'invalid_event')
    assert normalize_failure(JobEventError('not a slug: /home')).code == 'unexpected_error'
    with pytest.raises(JobEventError, match='^invalid_failure$'):
        JobOperationError('text')


def test_repr_exposes_no_diagnostics():
    diagnostics = job_diagnostics(retry_after_ms=424242)
    for value in (JobProgress('p', 'u', 0, None, diagnostics), JobWarning('w', diagnostics),
                  JobFailure(JobFailureFamily.INPUT, 'c', False, diagnostics), event(payload=JobWarning('w', diagnostics))):
        assert '424242' not in repr(value) and 'retry_after_ms' not in repr(value)


def test_family_values_match_the_documented_taxonomy():
    assert {family.value for family in JobFailureFamily} == {
        'input', 'capability', 'policy', 'provider_transient', 'provider_permanent', 'resource', 'integrity',
        'cancelled', 'internal',
    }


def test_outcome_does_not_match_a_job_with_the_same_state_and_another_summary():
    job = new_job(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'a' * 64), JOB, NOW)
    finished = transition_job(job, JobState.CANCELLED, expected_revision=1, now=NOW,
                              summary=JobTerminalSummary('shutdown'))
    assert not OUTCOMES[JobState.CANCELLED].matches(finished)
