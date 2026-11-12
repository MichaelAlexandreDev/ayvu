from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import logging
from threading import Barrier
import pytest
import requests
from ayvu.content.model import Constraint, ProtectedPart
from ayvu.jobs.execution import CancellationSource
from ayvu.provenance.model import NOT_APPLICABLE, UNKNOWN
from ayvu.providers.ports import (
    ConstraintsFeature, ContextFeature, ContextItem, DeadlineFeature, DeliveryState, ProtectedPartsFeature, ProviderRoute,
    ProviderStatus, Requirement, request_diagnostics, result_diagnostics,
)
from ayvu.providers.libretranslate.adapter import (
    ADAPTER_ID, ADAPTER_VERSION, LIBRETRANSLATE_SAFE_POINTS, LibreTranslateProvider, LibreTranslateProviderFactory,
)
from ayvu.providers.service import ProviderInstance, ProviderRegistry, execute
from ayvu.providers.libretranslate.client import (
    LibreTranslateTranslator, RequestRateLimiter, RoutedTranslator, TranslationRoute, Translator, TranslatorError,
)
from tests.support.provider_fakes import FakeSession, http_response, request
from ayvu.kernel import JobFailureFamily

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
