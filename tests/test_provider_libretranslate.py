from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import logging
from threading import Barrier

import pytest
import requests

from ayvu.content import Constraint, ProtectedPart
from ayvu.job_events import JobFailureFamily
from ayvu.job_execution import CancellationSource
from ayvu.provenance import NOT_APPLICABLE, UNKNOWN
from ayvu.provider import (
    ConstraintsFeature, ContextFeature, ContextItem, DeadlineFeature, DeliveryState, ProtectedPartsFeature, ProviderRoute,
    ProviderStatus, Requirement, request_diagnostics, result_diagnostics,
)
from ayvu.provider_libretranslate import (
    ADAPTER_ID, ADAPTER_VERSION, LIBRETRANSLATE_SAFE_POINTS, LibreTranslateProvider, LibreTranslateProviderFactory,
)
from ayvu.provider_service import ProviderInstance, ProviderRegistry, execute
from ayvu.translator import (
    LibreTranslateTranslator, RequestRateLimiter, RoutedTranslator, TranslationRoute, Translator, TranslatorError,
)
from provider_fakes import FakeSession, http_response, request

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
URL = 'http://localhost:5000/translate'
BRIDGE = TranslationRoute('fr', 'pt', 'en')
SECRET_URL = 'http://user:hunter2@private.example.test:5000'


def translator(responses=(), **changes):
    base = LibreTranslateTranslator(**{'url': 'http://localhost:5000', 'timeout': 3.0, 'retries': 0, **changes})
    base.session = FakeSession(responses)
    return base


def provider(base, route=None, **changes):
    return LibreTranslateProvider(RoutedTranslator(base, route) if route else base, **{'clock': lambda: NOW, **changes})


def ok(body='Ola'):
    return http_response(200, '{"translatedText": "%s"}' % body)


def test_direct_success_produces_the_rich_result_and_the_exact_payload():
    base = translator([ok()])
    result = provider(base).execute(request('  Hello ', idempotency_key='step:1'))
    assert base.session.posts == [(URL, {'q': '  Hello ', 'source': 'en', 'target': 'pt', 'format': 'text'}, 3.0)]
    assert (result.status, result.text, result.delivery) == (ProviderStatus.SUCCEEDED, 'Ola', DeliveryState.COMPLETED)
    assert result.route == ProviderRoute('en', 'pt') and result.failure is None and result.warnings == ()
    assert result.usage.characters_submitted == 8 and result.usage.requests is None and result.usage.latency_ms is None


def test_identity_states_what_is_not_known():
    producer = provider(translator([ok()])).execute(request()).producer
    assert producer.provider == 'libretranslate' and producer.model == UNKNOWN and producer.pipeline == NOT_APPLICABLE
    assert producer.adapter == f'libretranslate_compat@{ADAPTER_VERSION}' and producer.fully_identified is False


def test_intermediate_route_keeps_the_call_order_and_reports_it():
    base = translator([ok('Hello'), ok('Ola')])
    result = provider(base, BRIDGE).execute(request('Salut', 'fr', 'pt'))
    assert [post[1] for post in base.session.posts] == [
        {'q': 'Salut', 'source': 'fr', 'target': 'en', 'format': 'text'},
        {'q': 'Hello', 'source': 'en', 'target': 'pt', 'format': 'text'},
    ]
    assert result.text == 'Ola' and result.route == ProviderRoute('fr', 'pt', 'en')


def test_other_locales_bypass_the_configured_route_as_they_do_today():
    base = translator([ok()])
    result = provider(base, BRIDGE).execute(request('Hello', 'en', 'pt'))
    assert len(base.session.posts) == 1 and result.route == ProviderRoute('en', 'pt')


def test_direct_configured_route_is_reported_as_direct():
    base = translator([ok()])
    assert provider(base, TranslationRoute('en', 'pt')).execute(request()).route == ProviderRoute('en', 'pt')


def test_compatible_requested_route_is_accepted():
    base = translator([ok('Hello'), ok('Ola')])
    assert provider(base, BRIDGE).execute(request('Salut', 'fr', 'pt', route=ProviderRoute('fr', 'pt', 'en'))).text == 'Ola'


