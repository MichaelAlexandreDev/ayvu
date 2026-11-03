from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path

import pytest
import requests

from ayvu.provider import (
    ContextFeature, ContextItem, ConstraintsFeature, DeadlineFeature, ProtectedPartsFeature, ProviderRoute, Requirement,
)
from ayvu.content import Constraint, ProtectedPart
from ayvu.provider_capabilities import (
    FEATURE_CAPABILITIES, AvailabilityState, Capability, EvidenceSource, LimitOrigin, LimitSemantics, NegotiationDecision,
    ProviderRequirements, SupportState, availability_projection, capability_fingerprint, negotiate, snapshot_projection,
)
from ayvu.provider_libretranslate import (
    _SUPPORTED_FEATURES, ADAPTER_ID, ADAPTER_VERSION, DEFAULT_AVAILABILITY_TTL, LIBRETRANSLATE_DESCRIPTOR,
    LibreTranslateAvailabilityProbe, LibreTranslateProvider, discover_route, libretranslate_snapshot,
)
from ayvu.provider_service import observe_availability
from ayvu.translator import LibreTranslateTranslator, RequestRateLimiter, TranslatorError
from provider_fakes import FakeSession, http_response, request

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
LANGUAGES = json.dumps([
    {'code': 'en', 'name': 'English', 'targets': ['pt', 'fr']},
    {'code': 'fr', 'name': 'French', 'targets': ['en']},
    {'code': 'pt', 'name': 'Portuguese', 'targets': ['en']},
])
SECRET_URL = 'http://user:hunter2@private.example.test:5000'


def translator(get_responses=(), **changes):
    base = LibreTranslateTranslator(**{'url': 'http://localhost:5000', 'timeout': 3.0, 'retries': 0, **changes})
    base.session = FakeSession(get_responses=get_responses)
    return base


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def probe(*responses, **changes):
    base = translator(responses, **changes)
    return base, LibreTranslateAvailabilityProbe(base, clock=lambda: NOW)


def test_descriptor_states_identity_and_compatibility():
    assert (LIBRETRANSLATE_DESCRIPTOR.adapter_id, LIBRETRANSLATE_DESCRIPTOR.adapter_version) == (ADAPTER_ID, ADAPTER_VERSION)
    assert LIBRETRANSLATE_DESCRIPTOR.protocol_id == 'libretranslate_http' and LIBRETRANSLATE_DESCRIPTOR.request_schema_versions == (1,)


