import pytest

from ayvu.cache import CacheKey, TranslationCache
from ayvu.domain import LanguagePair
from ayvu.preflight import PreflightError, run_translation_preflight
from ayvu.translator import RequestRateLimiter, RoutedTranslator, TranslatorError, TranslatorLanguage


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        return text


class FailingTranslator:
    def translate(self, _text: str, _source: str, _target: str) -> str:
        raise TranslatorError("language pair is not available")


class TranslatorWithLanguages:
    timeout, retries, rate_limiter = 1.0, 0, RequestRateLimiter()

    def __init__(self, languages: tuple[TranslatorLanguage, ...]) -> None:
        self._languages = languages
        self.translate_calls: list[tuple[str, str, str]] = []
        self.list_calls = 0

    def translate(self, text: str, source: str, target: str) -> str:
        self.translate_calls.append((text, source, target))
        return text

    def list_languages(self) -> tuple[TranslatorLanguage, ...]:
        self.list_calls += 1
        return self._languages


class TranslatorWithFailingLanguages:
    timeout, retries, rate_limiter = 1.0, 0, RequestRateLimiter()

    def translate(self, text: str, _source: str, _target: str) -> str:
        return text

    def list_languages(self) -> tuple[TranslatorLanguage, ...]:
        raise TranslatorError("connection refused")


def raise_bad_epub(_path: object) -> object:
    raise ValueError("bad epub")


def test_preflight_checks_cache_epub_and_translator(monkeypatch, tmp_path):
    translator = FakeTranslator()
    cache_path = tmp_path / "cache.sqlite"
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=cache_path,
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=False,
    )

    probe_key = CacheKey(
        text="__ayvu_cache_write_check__",
        language_pair=LanguagePair(source="ayvu", target="ayvu"),
    )
    with TranslationCache(cache_path) as cache:
        assert cache.get(probe_key) is None
    assert result.translator is translator
    # No discovery available means an unknown route, never a hidden translation.
    assert translator.calls == []
    assert result.route is None


def test_preflight_dry_run_skips_translator_probe(monkeypatch, tmp_path):
    translator = FakeTranslator()
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=True,
    )

    assert translator.calls == []


def test_preflight_passes_execution_controls_to_translator_factory(monkeypatch, tmp_path):
    translator = FakeTranslator()
    calls: dict[str, object] = {}

    def fake_create_translator(*args: object, **kwargs: object) -> FakeTranslator:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return translator

    monkeypatch.setattr("ayvu.preflight.create_translator", fake_create_translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        requests_per_second=3.5,
        retry_backoff=0.25,
        retry_backoff_max=2.0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=True,
    )

    assert calls["args"] == ("libretranslate",)
    kwargs = calls["kwargs"]
    assert isinstance(kwargs.pop("rate_limiter"), RequestRateLimiter)
    assert kwargs == {
        "url": "http://localhost:5000",
        "timeout": 1.0,
        "retries": 0,
        "requests_per_second": 3.5,
        "retry_backoff": 0.25,
        "retry_backoff_max": 2.0,
    }


def test_preflight_cache_only_skips_route_resolution(monkeypatch, tmp_path):
    translator = TranslatorWithLanguages(
        (TranslatorLanguage(code="en", name="English", targets=("pt",)),)
    )
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: pytest.fail("cache-only constructed a provider"))
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=False,
        cache_only=True,
    )

    assert result.route is None
    assert result.translator is None
    assert result.translator_factory is None
    assert translator.translate_calls == []
    assert translator.list_calls == 0


def test_preflight_rejects_blank_language_pair(tmp_path):
    with pytest.raises(PreflightError) as error:
        run_translation_preflight(
            epub_path=tmp_path / "book.epub",
            cache_path=tmp_path / "cache.sqlite",
            glossary_path=None,
            translator_name="libretranslate",
            url="http://localhost:5000",
            timeout=1.0,
            retries=0,
            language_pair=LanguagePair(source="en", target=" "),
            dry_run=True,
        )

    assert error.value.summary == "O par de idiomas informado não é válido."
    assert "--source e --target" in error.value.next_step


def test_preflight_never_translates_to_find_out_whether_a_translator_works(monkeypatch, tmp_path):
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: FailingTranslator())
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=False,
    )

    assert result.route is None


def test_preflight_reports_epub_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: FakeTranslator())
    monkeypatch.setattr("ayvu.preflight.inspect_epub", raise_bad_epub)

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
            dry_run=False,
        )

    assert error.value.summary == "Não foi possível ler o EPUB informado."
    assert "bad epub" in error.value.detail
    assert "EPUB válido e legível" in error.value.next_step


def test_preflight_uses_languages_to_resolve_direct_route(monkeypatch, tmp_path):
    languages = (
        TranslatorLanguage(code="en", name="English", targets=("pt",)),
        TranslatorLanguage(code="pt", name="Portuguese", targets=("en",)),
    )
    translator = TranslatorWithLanguages(languages)
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=False,
    )

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
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="fr", target="pt"),
        dry_run=False,
    )

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

    monkeypatch.setattr("ayvu.preflight.create_translator", fake_create_translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="fr", target="pt"),
        dry_run=False,
    )

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
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

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
            dry_run=False,
        )

    assert error.value.summary == "O par de idiomas não está disponível no tradutor."
    assert "ayvu languages" in error.value.next_step


def test_preflight_reports_translator_unreachable_when_list_languages_fails(monkeypatch, tmp_path):
    translator = TranslatorWithFailingLanguages()
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())

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
            dry_run=False,
        )

    assert error.value.summary == "O tradutor não respondeu."
    assert "connection refused" not in error.value.detail
    assert error.value.detail == "provider_request_failed"
    assert "http://localhost:5000" not in error.value.next_step


def _preflight(tmp_path, translator, monkeypatch, source="en", target="pt", url="http://localhost:5000"):
    monkeypatch.setattr("ayvu.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.preflight.inspect_epub", lambda _path: object())
    return run_translation_preflight(
        epub_path=tmp_path / "book.epub", cache_path=tmp_path / "cache.sqlite", glossary_path=None,
        translator_name="libretranslate", url=url, timeout=1.0, retries=0,
        language_pair=LanguagePair(source=source, target=target), dry_run=False,
    )


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


def test_cli_network_decision_states_that_no_policy_ran():
    from datetime import UTC, datetime

    from ayvu.provider_composition import CLI_INSTANCE_ID, CliProviderSession
    from ayvu.provider_network import NetworkClass, ProviderOperation, network_binding

    session = CliProviderSession(TranslatorWithLanguages(()), clock=lambda: datetime(2026, 11, 3, tzinfo=UTC))
    decision = session._decision(ProviderOperation.LANGUAGES)
    assert decision.allowed is True and decision.network_class is NetworkClass.UNCLASSIFIED
    assert decision.reason_code == "legacy_cli_unrestricted" and decision.policy_ref is None
    assert decision.binding == network_binding(CLI_INSTANCE_ID, None, ProviderOperation.LANGUAGES)


def test_a_malformed_code_from_discovery_is_not_a_route():
    from datetime import UTC, datetime, timedelta

    from ayvu.provider_capabilities import AvailabilityState, LanguageEvidence, observation
    from ayvu.provider_libretranslate import discover_route

    now = datetime(2026, 11, 3, tzinfo=UTC)
    available = observation(AvailabilityState.AVAILABLE, None, now, timedelta(seconds=60), (LanguageEvidence("en", ("pt",)),))
    assert discover_route(available, "x y", "x y", now) is None
