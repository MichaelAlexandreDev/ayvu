from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import json
from uuid import UUID

import pytest

from ayvu.provenance import (
    MAX_ARTIFACTS, MAX_CONFIGURATION_REFS, MAX_PARENTS, MAX_RECORD_BYTES, NOT_APPLICABLE, UNKNOWN, ActorKind,
    ConfigurationRef, ProducerIdentity, ProvenanceRecord, ProvenanceRequest, ProvenanceValidationError, SourceKind,
    SubjectRef, decode_record, encode_record, request_identity,
)

PROJECT = str(UUID(int=1))
JOB = str(UUID(int=2))
A, B, C = (str(UUID(int=n)) for n in (10, 11, 12))
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
PRODUCER = ProducerIdentity('libretranslate@1.6', 'unknown', 'epub@1', 'segments@1')
REFERENCE = 'config:' + 'a' * 64
CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub', 'C:\\Users\\reader\\book.epub',
            'Translate this prompt', 'https://user:hunter2@example.test/translate', 'glossary term: whale => baleia')


def request(**changes):
    return ProvenanceRequest(**{'project_id': PROJECT, 'job_id': JOB, 'operation': 'translate',
                                'actor_kind': ActorKind.PROVIDER, 'source_kind': SourceKind.PROVIDER,
                                'producer': PRODUCER, **changes})


def record(**changes):
    return ProvenanceRecord(A, NOW, request(**changes))


FULL = dict(subject=SubjectRef('content_unit', 2, 'unit-1'), outcome_code='approved',
            configuration_refs=(ConfigurationRef('profile', REFERENCE), ConfigurationRef('glossary', 'rev:' + 'b' * 64)),
            parents=(C, B), supersedes=A, artifacts=(C, B), job_sequence=7, correlation_id=B, operation_id=C,
            idempotency_key='step:translate:7')


def test_serialized_kinds_are_exact():
    assert {kind.value for kind in ActorKind} == {'system', 'human', 'provider', 'ai'}
    assert {kind.value for kind in SourceKind} == {
        'pipeline', 'provider', 'cache', 'translation_memory', 'human', 'ai', 'validation'}


@pytest.mark.parametrize('changes', [{}, FULL], ids=['minimal', 'full'])
def test_record_round_trips_canonically(changes):
    original = ProvenanceRecord(B, NOW, request(**changes))
    text = encode_record(original)
    decoded = decode_record(text)
    assert decoded == original and encode_record(decoded) == text
    data = json.loads(text)
    assert data['schema_version'] == 1 and list(data) == sorted(data)


def test_reference_order_carries_no_meaning():
    first = request(parents=(A, B), artifacts=(A, B), configuration_refs=(
        ConfigurationRef('a', REFERENCE), ConfigurationRef('b', REFERENCE)))
    second = request(parents=(B, A), artifacts=(B, A), configuration_refs=(
        ConfigurationRef('b', REFERENCE), ConfigurationRef('a', REFERENCE)))
    assert first == second and request_identity(first) == request_identity(second)
    assert first.parents == (A, B)


def test_identity_covers_content_but_not_record_id_or_time():
    base = ProvenanceRecord(A, NOW, request(idempotency_key='k'))
    retry = ProvenanceRecord(B, NOW + timedelta(days=1), request(idempotency_key='k'))
    assert request_identity(base.request) == request_identity(retry.request)
    assert encode_record(base) != encode_record(retry)
    for change in ({'operation': 'other'}, {'outcome_code': 'x'}, {'parents': (A,)}, {'job_sequence': 1},
                   {'subject': SubjectRef('n', 1, 'v')}, {'source_kind': SourceKind.CACHE}):
        assert request_identity(request(idempotency_key='k', **change)) != request_identity(base.request)


@pytest.mark.parametrize('values,expected', [
    (('libre@1', 'argos@2', 'epub@1', 'segments@1'), True),
    (('not_applicable', 'not_applicable', 'epub@1', 'segments@1'), True),
    (('libre@1', UNKNOWN, 'epub@1', 'segments@1'), False),
    ((UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN), False),
    ((NOT_APPLICABLE, NOT_APPLICABLE, NOT_APPLICABLE, UNKNOWN), False),
])
def test_unknown_is_explicit_and_never_counts_as_identified(values, expected):
    assert ProducerIdentity(*values).fully_identified is expected


def test_producer_values_cannot_be_omitted():
    with pytest.raises(TypeError):
        ProducerIdentity('libre@1', 'model', 'epub@1')
    with pytest.raises(ProvenanceValidationError, match='^invalid_producer$'):
        request(producer=None)
    for value in (None, '', 'Has Space', '/usr/bin/x', 'x' * 65, 'https://user:pw@host'):
        with pytest.raises(ProvenanceValidationError, match='^invalid_producer$'):
            ProducerIdentity(value, 'unknown', 'unknown', 'unknown')