@pytest.mark.parametrize('route,requested,source', [
    (None, ProviderRoute('en', 'pt', 'es'), 'en'),
    (BRIDGE, ProviderRoute('fr', 'pt'), 'fr'),
    (BRIDGE, ProviderRoute('fr', 'pt', 'es'), 'fr'),
])
def test_conflicting_requested_route_fails_before_http(route, requested, source):
    base = translator([])
    result = provider(base, route).execute(request('x', source, 'pt', route=requested))
    assert result.failure.code == 'route_conflict' and base.session.posts == []
    assert result.delivery is DeliveryState.NOT_SENT and result.failure.family is JobFailureFamily.CAPABILITY


def test_retries_backoff_and_status_handling_are_unchanged(monkeypatch):
    sleeps = []
    monkeypatch.setattr('ayvu.translator.time.sleep', sleeps.append)
    base = translator([http_response(503, 'busy'), requests.exceptions.ConnectionError('down'), ok()], retries=2,
                      retry_backoff=0.5, retry_backoff_max=8.0)
    result = provider(base).execute(request())
    assert result.text == 'Ola' and len(base.session.posts) == 3
    assert sleeps == [0.5, 1.0]
    assert all(post == base.session.posts[0] for post in base.session.posts)


def test_timeout_value_reaches_every_request():
    base = translator([requests.exceptions.Timeout(), ok()], retries=1, timeout=9.5, retry_backoff=0.0)
    provider(base).execute(request())
    assert [post[2] for post in base.session.posts] == [9.5, 9.5]


def test_rate_limit_applies_per_request_through_the_adapter(monkeypatch):
    sleeps = []
    times = iter([10.0, 10.1])
    monkeypatch.setattr('ayvu.translator.time.monotonic', lambda: next(times))
    monkeypatch.setattr('ayvu.translator.time.sleep', sleeps.append)
    base = translator([ok('Hello'), ok('Ola')], requests_per_second=2.0)
    provider(base, BRIDGE).execute(request('Salut', 'fr', 'pt'))
    assert sleeps == [pytest.approx(0.4)]


def bare_factory(sessions, **limits):
    def make():
        base = LibreTranslateTranslator(retries=0, **limits)
        base.session = FakeSession([ok()])
        sessions.append(base.session)
        return base

    return make


def libre_registry(factory):
    target = ProviderRegistry()
    target.register(ProviderInstance('libre_local', ADAPTER_ID, ADAPTER_VERSION, 1), factory)
    return target


def test_sequential_use_has_one_translator_and_one_session():
    sessions = []
    target = libre_registry(LibreTranslateProviderFactory(bare_factory(sessions)))
    with target.open('libre_local') as opened:
        assert execute(opened, request()).text == 'Ola'
    assert len(sessions) == 1 and sessions[0].closed == 1


def test_each_parallel_task_gets_its_own_session_and_shares_the_rate_limiter():
    limiter = RequestRateLimiter(requests_per_second=None)
    sessions = []
    created = []

    def make():
        base = bare_factory(sessions, rate_limiter=limiter)()
        created.append(base)
        return base

    target = libre_registry(LibreTranslateProviderFactory(make))
    barrier = Barrier(4)

    def task(_):
        with target.open('libre_local') as opened:
            barrier.wait(timeout=5)
            return execute(opened, request()).text

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(task, range(4))) == ['Ola'] * 4
    assert len({id(session) for session in sessions}) == 4
    assert {id(base.rate_limiter) for base in created} == {id(limiter)}
    assert [session.closed for session in sessions] == [1, 1, 1, 1] and all(len(session.posts) == 1 for session in sessions)


def test_factory_applies_the_configured_route():
    sessions = []

    def make():
        base = LibreTranslateTranslator(retries=0)
        base.session = FakeSession([ok('Hello'), ok('Ola')])
        sessions.append(base.session)
        return base

    target = libre_registry(LibreTranslateProviderFactory(make, route=BRIDGE))
    with target.open('libre_local') as opened:
        result = execute(opened, request('Salut', 'fr', 'pt'))
    assert result.route == ProviderRoute('fr', 'pt', 'en') and len(sessions[0].posts) == 2


