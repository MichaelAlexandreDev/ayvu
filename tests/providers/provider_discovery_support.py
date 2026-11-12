from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
from uuid import UUID
import pytest
import requests
from ayvu.jobs.execution import CancellationSource
from ayvu.provenance.model import ProducerIdentity
from ayvu.providers.ports import DeliveryState, ProviderRoute, ProviderStatus, TranslationProviderResult
from ayvu.providers.capabilities import (
    NOT_PROBED, AvailabilityState, Capability, CapabilityFact, EvidenceSource, LanguageEvidence, NegotiationDecision,
    ProviderAvailability, ProviderCapabilitySnapshot, ProviderDescriptor, ProviderRequirements, SupportState, observation,
)
from ayvu.providers.discovery import (
    DISCOVERY_SAFE_POINT, SYNTHETIC_TEST_TEXT, DiscoveredLanguage, DiscoveryObservation, DiscoveryOutcome, Freshness,
    PolicyEligibility, ProviderDiscoveryError, ProviderDiscoveryService,
)
from ayvu.providers.libretranslate.adapter import ADAPTER_ID, ADAPTER_VERSION, LibreTranslateDiscoveryAdapter
from ayvu.providers.network import (
    NetworkClass, NetworkDecisionError, ProviderOperation, ResolvedProviderNetworkDecision, network_binding,
)
from ayvu.providers.service import ProviderInstance
from ayvu.providers.libretranslate.client import LibreTranslateTranslator
from tests.support.provider_fakes import FakeSession, http_response
from ayvu.kernel import JobFailure, JobFailureFamily

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


REFERENCE = 'config:' + 'a' * 64


ISSUER = 'test_composition'


PRODUCER = ProducerIdentity('fake_engine@1', 'fake_model@1', 'fake_adapter@1', 'not_applicable')


DESCRIPTOR = ProviderDescriptor('fake_engine', 3, 'fake_protocol', 1, (1,), (1, 2))


EVIDENCE = {SupportState.SUPPORTED: EvidenceSource.ADAPTER_CODE, SupportState.UNSUPPORTED: EvidenceSource.ADAPTER_CODE,
            SupportState.UNKNOWN: EvidenceSource.NONE, SupportState.CONFIGURATION_DEPENDENT: EvidenceSource.CONFIGURATION}


TTL = timedelta(seconds=60)


LANGUAGES = '[{"code": "en", "name": "English", "targets": ["pt", "fr"]}, {"code": "fr", "name": "French", "targets": ["en"]}, {"code": "pt", "name": "Portuguese", "targets": ["en"]}]'


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeProvider:
    def __init__(self, adapter):
        self.adapter = adapter

    def execute(self, request):
        self.adapter.executed.append(request)
        return TranslationProviderResult(request.request_id, ProviderStatus.SUCCEEDED, PRODUCER, DeliveryState.COMPLETED, text='Ola')

    def close(self):
        pass


class FakeAdapter:
    """A second provider, whose evidence is nothing like /languages, using only the port."""

    descriptor = DESCRIPTOR

    def __init__(self, clock, *, state=AvailabilityState.AVAILABLE, reason=None, states=None):
        self.clock, self.state, self.reason = clock, state, reason
        self.states = states or {}
        self.observed = 0
        self.executed = []

    def snapshot(self):
        facts = tuple(CapabilityFact(item, state, EVIDENCE[state]) for item in Capability
                      for state in [self.states.get(item, SupportState.SUPPORTED)])
        return ProviderCapabilitySnapshot(DESCRIPTOR, facts, (), self.clock())

    def observe(self):
        self.observed += 1
        if self.state is AvailabilityState.AVAILABLE:
            evidence = (LanguageEvidence('en', ('pt',)),)
            return DiscoveryObservation(observation(self.state, None, self.clock(), TTL, evidence), (DiscoveredLanguage('en', 'Inglês', ('pt',)),))
        return DiscoveryObservation(observation(self.state, self.reason, self.clock(), TTL))

    def route(self, availability, source, target, now, candidates):
        return ProviderRoute(source, target) if (source, target) == ('en', 'pt') else None

    def provider(self):
        return FakeProvider(self)


INSTANCE = ProviderInstance('fake_local', 'fake_engine', 3, 1, REFERENCE)


def decision(operation=ProviderOperation.AVAILABILITY, **changes):
    values = {'allowed': True, 'network_class': NetworkClass.UNCLASSIFIED, 'reason_code': 'test_allowed',
              'binding': network_binding('fake_local', REFERENCE, operation), 'policy_revision': 1, 'issuer': ISSUER,
              'issued_at': NOW, 'expires_at': NOW + timedelta(minutes=5), **changes}
    return ResolvedProviderNetworkDecision(**values)


def service(adapter=None, clock=None, instance=INSTANCE):
    clock = clock or Clock()
    subject = ProviderDiscoveryService(trusted_issuers=frozenset({ISSUER}), policy_revision=1, clock=clock,
                                       new_id=lambda: str(UUID(int=42)))
    adapter = adapter or FakeAdapter(clock)
    subject.register(instance, adapter)
    return subject, adapter, clock


class BrokenAdapter(FakeAdapter):
    def observe(self):
        raise RuntimeError('https://user:hunter2@example.test Call me Ishmael.')


class OddAdapter(FakeAdapter):
    def observe(self):
        return 'available'


def preflight(subject, requirements=(), source='en', target='pt', **options):
    return subject.preflight('fake_local', decision(ProviderOperation.PREFLIGHT), ProviderRequirements(tuple(requirements)), source, target, **options)


def libre(get_responses=(), posts=(), url='http://localhost:5000'):
    base = LibreTranslateTranslator(url=url, timeout=3.0, retries=0)
    base.session = FakeSession(posts, get_responses)
    clock = Clock()
    subject = ProviderDiscoveryService(trusted_issuers=frozenset({ISSUER}), policy_revision=1, clock=clock, new_id=lambda: str(UUID(int=42)))
    subject.register(ProviderInstance('libre_local', ADAPTER_ID, ADAPTER_VERSION, 1), LibreTranslateDiscoveryAdapter(base, clock=clock))
    return subject, base


def libre_decision(operation):
    return decision(operation, binding=network_binding('libre_local', None, operation))