def test_no_field_claims_reproducibility():
    data = json.loads(encode_record(record()))
    assert 'reproducible' not in json.dumps(data)
    assert not hasattr(record(), 'reproducible') and not hasattr(PRODUCER, 'reproducible')


@pytest.mark.parametrize('value', ['c0001-s0001', 'unit-1', 'a', 'Ns:1.2_3', 'A' * 128])
def test_subject_values_are_opaque_and_kept_exactly(value):
    decoded = decode_record(encode_record(record(subject=SubjectRef('content_unit', 1, value))))
    assert decoded.request.subject.value == value and decoded.request.subject.namespace == 'content_unit'


def test_subject_has_no_derived_structure():
    assert {name for name in dir(SubjectRef('n', 1, 'c0001-s0001')) if not name.startswith('_')} == {
        'namespace', 'version', 'value'}


@pytest.mark.parametrize('namespace,version,value', [
    ('Bad Namespace', 1, 'v'), ('n', 0, 'v'), ('n', True, 'v'), ('n', '1', 'v'), ('n', 1, ''), ('n', 1, None),
    ('n', 1, 'with space'), ('n', 1, '../escape'), ('n', 1, '/abs'), ('n', 1, 'A' * 129), ('n', 1, 'line\nbreak'),
])
def test_invalid_subjects_are_refused(namespace, version, value):
    with pytest.raises(ProvenanceValidationError, match='^invalid_subject$'):
        SubjectRef(namespace, version, value)


@pytest.mark.parametrize('changes,error', [
    ({'project_id': 'x'}, 'invalid_identity'), ({'job_id': '../x'}, 'invalid_identity'),
    ({'operation': 'Has Space'}, 'invalid_operation'), ({'operation': None}, 'invalid_operation'),
    ({'actor_kind': 'human'}, 'invalid_kind'), ({'source_kind': 'cache'}, 'invalid_kind'),
    ({'subject': 'unit-1'}, 'invalid_subject'), ({'outcome_code': 'Has Space'}, 'invalid_outcome_code'),
    ({'configuration_refs': ['x']}, 'invalid_configuration_ref'), ({'configuration_refs': ('x',)}, 'invalid_configuration_ref'),
    ({'configuration_refs': (ConfigurationRef('a', REFERENCE), ConfigurationRef('a', 'x:' + 'c' * 64))}, 'invalid_configuration_ref'),
    ({'configuration_refs': tuple(ConfigurationRef(f'k{i}', REFERENCE) for i in range(MAX_CONFIGURATION_REFS + 1))}, 'invalid_configuration_ref'),
    ({'parents': [A]}, 'invalid_parents'), ({'parents': (A, A)}, 'invalid_parents'), ({'parents': ('x',)}, 'invalid_identity'),
    ({'parents': tuple(str(UUID(int=i)) for i in range(MAX_PARENTS + 1))}, 'invalid_parents'),
    ({'artifacts': [A]}, 'invalid_artifacts'), ({'artifacts': (A, A)}, 'invalid_artifacts'),
    ({'artifacts': tuple(str(UUID(int=i)) for i in range(MAX_ARTIFACTS + 1))}, 'invalid_artifacts'),
    ({'supersedes': 'x'}, 'invalid_identity'), ({'correlation_id': 'x'}, 'invalid_identity'), ({'operation_id': 'x'}, 'invalid_identity'),
    ({'job_sequence': 0}, 'invalid_job_sequence'), ({'job_sequence': True}, 'invalid_job_sequence'),
    ({'idempotency_key': 'has space'}, 'invalid_idempotency_key'), ({'idempotency_key': ''}, 'invalid_idempotency_key'),
], ids=lambda value: value if isinstance(value, str) else None)
def test_invalid_requests_fail_with_bounded_codes(changes, error):
    with pytest.raises(ProvenanceValidationError, match=f'^{error}$'):
        request(**changes)


@pytest.mark.parametrize('kind,reference', [
    ('Bad Kind', REFERENCE), ('k', 'plain text'), ('k', 'config:short'), ('k', 'config:' + 'A' * 64), ('k', None),
    ('k', '/home/reader/glossary.json'),
])
def test_configuration_references_must_be_opaque(kind, reference):
    with pytest.raises(ProvenanceValidationError, match='^invalid_configuration_ref$'):
        ConfigurationRef(kind, reference)


@pytest.mark.parametrize('stamp', [datetime(2026, 11, 3), datetime(2026, 11, 3, tzinfo=timezone(timedelta(hours=1))), 'now'])
def test_timestamps_must_be_utc(stamp):
    with pytest.raises(ProvenanceValidationError, match='^invalid_timestamp$'):
        ProvenanceRecord(A, stamp, request())