def test_a_translator_without_a_session_needs_no_cleanup():
    class Plain(Translator):
        def translate(self, text, source, target):
            return text.upper()

    target = libre_registry(LibreTranslateProviderFactory(Plain))
    with target.open('libre_local') as opened:
        assert execute(opened, request()).text == 'HELLO'


def test_explicit_cleanup_runs_on_close():
    calls = []
    LibreTranslateProvider(translator(), cleanup=lambda: calls.append(1)).close()
    LibreTranslateProvider(translator()).close()
    assert calls == [1]


REQUIRED_FEATURES = [
    ContextFeature((ContextItem('previous', 'x'),), Requirement.REQUIRED),
    ConstraintsFeature((Constraint('max_chars', 5),), Requirement.REQUIRED),
    ProtectedPartsFeature((ProtectedPart(0, 2, 'markup'),), Requirement.REQUIRED),
    DeadlineFeature(NOW + timedelta(days=1), Requirement.REQUIRED),
]
ADVISORY_FEATURES = [
    (ContextFeature((ContextItem('previous', 'x'),), Requirement.ADVISORY), 'ignored_context'),
    (ConstraintsFeature((Constraint('max_chars', 5),), Requirement.ADVISORY), 'ignored_constraints'),
    (ProtectedPartsFeature((ProtectedPart(0, 2, 'markup'),), Requirement.ADVISORY), 'ignored_protected_parts'),
    (DeadlineFeature(NOW + timedelta(days=1), Requirement.ADVISORY), 'ignored_deadline'),
]


@pytest.mark.parametrize('feature', REQUIRED_FEATURES, ids=lambda item: item.name)
def test_required_unsupported_feature_fails_with_no_http(feature):
    base = translator([])
    result = provider(base).execute(request(features=(feature,)))
    assert base.session.posts == [] and result.status is ProviderStatus.FAILED
    assert result.failure.code == 'unsupported_required_feature' and result.failure.family is JobFailureFamily.CAPABILITY
    assert result.delivery is DeliveryState.NOT_SENT and result.text is None


@pytest.mark.parametrize('feature,code', ADVISORY_FEATURES, ids=lambda item: getattr(item, 'name', item))
def test_ignored_advisory_feature_is_an_explicit_warning(feature, code):
    base = translator([ok()])
    result = provider(base).execute(request(features=(feature,)))
    assert result.text == 'Ola' and [item.code for item in result.warnings] == [code]
    assert base.session.posts[0][1] == {'q': 'Hello', 'source': 'en', 'target': 'pt', 'format': 'text'}


def test_warnings_survive_a_failure():
    base = translator([requests.exceptions.Timeout()])
    result = provider(base).execute(request(features=(ADVISORY_FEATURES[0][0],)))
    assert result.failure.code == 'provider_timeout' and [item.code for item in result.warnings] == ['ignored_context']


@pytest.mark.parametrize('at', [NOW, NOW - timedelta(seconds=1)])
def test_expired_deadline_fails_with_no_http(at):
    base = translator([])
    result = provider(base).execute(request(features=(DeadlineFeature(at, Requirement.ADVISORY),)))
    assert result.failure.code == 'deadline_expired' and base.session.posts == []
    assert result.delivery is DeliveryState.NOT_SENT and not result.failure.retryable


def test_future_advisory_deadline_is_not_a_cancellation_claim():
    base = translator([requests.exceptions.Timeout()])
    result = provider(base).execute(request(features=(DeadlineFeature(NOW + timedelta(seconds=1), Requirement.ADVISORY),)))
    assert result.status is ProviderStatus.FAILED and result.failure.family is JobFailureFamily.PROVIDER_TRANSIENT


def test_cancellation_is_observed_before_the_first_request_only():
    source = CancellationSource(LIBRETRANSLATE_SAFE_POINTS)
    source.request('user_requested', NOW)
    base = translator([])
    result = provider(base).execute(request(cancellation=source.token))
    assert result.status is ProviderStatus.CANCELLED and base.session.posts == []
    assert result.failure.family is JobFailureFamily.CANCELLED and result.delivery is DeliveryState.NOT_SENT
    assert source.acknowledgement.safe_point == 'provider_before_request'


