from __future__ import annotations

import pytest

from ayvu.providers.libretranslate.client import LibreTranslateTranslator, RequestRateLimiter

from tests.providers.libretranslate.translator_support import FakeSession, make_response


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
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.monotonic", lambda: next(times))
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

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
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.monotonic", lambda: next(times))
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

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
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.monotonic", lambda: next(times))
    monkeypatch.setattr("ayvu.providers.libretranslate.client.time.sleep", lambda delay: sleeps.append(delay))

    first.translate("Hello", "en", "pt")
    second.translate("World", "en", "pt")

    assert len(first_session.posts) == 1
    assert len(second_session.posts) == 1
    assert sleeps == [pytest.approx(0.25)]
