from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import json
from uuid import UUID

import pytest

from ayvu.content import Constraint, ProtectedPart
from ayvu.job_events import JobFailure, JobFailureFamily, JobWarning
from ayvu.provider import (
    MAX_CONTEXT_CHARS, MAX_CONTEXT_ITEMS, MAX_TEXT_CHARS, MAX_WARNINGS, ConstraintsFeature, ContextFeature, ContextItem,
    DeadlineFeature, DeliveryState, ProtectedPartsFeature, ProviderRoute, ProviderStatus, ProviderUsage,
    ProviderValidationError, Requirement, TranslationProviderResult, UnitReference, request_diagnostics, result_diagnostics,
)
from provider_fakes import FAKE_PRODUCER, REQUEST_ID, failure, request, success

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
REFERENCE = 'config:' + 'a' * 64
UNIT = UnitReference('doc:v1:' + 'a' * 64, 'rev:v1:' + 'b' * 64, 'unit:v1:' + 'c' * 64)
PARTS = ProtectedPartsFeature((ProtectedPart(0, 2, 'markup'),), Requirement.REQUIRED)
CONTEXT = ContextFeature((ContextItem('previous', 'Earlier sentence'),), Requirement.ADVISORY)
CONSTRAINTS = ConstraintsFeature((Constraint('max_chars', 10),), Requirement.ADVISORY)
DEADLINE = DeadlineFeature(NOW, Requirement.ADVISORY)
CANARIES = ('Call me Ishmael.', '/home/reader/private/book.epub', 'https://user:hunter2@example.test/x',
            'Translate this prompt', '{"raw": "provider payload"}')


def test_request_keeps_exact_text_and_defaults_to_no_optional_data():
    item = request('  Hello \n')
    assert item.text == '  Hello \n' and item.schema_version == 1 and item.features == ()
    assert item.unit is None and item.route is None and item.cancellation is None


def test_request_carries_every_declared_field():
    token = type('Token', (), {'checkpoint': lambda self, point: None})()
    item = request(idempotency_key='step:1', unit=UNIT, route=ProviderRoute('en', 'pt', 'es'), policy_ref=REFERENCE,
                   configuration_ref=REFERENCE, cancellation=token, features=(CONTEXT, PARTS, CONSTRAINTS, DEADLINE))
    assert item.unit == UNIT and item.route.intermediate == 'es' and item.cancellation is token
    assert item.feature(ContextFeature) is CONTEXT and item.feature(DeadlineFeature) is DEADLINE


def test_features_have_a_canonical_order_and_lookups_return_none_when_absent():
    forward = request(features=(CONTEXT, PARTS, CONSTRAINTS, DEADLINE))
    backward = request(features=(DEADLINE, CONSTRAINTS, PARTS, CONTEXT))
    assert forward == backward
    assert [item.name for item in forward.features] == ['constraints', 'context', 'deadline', 'protected_parts']
    assert request().feature(ContextFeature) is None


def test_cancellation_token_does_not_affect_equality():
    token = type('Token', (), {'checkpoint': lambda self, point: None})()
    assert request() == request(cancellation=token)


@pytest.mark.parametrize('changes,code', [
    ({'request_id': 'not-a-uuid'}, 'invalid_request_id'), ({'request_id': str(UUID(int=0xabc)).upper()}, 'invalid_request_id'),
    ({'text': ''}, 'invalid_text'), ({'text': None}, 'invalid_text'), ({'text': '\ud800'}, 'invalid_text'), ({'text': b'x'}, 'invalid_text'),
    ({'source': 'not a locale'}, 'invalid_locale'), ({'target': None}, 'invalid_locale'),
    ({'idempotency_key': 'has space'}, 'invalid_idempotency_key'), ({'idempotency_key': 7}, 'invalid_idempotency_key'),
    ({'unit': 'unit:v1:x'}, 'invalid_unit_reference'), ({'route': 'en-pt'}, 'invalid_route'),
    ({'route': ProviderRoute('en', 'es')}, 'invalid_route'), ({'route': ProviderRoute('fr', 'pt')}, 'invalid_route'),
    ({'policy_ref': 'plain text'}, 'invalid_reference'), ({'configuration_ref': '/home/reader/config.json'}, 'invalid_reference'),
    ({'policy_ref': 'x:short'}, 'invalid_reference'), ({'cancellation': object()}, 'invalid_cancellation'),
    ({'features': ['x']}, 'invalid_features'), ({'features': ('x',)}, 'invalid_features'),
    ({'features': (CONTEXT, CONTEXT)}, 'duplicate_feature'), ({'schema_version': 2}, 'unsupported_provider_schema'),
    ({'schema_version': True}, 'unsupported_provider_schema'),
], ids=lambda value: value if isinstance(value, str) and value.startswith(('invalid', 'unsupported', 'duplicate')) else None)
def test_invalid_requests_fail_with_bounded_codes(changes, code):
    with pytest.raises(ProviderValidationError, match=f'^{code}$'):
        request(**changes)


