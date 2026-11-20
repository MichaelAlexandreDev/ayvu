import pytest
from ayvu.translation.preflight import PreflightError, run_translation_preflight
from ayvu.providers.libretranslate.client import RequestRateLimiter, TranslatorLanguage
from ayvu.kernel import LanguagePair
from tests.translation.preflight_support import accept_cache, FailingTranslator, FakeTranslator, TranslatorWithLanguages
from tests.translation.preflight_support import raise_bad_epub


def test_preflight_checks_cache_epub_and_translator(monkeypatch, tmp_path):
    translator = FakeTranslator()
    cache_path = tmp_path / "cache.sqlite"
    checked = []
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    result = run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=cache_path,
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=False, cache_check=checked.append)

    assert checked == [cache_path]
    assert result.translator is translator
    # No discovery available means an unknown route, never a hidden translation.
    assert translator.calls == []
    assert result.route is None


def test_preflight_dry_run_skips_translator_probe(monkeypatch, tmp_path):
    translator = FakeTranslator()
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

    run_translation_preflight(
        epub_path=tmp_path / "book.epub",
        cache_path=tmp_path / "cache.sqlite",
        glossary_path=None,
        translator_name="libretranslate",
        url="http://localhost:5000",
        timeout=1.0,
        retries=0,
        language_pair=LanguagePair(source="en", target="pt"),
        dry_run=True, cache_check=accept_cache)

    assert translator.calls == []


def test_preflight_passes_execution_controls_to_translator_factory(monkeypatch, tmp_path):
    translator = FakeTranslator()
    calls: dict[str, object] = {}

    def fake_create_translator(*args: object, **kwargs: object) -> FakeTranslator:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return translator

    monkeypatch.setattr("ayvu.translation.preflight.create_translator", fake_create_translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())

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
        dry_run=True, cache_check=accept_cache)

    assert calls["args"] == ("libretranslate",)
    kwargs = calls["kwargs"]
    assert isinstance(kwargs.pop("rate_limiter"), RequestRateLimiter)
    from ayvu.network.http import NetworkAuthorization
    assert isinstance(kwargs.pop("network_authorization"), NetworkAuthorization)
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
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: pytest.fail("cache-only constructed a provider"))
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
        dry_run=False,
        cache_only=True, cache_check=accept_cache)

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
            dry_run=True, cache_check=accept_cache)

    assert error.value.summary == "O par de idiomas informado não é válido."
    assert "--source e --target" in error.value.next_step


def test_preflight_never_translates_to_find_out_whether_a_translator_works(monkeypatch, tmp_path):
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: FailingTranslator())
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
        dry_run=False, cache_check=accept_cache)

    assert result.route is None


def test_preflight_reports_epub_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: FakeTranslator())
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", raise_bad_epub)

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
            dry_run=False, cache_check=accept_cache)

    assert error.value.summary == "Não foi possível ler o EPUB informado."
    assert "bad epub" in error.value.detail
    assert "EPUB válido e legível" in error.value.next_step
