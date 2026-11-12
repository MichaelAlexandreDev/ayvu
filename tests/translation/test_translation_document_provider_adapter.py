from __future__ import annotations
import pytest
import requests
from ayvu.artifacts.model import ArtifactState
from ayvu.content.model import ProtectedPart
from ayvu.providers.capabilities import Capability, ProviderRequirements
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ayvu.translation.document_contracts import TranslationExecutionError
from ayvu.translation.raw_results import RAW_RESULTS_ROLE
from ayvu.knowledge.cache.cache_only import CacheOnlyRequest
from ayvu.translation.plan import resolve_plan
from ayvu.providers.libretranslate.client import LibreTranslateTranslator, RoutedTranslator, TranslationRoute
from tests.support.provider_fakes import FakeSession, http_response
from ayvu.kernel import LanguagePair
from tests.support.translation_document_support import GLOSSARY_A, Glossed, NOW
from tests.support.translation_document_support import ROUTE
from tests.support.translation_document_support import cue, document, env
from tests.support.paths import PROJECT_ROOT


def test_the_production_libretranslate_adapter_runs_the_rich_path_over_fake_http(env):
    base = LibreTranslateTranslator(url='http://localhost:5000', retries=0)
    base.session = FakeSession([http_response(200, '{"translatedText": "Ola"}'), http_response(200, '{"translatedText": "Boa noite"}')])
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements((Capability.PLAIN_TEXT_INPUT,)),
                                LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000, glossary_ref=GLOSSARY_A)
    result = env.translate(provider=LibreTranslateProvider(base, clock=env.clock), subject_plan=subject_plan, glossary=Glossed)
    assert [unit.text for unit in result.units] == [' Ola ', 'Boa noite']
    assert [post[1] for post in base.session.posts] == [
        {'q': 'Hello there', 'source': 'en', 'target': 'pt', 'format': 'text'},
        {'q': 'Good night', 'source': 'en', 'target': 'pt', 'format': 'text'},
    ]
    assert base.session.gets == []


def test_the_production_adapter_failure_reaches_the_caller_without_its_message(env):
    # A permitted destination is necessary to reach the protocol failure under #159.
    base = LibreTranslateTranslator(url='http://localhost:5000', retries=0)
    base.session = FakeSession([http_response(500, 'Call me Ishmael.')])
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements(()), LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000)
    with pytest.raises(TranslationExecutionError, match='^provider_server_error$') as caught:
        env.translate(provider=LibreTranslateProvider(base, clock=env.clock), subject_plan=subject_plan)
    assert 'hunter2' not in repr(caught.value) and 'Ishmael' not in repr(caught.value.failure)


def test_the_production_adapter_refuses_url_credentials_before_translation(env):
    base = LibreTranslateTranslator(url='http://user:hunter2@private.example.test', retries=0)
    base.session = FakeSession([])
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements(()), LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000)
    with pytest.raises(TranslationExecutionError, match='^userinfo_unsupported$') as caught:
        env.translate(provider=LibreTranslateProvider(base, clock=env.clock), subject_plan=subject_plan)
    assert base.session.posts == [] and base.session.gets == []
    assert 'hunter2' not in repr(caught.value)


def test_a_bridge_route_must_match_the_plan_through_the_production_adapter(env):
    base = LibreTranslateTranslator(url='http://localhost:5000', retries=0)
    base.session = FakeSession([])
    routed = RoutedTranslator(base, TranslationRoute('en', 'pt', 'es'))
    subject_plan = resolve_plan(libretranslate_snapshot(base, NOW), ProviderRequirements(()), LIBRETRANSLATE_PRODUCER, ROUTE, chunk_limit=3000)
    with pytest.raises(TranslationExecutionError, match='^route_conflict$'):
        env.translate(provider=LibreTranslateProvider(routed, clock=env.clock), subject_plan=subject_plan)
    assert base.session.posts == [] and ArtifactState.READY


def test_the_facade_remains_free_of_durable_state():
    from pathlib import Path
    root = PROJECT_ROOT / 'src' / 'ayvu'
    for name in ('translation/pipeline.py',):
        source = (root / name).read_text()
        assert 'translation_document' not in source and 'from .artifact' not in source and 'from .provenance' not in source
    assert RAW_RESULTS_ROLE == 'raw_results' and LanguagePair(source='en', target='pt') and requests


@pytest.mark.parametrize('options', [{'dry_run': True}, {'cache_only': CacheOnlyRequest.resolve_compatible()}],
                         ids=['dry-run', 'cache-only'])
def test_an_untranslated_unit_returns_its_exact_source_with_protected_parts(env, options):
    subject = document(cue('a', ' Say <b> now ', 1, protected_parts=(ProtectedPart(5, 8, 'markup'),)))
    assert env.translate(subject, **options).units[0].text == ' Say <b> now '
