from __future__ import annotations

import requests
import pytest

from ayvu.translator import (
    LibreTranslateTranslator,
    RequestRateLimiter,
    RouteResolutionError,
    RoutedTranslator,
    TranslationRoute,
    Translator,
    TranslatorError,
    TranslatorLanguage,
    resolve_translation_route,
)


class FakeSession:
    def __init__(
        self,
        responses: list[requests.Response | requests.exceptions.RequestException] | None = None,
        get_responses: list[requests.Response | requests.exceptions.RequestException] | None = None,
    ) -> None:
        self.responses = responses or []
        self.get_responses = get_responses or []
        self.gets: list[tuple[str, float]] = []
        self.posts: list[tuple[str, dict[str, str], float]] = []

    def get(self, url: str, *, timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
        self.gets.append((url, timeout))
        response = self.get_responses.pop(0)
        if isinstance(response, requests.exceptions.RequestException):
            raise response
        return response

    def post(self, url: str, *, json: dict[str, str], timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
        self.posts.append((url, json, timeout))
        response = self.responses.pop(0)
        if isinstance(response, requests.exceptions.RequestException):
            raise response
        return response


def make_response(status_code: int, body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode("utf-8")
    response.url = "http://localhost:5000/translate"
    return response


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
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

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
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

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
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

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
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

    result = translator.translate("All right", "en", "pt")

    assert result == "Tudo certo"
    assert len(session.posts) == 4
    assert sleeps == [0.25, 0.5, 0.5]


def test_libretranslate_rate_limits_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    times = iter([10.0, 10.1])
    session = FakeSession(
        [
            make_response(200, '{"translatedText": "Ola"}'),
            make_response(200, '{"translatedText": "Mundo"}'),
        ]
    )
    translator = LibreTranslateTranslator(retries=0, requests_per_second=2.0)
    translator.session = session
    monkeypatch.setattr("ayvu.translator.time.monotonic", lambda: next(times))
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

    translator.translate("Hello", "en", "pt")
    translator.translate("World", "en", "pt")

    assert len(session.posts) == 2
    assert sleeps == [pytest.approx(0.4)]


def test_libretranslate_rate_limiter_is_shared_between_languages_and_translate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    times = iter([20.0, 20.2])
    session = FakeSession(
        responses=[make_response(200, '{"translatedText": "Ola"}')],
        get_responses=[
            make_response(
                200,
                '[{"code": "en", "name": "English", "targets": ["pt"]}]',
            )
        ],
    )
    translator = LibreTranslateTranslator(retries=0, requests_per_second=1.0)
    translator.session = session
    monkeypatch.setattr("ayvu.translator.time.monotonic", lambda: next(times))
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

    translator.list_languages()
    translator.translate("Hello", "en", "pt")

    assert len(session.gets) == 1
    assert len(session.posts) == 1
    assert sleeps == [pytest.approx(0.8)]


def test_libretranslate_instances_can_share_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    times = iter([30.0, 30.25])
    rate_limiter = RequestRateLimiter(requests_per_second=2.0)
    first_session = FakeSession([make_response(200, '{"translatedText": "Ola"}')])
    second_session = FakeSession([make_response(200, '{"translatedText": "Mundo"}')])
    first = LibreTranslateTranslator(retries=0, rate_limiter=rate_limiter)
    second = LibreTranslateTranslator(retries=0, rate_limiter=rate_limiter)
    first.session = first_session
    second.session = second_session
    monkeypatch.setattr("ayvu.translator.time.monotonic", lambda: next(times))
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

    first.translate("Hello", "en", "pt")
    second.translate("World", "en", "pt")

    assert len(first_session.posts) == 1
    assert len(second_session.posts) == 1
    assert sleeps == [pytest.approx(0.25)]


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
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

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
    monkeypatch.setattr("ayvu.translator.time.sleep", lambda delay: sleeps.append(delay))

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


def _language(code: str, targets: tuple[str, ...]) -> TranslatorLanguage:
    return TranslatorLanguage(code=code, name=code.upper(), targets=targets)


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


class RecordingTranslator(Translator):
    def __init__(self, responses: dict[tuple[str, str], str] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._responses = responses or {}

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        suffix = self._responses.get((source, target), target.upper())
        return f"{text}|{suffix}"


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
