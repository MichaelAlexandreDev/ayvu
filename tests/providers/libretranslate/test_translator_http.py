from __future__ import annotations

import requests
import pytest

from ayvu.providers.libretranslate.client import LibreTranslateTranslator, TranslatorError

from tests.providers.libretranslate.translator_support import FakeSession, make_response


def test_libretranslate_posts_payload_and_parses_response() -> None:
    session = FakeSession([make_response(200, '{"translatedText": "Ola"}')])
    translator = LibreTranslateTranslator(url="http://localhost:5000/", timeout=3.0, retries=0)
    translator.session = session

    result = translator.translate("Hello", "en", "pt")

    assert result == "Ola"
    assert session.posts == [
        (
            "http://localhost:5000/translate",
            {"q": "Hello", "source": "en", "target": "pt", "format": "text"},
            3.0,
        )
    ]


def test_libretranslate_returns_empty_text_without_http_call() -> None:
    session = FakeSession([])
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session

    assert translator.translate("", "en", "pt") == ""
    assert session.posts == []


def test_libretranslate_retries_5xx_before_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            make_response(503, "temporarily unavailable"),
            make_response(200, '{"translatedText": "Tudo certo"}'),
        ]
    )
    translator = LibreTranslateTranslator(retries=1)
    translator.session = session
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    result = translator.translate("All right", "en", "pt")

    assert result == "Tudo certo"
    assert len(session.posts) == 2
    assert sleeps == [0.5]


def test_libretranslate_retries_429_before_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            make_response(429, "too many requests"),
            make_response(200, '{"translatedText": "Tudo certo"}'),
        ]
    )
    translator = LibreTranslateTranslator(retries=1)
    translator.session = session
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    result = translator.translate("All right", "en", "pt")

    assert result == "Tudo certo"
    assert len(session.posts) == 2
    assert sleeps == [0.5]


def test_libretranslate_retries_any_5xx_before_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            make_response(508, "loop detected"),
            make_response(200, '{"translatedText": "Tudo certo"}'),
        ]
    )
    translator = LibreTranslateTranslator(retries=1)
    translator.session = session
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    result = translator.translate("All right", "en", "pt")

    assert result == "Tudo certo"
    assert len(session.posts) == 2
    assert sleeps == [0.5]


def test_libretranslate_uses_exponential_backoff_with_max(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            make_response(503, "temporarily unavailable"),
            make_response(503, "temporarily unavailable"),
            make_response(503, "temporarily unavailable"),
            make_response(200, '{"translatedText": "Tudo certo"}'),
        ]
    )
    translator = LibreTranslateTranslator(retries=3, retry_backoff=0.25, retry_backoff_max=0.5)
    translator.session = session
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    result = translator.translate("All right", "en", "pt")

    assert result == "Tudo certo"
    assert len(session.posts) == 4
    assert sleeps == [0.25, 0.5, 0.5]


def test_libretranslate_reports_http_error() -> None:
    session = FakeSession([make_response(400, "bad language pair")])
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session

    with pytest.raises(TranslatorError) as error:
        translator.translate("Hello", "en", "xx")

    assert str(error.value) == "LibreTranslate HTTP error 400"
    assert "bad language pair" not in str(error.value)


def test_libretranslate_reports_invalid_json_response() -> None:
    session = FakeSession([make_response(200, "not-json")])
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session

    with pytest.raises(TranslatorError) as error:
        translator.translate("Hello", "en", "pt")

    assert "LibreTranslate response was not valid JSON" in str(error.value)


def test_libretranslate_reports_missing_translated_text() -> None:
    session = FakeSession([make_response(200, '{"translatedText": 42}')])
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session

    with pytest.raises(TranslatorError) as error:
        translator.translate("Hello", "en", "pt")

    assert "LibreTranslate response did not include translatedText" in str(error.value)


def test_libretranslate_reports_connection_error_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            requests.exceptions.ConnectionError("refused"),
            requests.exceptions.ConnectionError("refused"),
        ]
    )
    translator = LibreTranslateTranslator(url="http://localhost:5000", retries=1)
    translator.session = session
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    with pytest.raises(TranslatorError) as error:
        translator.translate("Hello", "en", "pt")

    assert "Could not connect to LibreTranslate." in str(error.value)
    assert "http://localhost:5000" not in str(error.value)
    assert len(session.posts) == 2
    assert sleeps == [0.5]


def test_libretranslate_reports_timeout() -> None:
    session = FakeSession([requests.exceptions.Timeout("slow")])
    translator = LibreTranslateTranslator(timeout=1.5, retries=0)
    translator.session = session

    with pytest.raises(TranslatorError) as error:
        translator.translate("Hello", "en", "pt")

    assert "LibreTranslate request timed out after 1.5 seconds" in str(error.value)
