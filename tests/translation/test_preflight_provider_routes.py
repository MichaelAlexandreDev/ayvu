import pytest
from ayvu.v1.cache_check import verify_cache_writable
from ayvu.translation.preflight import PreflightError, run_translation_preflight
from ayvu.providers.libretranslate.client import RoutedTranslator, TranslatorLanguage
from ayvu.kernel import LanguagePair
from tests.translation.preflight_support import TranslatorWithFailingLanguages, TranslatorWithLanguages, _preflight


def test_preflight_uses_languages_to_resolve_direct_route(monkeypatch, tmp_path):
    languages = (
        TranslatorLanguage(code="en", name="English", targets=("pt",)),
        TranslatorLanguage(code="pt", name="Portuguese", targets=("en",)),
    )
    translator = TranslatorWithLanguages(languages)
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=False, cache_check=verify_cache_writable)

    assert translator.list_calls == 1
    assert translator.translate_calls == []
    assert result.route is not None and result.route.is_direct
    assert result.translator is translator


def test_preflight_wraps_translator_when_intermediate_route_is_needed(monkeypatch, tmp_path):
    languages = (
        TranslatorLanguage(code="fr", name="French", targets=("en",)),
        TranslatorLanguage(code="en", name="English", targets=("pt",)),
        TranslatorLanguage(code="pt", name="Portuguese", targets=("en",)),
    )
    translator = TranslatorWithLanguages(languages)
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="fr", target="pt"),
        dry_run=False, cache_check=verify_cache_writable)

    assert isinstance(result.translator, RoutedTranslator)
    assert result.route is not None
    assert result.route.intermediate == "en"


def test_preflight_translator_factory_preserves_intermediate_route(monkeypatch, tmp_path):
    languages = (
        TranslatorLanguage(code="fr", name="French", targets=("en",)),
        TranslatorLanguage(code="en", name="English", targets=("pt",)),
        TranslatorLanguage(code="pt", name="Portuguese", targets=("en",)),
    )
    created: list[TranslatorWithLanguages] = []

    def fake_create_translator(*_args: object, **_kwargs: object) -> TranslatorWithLanguages:
        translator = TranslatorWithLanguages(languages)
        created.append(translator)
        return translator

    monkeypatch.setattr("ayvu.translation.preflight.create_translator", fake_create_translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="fr", target="pt"),
        dry_run=False, cache_check=verify_cache_writable)

    assert result.translator_factory is not None
    worker_translator = result.translator_factory()
    assert isinstance(worker_translator, RoutedTranslator)
    assert len(created) == 2


def test_preflight_reports_missing_route_with_clear_message(monkeypatch, tmp_path):
    languages = (
        TranslatorLanguage(code="fr", name="French", targets=("de",)),
        TranslatorLanguage(code="pt", name="Portuguese", targets=()),
    )
    translator = TranslatorWithLanguages(languages)
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    with pytest.raises(PreflightError) as error:
        run_translation_preflight(
            epub_path=tmp_path / "book.epub",
            cache_path=tmp_path / "cache.sqlite",
            glossary_path=None,
            translator_name="libretranslate",
            url="http://localhost:5000",
            timeout=1.0,
            retries=0,
            language_pair=LanguagePair(source="fr", target="pt"),
            dry_run=False, cache_check=verify_cache_writable)

    assert error.value.summary == "O par de idiomas não está disponível no tradutor."
    assert "ayvu languages" in error.value.next_step


def test_preflight_reports_translator_unreachable_when_list_languages_fails(monkeypatch, tmp_path):
    translator = TranslatorWithFailingLanguages()
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    with pytest.raises(PreflightError) as error:
        run_translation_preflight(
            epub_path=tmp_path / "book.epub",
            cache_path=tmp_path / "cache.sqlite",
            glossary_path=None,
            translator_name="libretranslate",
            url="http://localhost:5000",
            timeout=1.0,
            retries=0,
            language_pair=LanguagePair(source="en", target="pt"),
            dry_run=False, cache_check=verify_cache_writable)

    assert error.value.summary == "O tradutor não respondeu."
    assert "connection refused" not in error.value.detail
    assert error.value.detail == "provider_request_failed"
    assert "http://localhost:5000" not in error.value.next_step


def test_preflight_discovers_routes_without_sending_any_text(monkeypatch, tmp_path):
    translator = TranslatorWithLanguages((
        TranslatorLanguage(code="fr", name="French", targets=("en",)), TranslatorLanguage(code="en", name="English", targets=("pt",)),
    ))
    result = _preflight(tmp_path, translator, monkeypatch, source="fr")
    assert (result.route.source, result.route.intermediate, result.route.target) == ("fr", "en", "pt")
    assert translator.translate_calls == [] and translator.list_calls == 1


def test_preflight_empty_language_list_is_a_missing_pair_except_for_the_same_language(monkeypatch, tmp_path):
    translator = TranslatorWithLanguages(())
    with pytest.raises(PreflightError) as error:
        _preflight(tmp_path, translator, monkeypatch)
    assert error.value.summary == "O par de idiomas não está disponível no tradutor."
    assert _preflight(tmp_path, TranslatorWithLanguages(()), monkeypatch, target="en").route.is_direct


def test_preflight_never_repeats_credentials_from_the_url(monkeypatch, tmp_path):
    with pytest.raises(PreflightError) as error:
        _preflight(tmp_path, TranslatorWithFailingLanguages(), monkeypatch, url="http://user:hunter2@host:5000")
    assert "hunter2" not in error.value.next_step and "hunter2" not in error.value.detail


def test_preflight_accepts_underscored_codes_a_server_may_report(monkeypatch, tmp_path):
    translator = TranslatorWithLanguages((TranslatorLanguage(code="pt_BR", name="Portuguese", targets=("en",)),))
    assert _preflight(tmp_path, translator, monkeypatch, source="pt_BR", target="en").route.is_direct


def test_preflight_next_step_is_exact_and_holds_no_address(monkeypatch, tmp_path):
    with pytest.raises(PreflightError) as error:
        _preflight(tmp_path, TranslatorWithFailingLanguages(), monkeypatch)
    assert error.value.next_step == "Inicie o LibreTranslate, verifique --url e confirme que o servidor está acessível."


def test_cli_network_decision_records_the_loopback_compatibility_default():
    from datetime import UTC, datetime

    from ayvu.providers.session import CLI_INSTANCE_ID, CliProviderSession
    from ayvu.providers.network import NetworkClass, ProviderOperation, network_binding

    session = CliProviderSession(TranslatorWithLanguages(()), clock=lambda: datetime(2026, 11, 3, tzinfo=UTC))
    decision = session._decision(ProviderOperation.LANGUAGES)
    assert decision.allowed is True and decision.network_class is NetworkClass.LOOPBACK_ONLY
    assert decision.reason_code == "loopback_permitted" and decision.policy_ref is None
    assert session.network_decision.defaulted is True
    assert decision.binding == network_binding(CLI_INSTANCE_ID, None, ProviderOperation.LANGUAGES)


def test_a_malformed_code_from_discovery_is_not_a_route():
    from datetime import UTC, datetime, timedelta

    from ayvu.providers.capabilities import AvailabilityState, LanguageEvidence, observation
    from ayvu.providers.libretranslate.adapter import discover_route

    now = datetime(2026, 11, 3, tzinfo=UTC)
    available = observation(AvailabilityState.AVAILABLE, None, now, timedelta(seconds=60), (LanguageEvidence("en", ("pt",)),))
    assert discover_route(available, "x y", "x y", now) is None
