from __future__ import annotations

import pytest

from ayvu.providers.libretranslate.client import LibreTranslateTranslator, TranslatorError

from tests.providers.libretranslate.translator_support import FakeSession, make_response


def test_libretranslate_lists_languages_from_base_url() -> None:
    session = FakeSession(
        get_responses=[
            make_response(
                200,
                '[{"code": "en", "name": "English", "targets": ["pt", "es"]},'
                ' {"code": "pt", "name": "Portuguese", "targets": ["en"]}]',
            )
        ]
    )
    translator = LibreTranslateTranslator(url="http://localhost:5000/translate", timeout=2.0, retries=0)
    translator.session = session

    languages = translator.list_languages()

    assert session.gets == [("http://localhost:5000/languages", 2.0)]
    assert [language.code for language in languages] == ["en", "pt"]
    assert [language.name for language in languages] == ["English", "Portuguese"]
    assert languages[0].targets == ("pt", "es")
    assert languages[0].state == "installed"


def test_libretranslate_retries_languages_5xx_before_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        get_responses=[
            make_response(503, "temporarily unavailable"),
            make_response(200, '[{"code": "pt", "name": "Portuguese", "targets": ["en"]}]'),
        ]
    )
    translator = LibreTranslateTranslator(retries=1)
    translator.session = session
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    languages = translator.list_languages()

    assert [language.code for language in languages] == ["pt"]
    assert len(session.gets) == 2
    assert sleeps == [0.5]


def test_libretranslate_reports_invalid_languages_json() -> None:
    session = FakeSession(get_responses=[make_response(200, "not-json")])
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session

    with pytest.raises(TranslatorError) as error:
        translator.list_languages()

    assert "LibreTranslate languages response was not valid JSON" in str(error.value)


def test_libretranslate_reports_languages_http_error() -> None:
    session = FakeSession(get_responses=[make_response(500, "broken")])
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session

    with pytest.raises(TranslatorError) as error:
        translator.list_languages()

    assert str(error.value) == "LibreTranslate HTTP error 500"
    assert "broken" not in str(error.value)
