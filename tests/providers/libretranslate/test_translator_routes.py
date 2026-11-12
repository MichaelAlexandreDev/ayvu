from __future__ import annotations

import pytest

from ayvu.providers.libretranslate.client import RouteResolutionError, RoutedTranslator, TranslationRoute, TranslatorLanguage, resolve_translation_route

from tests.providers.libretranslate.translator_support import RecordingTranslator, _language


def test_resolve_translation_route_finds_direct_route() -> None:
    languages = (_language("en", ("pt", "es")), _language("pt", ("en",)))

    route = resolve_translation_route(languages, "en", "pt")

    assert route.is_direct
    assert route.describe() == "en -> pt"


def test_resolve_translation_route_uses_english_bridge_when_direct_missing() -> None:
    languages = (
        _language("fr", ("en",)),
        _language("en", ("pt",)),
        _language("pt", ("en",)),
    )

    route = resolve_translation_route(languages, "fr", "pt")

    assert not route.is_direct
    assert route.intermediate == "en"
    assert route.describe() == "fr -> en -> pt"


def test_resolve_translation_route_raises_when_no_route_available() -> None:
    languages = (_language("fr", ("de",)), _language("pt", ()))

    with pytest.raises(RouteResolutionError):
        resolve_translation_route(languages, "fr", "pt")


def test_resolve_translation_route_returns_direct_when_source_equals_target() -> None:
    languages: tuple[TranslatorLanguage, ...] = ()

    route = resolve_translation_route(languages, "pt", "pt")

    assert route.is_direct


def test_routed_translator_forwards_single_call_for_direct_route() -> None:
    base = RecordingTranslator()
    route = TranslationRoute(source="en", target="pt")
    translator = RoutedTranslator(base, route)

    result = translator.translate("Hello", "en", "pt")

    assert result == "Hello|PT"
    assert base.calls == [("Hello", "en", "pt")]


def test_routed_translator_chains_through_intermediate_language() -> None:
    base = RecordingTranslator()
    route = TranslationRoute(source="fr", target="pt", intermediate="en")
    translator = RoutedTranslator(base, route)

    result = translator.translate("Bonjour", "fr", "pt")

    assert base.calls == [("Bonjour", "fr", "en"), ("Bonjour|EN", "en", "pt")]
    assert result == "Bonjour|EN|PT"


def test_routed_translator_bypasses_intermediate_for_other_pairs() -> None:
    base = RecordingTranslator()
    route = TranslationRoute(source="fr", target="pt", intermediate="en")
    translator = RoutedTranslator(base, route)

    translator.translate("Hello", "en", "pt")

    assert base.calls == [("Hello", "en", "pt")]