def test_a_request_during_a_blocking_call_does_not_claim_cancellation():
    source = CancellationSource(LIBRETRANSLATE_SAFE_POINTS)

    class Requesting(FakeSession):
        def post(self, url, *, json, timeout, allow_redirects=False):
            assert allow_redirects is False
            source.request('user_requested', NOW)
            return super().post(url, json=json, timeout=timeout, allow_redirects=allow_redirects)

    base = translator()
    base.session = Requesting([ok()])
    result = provider(base).execute(request(cancellation=source.token))
    assert result.status is ProviderStatus.SUCCEEDED and source.acknowledgement is None


def test_timeout_is_a_failure_and_never_a_cancellation():
    source = CancellationSource(LIBRETRANSLATE_SAFE_POINTS)
    result = provider(translator([requests.exceptions.Timeout()])).execute(request(cancellation=source.token))
    assert result.status is ProviderStatus.FAILED and result.failure.code == 'provider_timeout'
    assert result.delivery is DeliveryState.UNKNOWN and result.failure.retryable


@pytest.mark.parametrize('response,code,family,retryable,delivery', [
    (requests.exceptions.ConnectionError('down'), 'provider_unavailable', JobFailureFamily.PROVIDER_TRANSIENT, True, DeliveryState.UNKNOWN),
    (requests.exceptions.Timeout('slow'), 'provider_timeout', JobFailureFamily.PROVIDER_TRANSIENT, True, DeliveryState.UNKNOWN),
    (requests.exceptions.RequestException('other'), 'provider_request_failed', JobFailureFamily.PROVIDER_TRANSIENT, True, DeliveryState.UNKNOWN),
    (http_response(401, 'no'), 'provider_unauthorized', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(403, 'no'), 'provider_unauthorized', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(429, 'slow down'), 'provider_rate_limited', JobFailureFamily.PROVIDER_TRANSIENT, True, DeliveryState.COMPLETED),
    (http_response(500, 'oops'), 'provider_server_error', JobFailureFamily.PROVIDER_TRANSIENT, True, DeliveryState.COMPLETED),
    (http_response(400, 'bad'), 'provider_rejected', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(200, 'not json'), 'malformed_response', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(200, '{"other": 1}'), 'malformed_response', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(200, '[]'), 'malformed_response', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(200, '{"translatedText": 7}'), 'malformed_response', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(200, '{"translatedText": ""}'), 'empty_result', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
    (http_response(200, '{"translatedText": "   "}'), 'empty_result', JobFailureFamily.PROVIDER_PERMANENT, False, DeliveryState.COMPLETED),
], ids=lambda value: value if isinstance(value, str) and value.startswith(('provider', 'malformed', 'empty')) else None)
def test_every_failure_is_typed_and_never_a_translation(response, code, family, retryable, delivery):
    result = provider(translator([response])).execute(request())
    assert result.status is ProviderStatus.FAILED and result.text is None
    assert (result.failure.code, result.failure.family, result.failure.retryable, result.delivery) == (code, family, retryable, delivery)


def test_whitespace_source_may_translate_to_whitespace():
    result = provider(translator([http_response(200, '{"translatedText": "  "}')])).execute(request('   '))
    assert result.status is ProviderStatus.SUCCEEDED and result.text == '  '


def test_a_translator_raising_an_unclassified_error_is_internal():
    class Broken(Translator):
        def translate(self, text, source, target):
            raise TranslatorError('Secret body')

    class Exploding(Translator):
        def translate(self, text, source, target):
            raise RuntimeError('Secret body')

    for broken in (Broken(), Exploding()):
        result = LibreTranslateProvider(broken).execute(request())
        assert result.failure.code == 'unexpected_error' and result.failure.family is JobFailureFamily.INTERNAL
        assert result.delivery is DeliveryState.UNKNOWN and 'Secret' not in repr(result.failure)


def test_a_non_string_from_a_translator_is_not_a_translation():
    class Odd(Translator):
        def translate(self, text, source, target):
            return None

    assert LibreTranslateProvider(Odd()).execute(request()).failure.code == 'empty_result'


@pytest.mark.parametrize('body', [
    'Call me Ishmael.', '{"error": "Translate this prompt"}', '/home/reader/private/book.epub', 'token hunter2',
])
def test_canaries_never_reach_results_diagnostics_or_logs(body, caplog):
    caplog.set_level(logging.DEBUG)
    base = translator([http_response(500, body)], url=SECRET_URL)
    item = request('Private source text', features=(ContextFeature((ContextItem('previous', 'Private context'),), Requirement.ADVISORY),))
    result = provider(base).execute(item)
    texts = [repr(result), repr(result.failure), repr(item), repr(provider(base)), str(request_diagnostics(item)),
             str(result_diagnostics(result)), caplog.text, str(vars(result.failure))]
    for secret in (body, 'hunter2', SECRET_URL, 'Private source text', 'Private context', 'private.example.test'):
        assert not any(secret in text for text in texts)


def test_connection_error_with_credentials_in_the_url_does_not_leak():
    base = translator([requests.exceptions.ConnectionError(f'cannot reach {SECRET_URL}')], url=SECRET_URL)
    result = provider(base).execute(request())
    assert result.failure.code == 'userinfo_unsupported'
    assert 'hunter2' not in repr(result) and 'hunter2' not in str(result_diagnostics(result))


@pytest.mark.parametrize('response,message,reason,status', [
    (requests.exceptions.ConnectionError('x'),
     'Could not connect to LibreTranslate. Is the local translation server running?', 'connection', None),
    (requests.exceptions.Timeout('x'), 'LibreTranslate request timed out after 3.0 seconds', 'timeout', None),
    (requests.exceptions.RequestException('boom'), 'LibreTranslate request failed', 'request', None),
    (http_response(400, 'b' * 400), 'LibreTranslate HTTP error 400', 'http', 400),
    (http_response(200, 'not json'), 'LibreTranslate response was not valid JSON', 'malformed', None),
    (http_response(200, '{}'), 'LibreTranslate response did not include translatedText', 'malformed', None),
], ids=['connection', 'timeout', 'request', 'http', 'json', 'field'])
def test_error_messages_are_sanitized_and_keep_typed_contracts(response, message, reason, status):
    with pytest.raises(TranslatorError) as caught:
        translator([response]).translate('Hello', 'en', 'pt')
    assert str(caught.value) == message
    assert (caught.value.reason, caught.value.status_code) == (reason, status)


def test_existing_translator_still_accepts_an_empty_translation():
    # The deliberate difference from the rich path, until consumers migrate.
    assert translator([http_response(200, '{"translatedText": ""}')]).translate('Hello', 'en', 'pt') == ''


def test_plain_translator_errors_remain_constructible():
    error = TranslatorError('message')
    assert str(error) == 'message' and error.reason is None and error.status_code is None
    assert str(TranslatorError()) == ''


def test_factory_metadata_matches_what_the_registry_checks():
    factory = LibreTranslateProviderFactory(translator)
    assert (factory.adapter_id, factory.adapter_version) == (ADAPTER_ID, ADAPTER_VERSION) and 1 in factory.supported_config_versions
    assert repr(factory) == 'LibreTranslateProviderFactory()'


def test_refusals_follow_the_documented_order():
    source = CancellationSource(LIBRETRANSLATE_SAFE_POINTS)
    source.request('user_requested', NOW)
    expired = NOW - timedelta(seconds=1)
    everything = dict(route=ProviderRoute('en', 'pt', 'es'), cancellation=source.token)
    required = DeadlineFeature(expired, Requirement.REQUIRED)
    advisory = DeadlineFeature(expired, Requirement.ADVISORY)
    run = lambda **changes: provider(translator([])).execute(request(**changes)).failure.code
    assert run(features=(required,), **everything) == 'unsupported_required_feature'
    assert run(features=(advisory,), **everything) == 'route_conflict'
    assert run(features=(advisory,), cancellation=source.token) == 'deadline_expired'
    assert run(cancellation=source.token) == 'cancelled_before_request'
    assert source.acknowledgement is not None
