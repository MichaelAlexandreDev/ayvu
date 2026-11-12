import logging

import pytest
import requests

from ayvu.providers.ports import ProviderRoute
from ayvu.providers.capabilities import Capability, ProviderRequirements, SupportState
from ayvu.providers.discovery import DiscoveryOutcome
from ayvu.providers.network import ProviderOperation
from tests.support.provider_fakes import http_response

from tests.providers.provider_discovery_support import LANGUAGES
from tests.providers.provider_discovery_support import libre, libre_decision
from tests.support.paths import PROJECT_ROOT


def test_libretranslate_languages_keep_the_servers_order_and_names():
    subject, base = libre([http_response(200, LANGUAGES)])
    result = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    assert [(item.code, item.name, item.targets, item.state) for item in result.languages] == [
        ('en', 'English', ('pt', 'fr'), 'installed'), ('fr', 'French', ('en',), 'installed'), ('pt', 'Portuguese', ('en',), 'installed')]
    assert base.session.gets == [('http://localhost:5000/languages', 3.0)] and base.session.posts == []


@pytest.mark.parametrize('source,target,route', [
    ('en', 'pt', ProviderRoute('en', 'pt')), ('fr', 'pt', ProviderRoute('fr', 'pt', 'en')), ('de', 'pt', None),
])
def test_libretranslate_routes_come_from_live_language_data(source, target, route):
    subject, base = libre([http_response(200, LANGUAGES)])
    result = subject.resolve_route('libre_local', libre_decision(ProviderOperation.ROUTE), source, target)
    assert result.route == route and base.session.posts == []
    assert result.outcome is (DiscoveryOutcome.OK if route else DiscoveryOutcome.NO_ROUTE)


def test_libretranslate_test_sends_exactly_the_known_text():
    subject, base = libre(posts=[http_response(200, '{"translatedText": "Ola mundo"}')])
    result = subject.test_provider('libre_local', libre_decision(ProviderOperation.TEST), 'en', 'pt')
    assert result.text == 'Ola mundo' and base.session.gets == []
    assert base.session.posts == [('http://localhost:5000/translate', {'q': 'Hello world', 'source': 'en', 'target': 'pt', 'format': 'text'}, 3.0)]


@pytest.mark.parametrize('response,outcome,code', [
    (requests.exceptions.Timeout(), DiscoveryOutcome.UNAVAILABLE, 'provider_timeout'), (requests.exceptions.ConnectionError(), DiscoveryOutcome.UNAVAILABLE, 'provider_unavailable'),
    (http_response(401, 'no'), DiscoveryOutcome.UNAVAILABLE, 'provider_unauthorized'), (http_response(429, 'slow'), DiscoveryOutcome.UNAVAILABLE, 'provider_rejected'),
    (http_response(500, 'oops'), DiscoveryOutcome.UNAVAILABLE, 'provider_server_error'), (http_response(200, 'not json'), DiscoveryOutcome.DEGRADED, 'malformed_languages'),
    (http_response(200, '[]'), DiscoveryOutcome.DEGRADED, 'no_languages'), (http_response(200, '[{"code": "en", "name": "line\\nbreak"}]'), DiscoveryOutcome.DEGRADED, 'malformed_languages'),
], ids=lambda value: value if isinstance(value, str) else None)
def test_libretranslate_discovery_failures_are_typed(response, outcome, code):
    subject, base = libre([response])
    result = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    assert (result.outcome, result.code, result.languages) == (outcome, code, ())
    assert base.session.posts == [] and not any('health' in url for url, _ in base.session.gets)


@pytest.mark.parametrize('response,code', [
    (requests.exceptions.Timeout(), 'provider_timeout'), (http_response(401, 'no'), 'provider_unauthorized'), (http_response(429, 'slow'), 'provider_rate_limited'),
    (http_response(500, 'oops'), 'provider_server_error'), (http_response(400, 'bad'), 'provider_rejected'), (http_response(200, 'not json'), 'malformed_response'),
    (http_response(200, '{"translatedText": ""}'), 'empty_result'),
], ids=lambda value: value if isinstance(value, str) else None)
def test_libretranslate_test_failures_are_typed(response, code):
    subject, _ = libre(posts=[response])
    result = subject.test_provider('libre_local', libre_decision(ProviderOperation.TEST), 'en', 'pt')
    assert (result.outcome, result.code, result.text) == (DiscoveryOutcome.FAILED, code, None)


def test_libretranslate_preflight_makes_no_translation_request():
    subject, base = libre([http_response(200, LANGUAGES)])
    result = subject.preflight('libre_local', libre_decision(ProviderOperation.PREFLIGHT),
                               ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.ROUTE_DISCOVERY)), 'fr', 'pt')
    assert result.outcome is DiscoveryOutcome.OK and result.route == ProviderRoute('fr', 'pt', 'en')
    assert base.session.posts == [] and [url for url, _ in base.session.gets] == ['http://localhost:5000/languages']


def test_libretranslate_description_makes_no_request():
    subject, base = libre()
    description = subject.describe('libre_local')
    assert description.capabilities.state_of(Capability.CONTEXT) is SupportState.UNSUPPORTED
    assert base.session.gets == [] and base.session.posts == []


@pytest.mark.parametrize('body', ['Call me Ishmael.', '{"error": "Translate this prompt"}', '/home/reader/private/book.epub'])
def test_canaries_stay_out_of_results_and_logs(body, caplog):
    caplog.set_level(logging.DEBUG)
    url = 'http://user:hunter2@private.example.test'
    subject, _ = libre([http_response(500, body), http_response(200, '[{"code": "en", "name": "Secret label", "targets": ["pt"]}]')], posts=[http_response(500, body)], url=url)
    languages = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    test = subject.test_provider('libre_local', libre_decision(ProviderOperation.TEST), 'en', 'pt')
    listed = subject.languages('libre_local', libre_decision(ProviderOperation.LANGUAGES))
    texts = [repr(languages), repr(test), repr(test.failure), repr(subject), caplog.text, str(languages.code), str(test.code),
             repr(subject.describe('libre_local')), repr(libre_decision(ProviderOperation.TEST)), repr(listed.languages)]
    for secret in (body, 'hunter2', 'private.example.test', 'Secret label'):
        assert not any(secret in text for text in texts)


def test_contract_modules_name_no_provider_and_read_no_url():
    from pathlib import Path
    root = PROJECT_ROOT / 'src' / 'ayvu'
    for name in ('providers/discovery.py', 'providers/network.py'):
        source = (root / name).read_text()
        assert 'libretranslate' not in source.lower() and 'urlparse' not in source and 'urllib' not in source and 'localhost' not in source
        assert 'import requests' not in source and '/health' not in source


def test_libretranslate_routes_use_only_the_given_candidates():
    subject, _ = libre([http_response(200, LANGUAGES)])
    result = subject.resolve_route('libre_local', libre_decision(ProviderOperation.ROUTE), 'fr', 'pt', candidates=('es',))
    assert result.outcome is DiscoveryOutcome.NO_ROUTE and result.route is None