def test_text_size_is_bounded():
    assert len(request('x' * MAX_TEXT_CHARS).text) == MAX_TEXT_CHARS
    with pytest.raises(ProviderValidationError, match='^invalid_text$'):
        request('x' * (MAX_TEXT_CHARS + 1))


@pytest.mark.parametrize('parts', [
    (ProtectedPart(0, 6, 'markup'),), (ProtectedPart(0, 2, 'markup'), ProtectedPart(1, 3, 'url')), ('x',),
])
def test_protected_parts_must_fit_the_text_without_overlapping(parts):
    with pytest.raises(ProviderValidationError, match='^invalid_protected_parts$'):
        request('Hello', features=(ProtectedPartsFeature(parts, Requirement.REQUIRED),))


@pytest.mark.parametrize('build,code', [
    (lambda: ProtectedPartsFeature((), Requirement.REQUIRED), 'invalid_protected_parts'),
    (lambda: ProtectedPartsFeature((ProtectedPart(0, 1, 'markup'),), 'required'), 'invalid_requirement'),
    (lambda: ContextFeature((), Requirement.ADVISORY), 'invalid_context'),
    (lambda: ContextFeature(('text',), Requirement.ADVISORY), 'invalid_context'),
    (lambda: ContextFeature(tuple(ContextItem('r', 'x') for _ in range(MAX_CONTEXT_ITEMS + 1)), Requirement.ADVISORY), 'invalid_context'),
    (lambda: ContextFeature(tuple(ContextItem('r', 'x' * (MAX_CONTEXT_CHARS // 2 + 1)) for _ in range(2)), Requirement.ADVISORY), 'invalid_context'),
    (lambda: ContextItem('Bad Role', 'x'), 'invalid_context'), (lambda: ContextItem('role', ''), 'invalid_context'),
    (lambda: ConstraintsFeature((), Requirement.ADVISORY), 'invalid_constraints'),
    (lambda: ConstraintsFeature(('x',), Requirement.ADVISORY), 'invalid_constraints'),
    (lambda: DeadlineFeature(datetime(2026, 1, 1), Requirement.ADVISORY), 'invalid_deadline'),
    (lambda: DeadlineFeature(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))), Requirement.ADVISORY), 'invalid_deadline'),
    (lambda: DeadlineFeature('soon', Requirement.ADVISORY), 'invalid_deadline'),
    (lambda: DeadlineFeature(NOW, None), 'invalid_requirement'),
    (lambda: ProviderRoute('en', 'not a locale'), 'invalid_route'), (lambda: ProviderRoute('en', 'pt', '/x'), 'invalid_route'),
    (lambda: UnitReference('x', 'rev:v1:' + 'b' * 64, 'unit:v1:' + 'c' * 64), 'invalid_unit_reference'),
    (lambda: UnitReference('doc:v1:' + 'a' * 64, 'unit:v1:' + 'c' * 64, 'unit:v1:' + 'c' * 64), 'invalid_unit_reference'),
    (lambda: UnitReference('doc:v1:' + 'a' * 64, 'rev:v1:' + 'b' * 64, 'c0001-s0001'), 'invalid_unit_reference'),
])
def test_invalid_values_fail_with_bounded_codes(build, code):
    with pytest.raises(ProviderValidationError, match=f'^{code}$'):
        build()


def test_a_success_has_text_and_a_completed_delivery():
    result = success(request())
    assert (result.status, result.text, result.failure, result.delivery) == (
        ProviderStatus.SUCCEEDED, 'Ola', None, DeliveryState.COMPLETED)


@pytest.mark.parametrize('text', ['', None, 7], ids=repr)
def test_a_success_cannot_be_empty(text):
    with pytest.raises(ProviderValidationError, match='^invalid_success$'):
        success(request(), text=text)


def test_a_success_cannot_hold_a_failure_or_an_uncertain_delivery():
    item = request()
    with pytest.raises(ProviderValidationError, match='^invalid_success$'):
        success(item, failure=JobFailure(JobFailureFamily.INTERNAL, 'x'))
    with pytest.raises(ProviderValidationError, match='^invalid_success$'):
        TranslationProviderResult(item.request_id, ProviderStatus.SUCCEEDED, FAKE_PRODUCER, DeliveryState.UNKNOWN, text='Ola')


@pytest.mark.parametrize('status,family,delivery,text', [
    (ProviderStatus.FAILED, None, DeliveryState.UNKNOWN, None),
    (ProviderStatus.FAILED, JobFailureFamily.INTERNAL, DeliveryState.UNKNOWN, 'partial'),
    (ProviderStatus.FAILED, JobFailureFamily.CANCELLED, DeliveryState.UNKNOWN, None),
    (ProviderStatus.CANCELLED, JobFailureFamily.INTERNAL, DeliveryState.NOT_SENT, None),
    (ProviderStatus.CANCELLED, JobFailureFamily.CANCELLED, DeliveryState.COMPLETED, None),
    (ProviderStatus.CANCELLED, None, DeliveryState.NOT_SENT, None),
])
def test_failed_and_cancelled_results_cannot_carry_text_or_contradict_themselves(status, family, delivery, text):
    problem = None if family is None else JobFailure(family, 'code')
    with pytest.raises(ProviderValidationError, match='^invalid_failure_result$'):
        TranslationProviderResult(REQUEST_ID, status, FAKE_PRODUCER, delivery, text=text, failure=problem)


@pytest.mark.parametrize('changes,code', [
    ({'route': 'en-pt'}, 'invalid_route'), ({'warnings': ['x']}, 'invalid_result'), ({'warnings': ('x',)}, 'invalid_result'),
    ({'warnings': tuple(JobWarning('w') for _ in range(MAX_WARNINGS + 1))}, 'invalid_result'),
    ({'usage': 'x'}, 'invalid_result'), ({'schema_version': 2}, 'unsupported_provider_schema'),
])
def test_invalid_results_fail_with_bounded_codes(changes, code):
    with pytest.raises(ProviderValidationError, match=f'^{code}$'):
        success(request(), **changes)


@pytest.mark.parametrize('status,producer,delivery', [
    ('succeeded', FAKE_PRODUCER, DeliveryState.COMPLETED), (ProviderStatus.SUCCEEDED, 'x', DeliveryState.COMPLETED),
    (ProviderStatus.SUCCEEDED, FAKE_PRODUCER, 'completed'),
])
def test_result_headers_are_typed(status, producer, delivery):
    with pytest.raises(ProviderValidationError, match='^invalid_result$'):
        TranslationProviderResult(REQUEST_ID, status, producer, delivery, text='x')


def test_result_requires_a_valid_request_id():
    with pytest.raises(ProviderValidationError, match='^invalid_request_id$'):
        TranslationProviderResult('x', ProviderStatus.SUCCEEDED, FAKE_PRODUCER, DeliveryState.COMPLETED, text='x')


@pytest.mark.parametrize('field,value', [('characters_submitted', -1), ('requests', True), ('latency_ms', 1.5), ('requests', 2**63)])
def test_usage_is_bounded(field, value):
    with pytest.raises(ProviderValidationError, match='^invalid_usage$'):
        ProviderUsage(**{field: value})


def test_unknown_usage_is_explicit_and_distinct_from_zero():
    assert ProviderUsage() != ProviderUsage(requests=0, characters_submitted=0, latency_ms=0)
    assert ProviderUsage().requests is None


def everything():
    item = request(CANARIES[0], idempotency_key='step:1', unit=UNIT, policy_ref=REFERENCE, configuration_ref=REFERENCE,
                   features=(ContextFeature((ContextItem('previous', CANARIES[3]),), Requirement.ADVISORY), CONSTRAINTS, DEADLINE))
    return item, success(item, CANARIES[4])


def test_repr_exposes_only_codes():
    item, result = everything()
    values = [item, result, *item.features, item.feature(ContextFeature).items[0], UNIT]
    text = ' '.join(map(repr, values))
    for secret in (*CANARIES, REFERENCE, 'step:1', UNIT.unit_id, UNIT.document_id):
        assert secret not in text


def test_diagnostics_are_an_allowlist():
    item, result = everything()
    projected = json.dumps([request_diagnostics(item), result_diagnostics(result)])
    for secret in (*CANARIES, REFERENCE, 'step:1', UNIT.unit_id, UNIT.document_id, 'Earlier'):
        assert secret not in projected
    assert request_diagnostics(item) == {
        'schema_version': 1, 'request_id': REQUEST_ID, 'source_locale': 'en', 'target_locale': 'pt',
        'text_chars': len(CANARIES[0]), 'features': [['constraints', 'advisory'], ['context', 'advisory'], ['deadline', 'advisory']],
    }
    assert result_diagnostics(result)['text_chars'] == len(CANARIES[4])


def test_failure_diagnostics_show_the_code_and_no_text():
    projected = result_diagnostics(failure(request(), warnings=(JobWarning('ignored_context'),), route=ProviderRoute('en', 'pt', 'es')))
    assert projected['failure'] == {'family': 'provider_transient', 'code': 'fake_failure', 'retryable': True}
    assert projected['warnings'] == ['ignored_context'] and projected['text_chars'] is None
    assert projected['route'] == ['en', 'es', 'pt'] and projected['status'] == 'failed'


@pytest.mark.parametrize('canary', CANARIES)
def test_errors_never_quote_rejected_values(canary):
    for build in (lambda: request(source=canary), lambda: request(idempotency_key=canary), lambda: request(policy_ref=canary),
                  lambda: ContextItem(canary, 'x'), lambda: UnitReference(canary, canary, canary), lambda: ProviderRoute(canary, 'pt')):
        with pytest.raises(ProviderValidationError) as caught:
            build()
        assert canary not in str(caught.value) and canary not in repr(caught.value)


def test_values_are_frozen():
    item, result = everything()
    with pytest.raises(FrozenInstanceError):
        item.text = 'x'
    with pytest.raises(FrozenInstanceError):
        result.text = 'x'
