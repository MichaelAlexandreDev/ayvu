from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

import pytest

from ayvu.jobs.model import JobRequest, JobState, JobTerminalSummary, new_job, transition_job
from ayvu.jobs.events import MAX_EVENT_BYTES, CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobEventKind, JobOutcome, JobProgress, decode_event, encode_event
from ayvu.kernel import JobEventError, JobFailure, JobFailureFamily, JobOperationError, JobWarning, job_diagnostics, normalize_failure

from tests.jobs.job_events_support import CANARIES, JOB, NOW, OUTCOMES, PAYLOADS
from tests.jobs.job_events_support import PROJECT, event


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