def test_record_requires_a_request_and_a_valid_identity():
    with pytest.raises(ProvenanceValidationError, match='^invalid_request$'):
        ProvenanceRecord(A, NOW, 'x')
    with pytest.raises(ProvenanceValidationError, match='^invalid_identity$'):
        ProvenanceRecord('x', NOW, request())


@pytest.mark.parametrize('version', [0, 2, True, '1'])
def test_unsupported_schema_version_is_distinct(version):
    with pytest.raises(ProvenanceValidationError, match='^unsupported_provenance_version$'):
        ProvenanceRecord(A, NOW, request(), version)
    data = json.loads(encode_record(record()))
    data['schema_version'] = version
    with pytest.raises(ProvenanceValidationError, match='^unsupported_provenance_version$'):
        decode_record(json.dumps(data, sort_keys=True, separators=(',', ':')))


@pytest.mark.parametrize('mutate', [
    lambda data: {**data, 'extra': 1},
    lambda data: {key: value for key, value in data.items() if key != 'record_id'},
    lambda data: {**data, 'request': {**data['request'], 'extra': 1}},
    lambda data: {**data, 'request': {**data['request'], 'source_text': 'Call me Ishmael.'}},
    lambda data: {**data, 'request': {**data['request'], 'parents': [B, A]}},
    lambda data: {**data, 'request': {**data['request'], 'source_kind': 'other'}},
    lambda data: {**data, 'request': {**data['request'], 'producer': {'provider': 'x'}}},
    lambda data: {**data, 'recorded_at': '2026-11-03T12:00:00+00:00'},
], ids=range(8))
def test_decode_rejects_anything_but_the_exact_contract(mutate):
    data = json.loads(encode_record(record(parents=(A, B))))
    with pytest.raises(ProvenanceValidationError, match='^invalid_record$') as caught:
        decode_record(json.dumps(mutate(data), sort_keys=True, separators=(',', ':')))
    assert 'Ishmael' not in str(caught.value)


@pytest.mark.parametrize('text', ['', 'not json', '[]', '{}', '[' * 20000, None, 7, 'x' * (MAX_RECORD_BYTES + 1)])
def test_decode_rejects_malformed_text(text):
    with pytest.raises(ProvenanceValidationError, match='^invalid_record$'):
        decode_record(text)


def test_decode_rejects_non_canonical_formatting():
    text = encode_record(record())
    for variant in (text + ' ', json.dumps(json.loads(text))):
        with pytest.raises(ProvenanceValidationError, match='^invalid_record$'):
            decode_record(variant)


def test_largest_allowed_record_fits_the_size_limit():
    big = record(
        subject=SubjectRef('n' * 64, 1, 'v' * 128), outcome_code='o' * 64, operation='p' * 64,
        producer=ProducerIdentity(*(['x' * 64] * 4)), idempotency_key='k' * 128,
        parents=tuple(str(UUID(int=i)) for i in range(MAX_PARENTS)),
        artifacts=tuple(str(UUID(int=i)) for i in range(MAX_ARTIFACTS)),
        configuration_refs=tuple(ConfigurationRef(f'k{i}' + 'x' * 60, 'r' * 32 + ':' + 'a' * 64)
                                 for i in range(MAX_CONFIGURATION_REFS)))
    assert decode_record(encode_record(big)) == big


def test_oversized_record_is_refused(monkeypatch):
    monkeypatch.setattr('ayvu.provenance.MAX_RECORD_BYTES', 16)
    with pytest.raises(ProvenanceValidationError, match='^record_too_large$'):
        encode_record(record())


@pytest.mark.parametrize('canary', CANARIES)
def test_private_text_cannot_enter_any_field_and_errors_do_not_quote_it(canary):
    for build in (lambda: SubjectRef(canary, 1, 'v'), lambda: SubjectRef('n', 1, canary),
                  lambda: ProducerIdentity(canary, 'x', 'x', 'x'), lambda: ConfigurationRef(canary, REFERENCE),
                  lambda: ConfigurationRef('k', canary), lambda: request(operation=canary),
                  lambda: request(outcome_code=canary), lambda: request(idempotency_key=canary),
                  lambda: request(parents=(canary,)), lambda: request(artifacts=(canary,)),
                  lambda: request(supersedes=canary), lambda: request(project_id=canary)):
        with pytest.raises(ProvenanceValidationError) as caught:
            build()
        assert canary not in str(caught.value) and canary not in repr(caught.value)


def test_repr_exposes_only_codes():
    full = ProvenanceRecord(A, NOW, request(**FULL))
    text = ' '.join(map(repr, (full, full.request, full.request.subject, full.request.configuration_refs[0])))
    for secret in ('unit-1', REFERENCE, 'step:translate:7'):
        assert secret not in text


def test_values_are_frozen():
    value = record()
    with pytest.raises(FrozenInstanceError):
        value.record_id = B
    with pytest.raises(FrozenInstanceError):
        value.request.operation = 'other'
