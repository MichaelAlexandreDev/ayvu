from __future__ import annotations
import requests
import pytest
from ayvu.providers.libretranslate.client import (
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


def _language(code: str, targets: tuple[str, ...]) -> TranslatorLanguage:
    return TranslatorLanguage(code=code, name=code.upper(), targets=targets)


class RecordingTranslator(Translator):
    def __init__(self, responses: dict[tuple[str, str], str] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._responses = responses or {}

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        suffix = self._responses.get((source, target), target.upper())
        return f"{text}|{suffix}"