@pytest.mark.parametrize('capabilities,state,evidence', [
    ([Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT, Capability.LANGUAGE_DISCOVERY, Capability.ROUTE_DISCOVERY,
      Capability.PRE_REQUEST_CANCELLATION], SupportState.SUPPORTED, EvidenceSource.ADAPTER_CODE),
    ([Capability.CONTEXT, Capability.CONSTRAINTS, Capability.PROTECTED_PARTS, Capability.DEADLINE, Capability.BATCH, Capability.STREAMING,
      Capability.STRUCTURED_OUTPUT, Capability.NATIVE_GLOSSARY, Capability.MULTIMODAL_INPUT, Capability.IN_FLIGHT_CANCELLATION,
      Capability.SHARED_INSTANCE_CONCURRENCY], SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    ([Capability.MODEL_IDENTITY, Capability.RETENTION_CLAIM, Capability.TRAINING_CLAIM, Capability.COST_REPORTING,
      Capability.RESOURCE_REPORTING], SupportState.UNKNOWN, EvidenceSource.NONE),
    ([Capability.NETWORK_CLASS, Capability.AUTHENTICATION], SupportState.CONFIGURATION_DEPENDENT, EvidenceSource.CONFIGURATION),
], ids=['supported', 'unsupported', 'unknown', 'configuration'])
def test_every_capability_is_asserted_with_evidence(capabilities, state, evidence):
    facts = {item.capability: item for item in libretranslate_snapshot(translator(), NOW).facts}
    assert len(facts) == len(Capability)
    for capability in capabilities:
        assert (facts[capability].state, facts[capability].evidence) == (state, evidence), capability


def test_the_table_above_covers_every_capability():
    subject = libretranslate_snapshot(translator(), NOW)
    assert {item.state for item in subject.facts} == set(SupportState)


def test_server_limits_are_unknown_and_client_settings_are_client_policy():
    limits = {item.name: item for item in libretranslate_snapshot(translator(timeout=2.5, retries=3), NOW).limits}
    for name in ('server_max_input_chars', 'server_max_concurrent_requests', 'server_request_quota'):
        assert limits[name].origin is LimitOrigin.SERVER and limits[name].semantics is LimitSemantics.UNKNOWN and limits[name].value is None
    assert limits['client_request_timeout'].value == 2500 and limits['client_request_timeout'].origin is LimitOrigin.CLIENT_POLICY
    assert limits['client_retries'].value == 3 and limits['client_retries'].evidence is EvidenceSource.CONFIGURATION
    assert limits['client_requests_per_second'].semantics is LimitSemantics.UNBOUNDED


def test_rate_limit_is_the_one_that_really_applies_when_shared():
    shared = RequestRateLimiter(requests_per_second=4.0)
    limits = {item.name: item for item in libretranslate_snapshot(translator(rate_limiter=shared, requests_per_second=9.0), NOW).limits}
    assert limits['client_requests_per_second'].value == 4.0


def test_loopback_address_changes_no_capability():
    local = libretranslate_snapshot(translator(url='http://127.0.0.1:5000'), NOW)
    remote = libretranslate_snapshot(translator(url='https://example.test'), NOW)
    assert local == remote and local.state_of(Capability.NETWORK_CLASS) is SupportState.CONFIGURATION_DEPENDENT
    assert capability_fingerprint(local) == capability_fingerprint(remote)


def test_claimed_features_match_what_the_adapter_honors():
    subject = libretranslate_snapshot(translator(), NOW)
    assert _SUPPORTED_FEATURES == frozenset(
        name for name, capability in FEATURE_CAPABILITIES.items() if subject.state_of(capability) is SupportState.SUPPORTED)
    features = [ContextFeature((ContextItem('previous', 'x'),), Requirement.REQUIRED),
                ConstraintsFeature((Constraint('max_chars', 5),), Requirement.REQUIRED),
                ProtectedPartsFeature((ProtectedPart(0, 1, 'markup'),), Requirement.REQUIRED),
                DeadlineFeature(NOW + timedelta(days=1), Requirement.REQUIRED)]
    for feature in features:
        capability = FEATURE_CAPABILITIES[feature.name]
        assert subject.state_of(capability) is SupportState.UNSUPPORTED
        base = translator()
        result = LibreTranslateProvider(base, clock=lambda: NOW).execute(request(features=(feature,)))
        assert result.failure.code == 'unsupported_required_feature' and base.session.gets == [] and base.session.posts == []


@pytest.mark.parametrize('capability,decision', [
    (Capability.PLAIN_TEXT_INPUT, NegotiationDecision.ACCEPT), (Capability.CONTEXT, NegotiationDecision.REJECT),
    (Capability.BATCH, NegotiationDecision.REJECT), (Capability.MODEL_IDENTITY, NegotiationDecision.PREFLIGHT_REQUIRED),
    (Capability.NETWORK_CLASS, NegotiationDecision.PREFLIGHT_REQUIRED),
])
def test_negotiation_against_the_real_snapshot_makes_no_call(capability, decision):
    base = translator()
    result = negotiate(libretranslate_snapshot(base, NOW), ProviderRequirements((capability,)))
    assert result.decision is decision and base.session.gets == [] and base.session.posts == []


def test_successful_probe_keeps_codes_and_not_names():
    base, subject = probe(http_response(200, LANGUAGES))
    result = subject.probe()
    assert (result.state, result.reason_code, result.observed_at) == (AvailabilityState.AVAILABLE, None, NOW)
    assert result.expires_at == NOW + DEFAULT_AVAILABILITY_TTL
    assert [(item.code, item.targets) for item in result.languages] == [('en', ('fr', 'pt')), ('fr', ('en',)), ('pt', ('en',))]
    assert 'English' not in repr(result)


def test_probe_only_gets_languages():
    base, subject = probe(http_response(200, LANGUAGES))
    subject.probe()
    assert base.session.gets == [('http://localhost:5000/languages', 3.0)]
    assert base.session.posts == [] and not any('health' in url for url, _ in base.session.gets)


def test_a_failing_probe_also_sends_no_text():
    base, subject = probe(requests.exceptions.Timeout())
    subject.probe()
    assert base.session.posts == [] and len(base.session.gets) == 1


def test_probe_uses_the_injected_ttl():
    base = translator([http_response(200, LANGUAGES)])
    result = LibreTranslateAvailabilityProbe(base, clock=lambda: NOW, ttl=timedelta(seconds=5)).probe()
    assert result.expires_at == NOW + timedelta(seconds=5)


@pytest.mark.parametrize('response,state,reason', [
    (requests.exceptions.Timeout('slow'), AvailabilityState.UNAVAILABLE, 'provider_timeout'),
    (requests.exceptions.ConnectionError('down'), AvailabilityState.UNAVAILABLE, 'provider_unavailable'),
    (requests.exceptions.RequestException('other'), AvailabilityState.UNAVAILABLE, 'provider_request_failed'),
    (http_response(401, 'no'), AvailabilityState.UNAVAILABLE, 'provider_unauthorized'),
    (http_response(403, 'no'), AvailabilityState.UNAVAILABLE, 'provider_unauthorized'),
    (http_response(500, 'oops'), AvailabilityState.UNAVAILABLE, 'provider_server_error'),
    (http_response(404, 'gone'), AvailabilityState.UNAVAILABLE, 'provider_rejected'),
    (http_response(200, 'not json'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '{"not": "a list"}'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '["not an object"]'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '[{"code": "en"}]'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '[{"code": "not a code", "name": "x"}]'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '[{"code": "en", "name": "x", "targets": ["/etc/passwd"]}]'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '[{"code": "en", "name": "x"}, {"code": "en", "name": "y"}]'), AvailabilityState.DEGRADED, 'malformed_languages'),
    (http_response(200, '[]'), AvailabilityState.DEGRADED, 'no_languages'),
], ids=lambda value: value if isinstance(value, str) else None)
def test_every_probe_outcome_is_typed_and_changes_availability_only(response, state, reason):
    base, subject = probe(response)
    before_descriptor = repr(LIBRETRANSLATE_DESCRIPTOR)
    before_snapshot = snapshot_projection(libretranslate_snapshot(base, NOW))
    result = subject.probe()
    assert (result.state, result.reason_code, result.languages, result.observed_at) == (state, reason, (), NOW)
    assert repr(LIBRETRANSLATE_DESCRIPTOR) == before_descriptor
    assert snapshot_projection(libretranslate_snapshot(base, NOW)) == before_snapshot


def test_too_much_discovery_data_is_refused_before_it_is_kept():
    many = json.dumps([{'code': f'x{index:04d}', 'name': 'n'} for index in range(513)])
    assert probe(http_response(200, many))[1].probe().reason_code == 'discovery_too_large'
    wide = json.dumps([{'code': 'en', 'name': 'n', 'targets': ['pt'] * 513}])
    assert probe(http_response(200, wide))[1].probe().reason_code == 'discovery_too_large'


def test_observe_availability_reports_an_unexpected_exception_as_unknown():
    class Broken:
        def list_languages(self):
            raise RuntimeError('Secret body')

    result = observe_availability(LibreTranslateAvailabilityProbe(Broken(), clock=lambda: NOW))
    assert (result.state, result.reason_code) == (AvailabilityState.UNKNOWN, 'probe_failed')


def test_availability_recovers_and_expires_without_touching_static_facts():
    clock = Clock()
    base = translator([requests.exceptions.Timeout(), http_response(200, LANGUAGES)])
    subject = LibreTranslateAvailabilityProbe(base, clock=clock, ttl=timedelta(seconds=30))
    fingerprint = capability_fingerprint(libretranslate_snapshot(base, NOW))
    first = subject.probe()
    assert first.state is AvailabilityState.UNAVAILABLE
    second = subject.probe()
    assert second.state is AvailabilityState.AVAILABLE
    clock.now = NOW + timedelta(seconds=30)
    stale = second.current(clock())
    assert (stale.state, stale.reason_code) == (AvailabilityState.UNKNOWN, 'evidence_expired')
    assert discover_route(second, 'en', 'pt', clock()) is None
    assert capability_fingerprint(libretranslate_snapshot(base, NOW)) == fingerprint


def availability():
    return probe(http_response(200, LANGUAGES))[1].probe()


@pytest.mark.parametrize('source,target,route', [
    ('en', 'pt', ProviderRoute('en', 'pt')),
    ('fr', 'pt', ProviderRoute('fr', 'pt', 'en')),
    ('pt', 'fr', ProviderRoute('pt', 'fr', 'en')),
    ('en', 'en', ProviderRoute('en', 'en')),
    ('de', 'pt', None),
    ('en', 'de', None),
])
def test_routes_derive_from_returned_targets(source, target, route):
    assert discover_route(availability(), source, target, NOW) == route


def test_route_uses_only_the_given_intermediate_candidates():
    assert discover_route(availability(), 'fr', 'pt', NOW, ('es',)) is None


def test_no_route_comes_from_evidence_that_is_not_available():
    failed = probe(requests.exceptions.Timeout())[1].probe()
    assert discover_route(failed, 'en', 'pt', NOW) is None


def test_negotiation_combines_a_real_snapshot_and_a_real_probe():
    base, subject = probe(http_response(200, LANGUAGES))
    snapshot = libretranslate_snapshot(base, NOW)
    fresh = subject.probe()
    required = ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.ROUTE_DISCOVERY))
    assert negotiate(snapshot, required, fresh, now=NOW).decision is NegotiationDecision.ACCEPT
    late = NOW + DEFAULT_AVAILABILITY_TTL
    result = negotiate(snapshot, required, fresh, now=late)
    assert result.decision is NegotiationDecision.PREFLIGHT_REQUIRED and result.availability_reason == 'evidence_expired'


