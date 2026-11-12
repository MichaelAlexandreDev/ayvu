"""Audit current recipe IDs against real builders without changing runtime selection.

These tests prove today's single provider/input recipe. They do not claim that
builders enforce exclusions or that desktop workflows have been implemented.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests

from ayvu.composition import cli
from ayvu.composition.builders import content, providers, review, translate_project
from ayvu.composition.desktop import desktop_surface
from ayvu.formats import FormatRegistry
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter
from ayvu.kernel import LanguagePair, TranslationOptions
from ayvu.providers.discovery import ProviderDiscoveryService
from ayvu.providers.libretranslate.adapter import LibreTranslateProvider
from ayvu.providers.libretranslate.client import LibreTranslateTranslator
from ayvu.translation import RuntimeContext
from ayvu.translation import preflight
from tests.composition.test_edition_surfaces import CASE_IDS, EDITION_IDS


def effective_surface(edition_id):
    return cli.cli_surface(edition_id) if edition_id.startswith("cli_") else desktop_surface(edition_id)


@pytest.mark.parametrize("edition_id", EDITION_IDS, ids=CASE_IDS)
def test_format_ids_match_content_review_and_translation_recipes(edition_id, monkeypatch):
    surface = effective_surface(edition_id)
    registrations = []
    register = FormatRegistry.register

    def capture(registry, registration, adapter):
        register(registry, registration, adapter)
        registrations.append((registration.adapter_id, adapter.descriptor.adapter_id))

    monkeypatch.setattr(FormatRegistry, "register", capture)
    cases = content.open_content_use_cases()
    review_formats = review.epub_review_formats()
    translation_formats = translate_project.epub_format_service()
    assert registrations == [("epub", "epub")] * 3
    for service in (cases.inspect._formats, review_formats, translation_formats):
        observed = tuple(item.adapter_id for item in service._registry.descriptors())
        assert observed == surface.input_formats == ("epub",)
    # The content builder still constructs its writer unconditionally. Only
    # surfaces offering extraction claim it; exclusion inside builders is batch E.
    assert type(cases.export_visible_content._writer) is MarkdownDirectoryWriter
    if surface.extraction_writers:
        assert surface.extraction_writers == ("markdown",)
    else:
        assert "extract" not in surface.actions
    assert surface.assistance_roles == ()


@pytest.mark.parametrize("edition_id", EDITION_IDS, ids=CASE_IDS)
def test_provider_id_matches_discovery_and_v1_preflight(edition_id, tmp_path, monkeypatch):
    surface = effective_surface(edition_id)
    registrations = []
    register = ProviderDiscoveryService.register

    def capture(service, instance, adapter):
        register(service, instance, adapter)
        registrations.append((instance.adapter_id, adapter.descriptor.adapter_id))

    def no_http(*args, **kwargs):
        raise AssertionError("HTTP session acquired during recipe audit")

    monkeypatch.setattr(ProviderDiscoveryService, "register", capture)
    monkeypatch.setattr(requests.Session, "__init__", no_http)
    session = providers.cli_provider_session(url="http://localhost:5000", timeout=30, retries=0, network_policy="none")
    assert tuple(item.adapter_id for item in session._service.list_providers()) == surface.providers
    session.languages()
    session.test("en", "pt")
    assert registrations == [("libretranslate", "libretranslate")]
    # Keep file validation and storage out of this recipe audit; constructors,
    # preflight orchestration and the legacy executor remain real.
    monkeypatch.setattr(preflight, "inspect_epub", lambda _: None)
    monkeypatch.setattr(cli, "verify_cache_writable", lambda _: None)
    prepared = cli.run_translation_preflight(epub_path=tmp_path / "book.epub", cache_path=tmp_path / "cache",
        glossary_path=None, translator_name=surface.providers[0], url="http://localhost:5000", timeout=30,
        retries=0, language_pair=LanguagePair("en", "pt"), dry_run=True, network_policy="none")
    assert type(prepared.translator) is LibreTranslateTranslator
    runtime = translate_project.LegacyTranslationRuntime(tmp_path / "book.epub",
        TranslationOptions(language_pair=LanguagePair("en", "pt")), lambda: prepared, lambda: nullcontext(object()))
    assert runtime.preflight() is None
    with runtime.resources() as resources:
        assert resources.executor._translator is prepared.translator
    prepared.translator.close()


@pytest.mark.parametrize("edition_id", EDITION_IDS, ids=CASE_IDS)
def test_provider_id_matches_real_rich_runtime_acquisition_and_cleanup(edition_id, tmp_path, monkeypatch):
    surface = effective_surface(edition_id)
    acquired, closed = [], []
    now = datetime(2026, 11, 12, tzinfo=UTC)
    translator = LibreTranslateTranslator(network_policy="none")
    monkeypatch.setattr(translator, "close", lambda: closed.append("translator"))
    monkeypatch.setattr(translate_project, "open_artifact_service", lambda *a, **kw: object())
    monkeypatch.setattr(translate_project, "open_provenance_repository", lambda *a, **kw: object())

    def capture(*args, **kwargs):
        provider = LibreTranslateProvider(*args, **kwargs)
        acquired.append(provider)
        return provider

    monkeypatch.setattr(translate_project, "LibreTranslateProvider", capture)
    runtime = translate_project.RichTranslationRuntime(tmp_path / "book.epub",
        TranslationOptions(language_pair=LanguagePair("en", "pt")), lambda: None, tmp_path / "cache",
        "audit-model", tmp_path / "project.sqlite", tmp_path / "backups", tmp_path / "artifacts",
        "00000000-0000-0000-0000-000000000001", clock=lambda: now)
    runtime._prepared = translate_project.LegacyResources(translator)
    with runtime.resources(RuntimeContext(None, None, SimpleNamespace())) as resources:
        assert resources.prepare_document is not None
        assert tuple(provider._producer.provider for provider in acquired) == surface.providers == ("libretranslate",)
        assert acquired[0]._translator is translator
        assert closed == []
    assert closed == ["translator"]