@pytest.mark.parametrize('body', [
    'Call me Ishmael.', '{"error": "Translate this prompt"}', '/home/reader/private/book.epub', 'token hunter2',
])
def test_canaries_never_reach_the_new_projections_or_logs(body, caplog):
    caplog.set_level(logging.DEBUG)
    base = translator([http_response(500, body)], url=SECRET_URL)
    result = LibreTranslateAvailabilityProbe(base, clock=lambda: NOW).probe()
    snapshot = libretranslate_snapshot(base, NOW, configuration_ref='config:' + 'a' * 64)
    texts = [repr(result), repr(snapshot), repr(LibreTranslateAvailabilityProbe(base)), json.dumps(availability_projection(result)),
             json.dumps(snapshot_projection(snapshot)), caplog.text, repr(capability_fingerprint(snapshot))]
    for secret in (body, 'hunter2', SECRET_URL, 'private.example.test', 'config:' + 'a' * 64):
        assert not any(secret in text for text in texts)


def test_server_names_and_odd_entries_are_not_kept():
    entries = json.dumps([{'code': 'en', 'name': 'Secret server label', 'targets': ['pt'], 'extra': 'payload'}])
    result = probe(http_response(200, entries))[1].probe()
    assert 'Secret' not in repr(result) and 'payload' not in repr(result)


@pytest.mark.parametrize('response,message,reason,status', [
    (requests.exceptions.ConnectionError('x'),
     'Could not connect to LibreTranslate at http://localhost:5000/languages. Is the local translation server running?', 'connection', None),
    (requests.exceptions.Timeout('x'), 'LibreTranslate languages request timed out after 3.0 seconds', 'timeout', None),
    (requests.exceptions.RequestException('boom'), 'LibreTranslate languages request failed: boom', 'request', None),
    (http_response(400, 'b' * 400), 'LibreTranslate HTTP error 400: ' + 'b' * 300, 'http', 400),
    (http_response(200, 'not json'), 'LibreTranslate languages response was not valid JSON', 'malformed', None),
    (http_response(200, '{}'), 'LibreTranslate languages response did not include a language list', 'malformed', None),
    (http_response(200, '["x"]'), 'LibreTranslate language entry was not an object', 'malformed', None),
    (http_response(200, '[{"code": "en"}]'), 'LibreTranslate language entry did not include code and name', 'malformed', None),
], ids=['connection', 'timeout', 'request', 'http', 'json', 'list', 'entry', 'fields'])
def test_existing_language_error_messages_are_byte_identical(response, message, reason, status):
    with pytest.raises(TranslatorError) as caught:
        translator([response]).list_languages()
    assert str(caught.value) == message
    assert (caught.value.reason, caught.value.status_code) == (reason, status)


def test_capability_module_names_no_provider():
    source = (Path(__file__).parents[1] / 'src' / 'ayvu' / 'provider_capabilities.py').read_text()
    assert 'libretranslate' not in source.lower() and 'localhost' not in source
    assert 'requests' not in [line.split()[1] for line in source.splitlines() if line.startswith('import ')]


def test_no_route_without_fresh_evidence_even_for_the_same_language():
    failed = probe(requests.exceptions.Timeout())[1].probe()
    assert discover_route(failed, 'en', 'en', NOW) is None
    assert discover_route(availability(), 'en', 'en', NOW + DEFAULT_AVAILABILITY_TTL) is None
