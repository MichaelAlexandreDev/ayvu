from __future__ import annotations

import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol

import requests


SUPPORTED_TRANSLATORS = ("libretranslate",)


class TranslatorError(RuntimeError):
    """``reason`` and ``status_code`` classify a failure without reading its message,
    which can quote the response body or a URL holding credentials."""

    def __init__(self, *args: object, reason: str | None = None, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.reason = reason
        self.status_code = status_code


class UnsupportedTranslatorError(TranslatorError):
    pass


class RouteResolutionError(TranslatorError):
    pass


@dataclass(frozen=True)
class TranslatorLanguage:
    code: str
    name: str
    targets: tuple[str, ...] = ()
    state: str = "installed"


@dataclass(frozen=True)
class TranslationRoute:
    source: str
    target: str
    intermediate: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.intermediate is None

    def describe(self) -> str:
        if self.intermediate is None:
            return f"{self.source} -> {self.target}"
        return f"{self.source} -> {self.intermediate} -> {self.target}"


class Translator(ABC):
    @abstractmethod
    def translate(self, text: str, source: str, target: str) -> str:
        raise NotImplementedError


def resolve_translation_route(
    languages: tuple[TranslatorLanguage, ...],
    source: str,
    target: str,
    intermediate_candidates: tuple[str, ...] = ("en",),
) -> TranslationRoute:
    if source == target:
        return TranslationRoute(source=source, target=target)

    by_code = {language.code: language for language in languages}

    source_lang = by_code.get(source)
    if source_lang is not None and target in source_lang.targets:
        return TranslationRoute(source=source, target=target)

    for bridge in intermediate_candidates:
        if bridge in (source, target):
            continue
        bridge_lang = by_code.get(bridge)
        if bridge_lang is None or target not in bridge_lang.targets:
            continue
        if source_lang is None or bridge not in source_lang.targets:
            continue
        return TranslationRoute(source=source, target=target, intermediate=bridge)

    raise RouteResolutionError(
        f"No translation route available from '{source}' to '{target}'."
    )


class RoutedTranslator(Translator):
    def __init__(self, base: Translator, route: TranslationRoute) -> None:
        self._base = base
        self._route = route

    @property
    def route(self) -> TranslationRoute:
        return self._route

    def translate(self, text: str, source: str, target: str) -> str:
        if (
            self._route.is_direct
            or source != self._route.source
            or target != self._route.target
        ):
            return self._base.translate(text, source, target)
        intermediate = self._route.intermediate
        assert intermediate is not None
        first = self._base.translate(text, source, intermediate)
        return self._base.translate(first, intermediate, target)


class HttpSession(Protocol):
    def get(self, url: str, *, timeout: float) -> requests.Response:
        pass

    def post(self, url: str, *, json: dict[str, str], timeout: float) -> requests.Response:
        pass


@dataclass(frozen=True)
class LibreTranslatePayload:
    text: str
    source: str
    target: str

    def as_json(self) -> dict[str, str]:
        return {
            "q": self.text,
            "source": self.source,
            "target": self.target,
            "format": "text",
        }


@dataclass(frozen=True)
class RetryPolicy:
    retries: int
    backoff: float = 0.5
    max_backoff: float = 8.0

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise TranslatorError("retry count must be zero or greater")
        if self.backoff < 0:
            raise TranslatorError("retry backoff must be zero or greater")
        if self.max_backoff < 0:
            raise TranslatorError("retry backoff max must be zero or greater")

    @property
    def max_attempts(self) -> int:
        return max(1, self.retries + 1)

    def attempts(self) -> range:
        return range(1, self.max_attempts + 1)

    def can_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts

    def delay_for(self, attempt: int) -> float:
        if self.backoff == 0 or self.max_backoff == 0:
            return 0.0
        return min(self.max_backoff, self.backoff * (2 ** max(0, attempt - 1)))

    def should_retry_status(self, status_code: int, attempt: int) -> bool:
        return (status_code == 429 or status_code >= 500) and self.can_retry(attempt)


@dataclass
class RequestRateLimiter:
    requests_per_second: float | None = None
    _last_request_at: float | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.requests_per_second is not None and self.requests_per_second <= 0:
            raise TranslatorError("requests per second must be greater than zero")

    def wait(self) -> None:
        if self.requests_per_second is None:
            return

        with self._lock:
            interval = 1.0 / self.requests_per_second
            now = time.monotonic()
            if self._last_request_at is None:
                self._last_request_at = now
                return

            next_request_at = self._last_request_at + interval
            delay = next_request_at - now
            if delay > 0:
                time.sleep(delay)
                self._last_request_at = next_request_at
                return

            self._last_request_at = now


class LibreTranslateResponseParser:
    def parse(self, response: requests.Response) -> str:
        try:
            data = response.json()
        except ValueError as exc:
            raise TranslatorError("LibreTranslate response was not valid JSON", reason="malformed") from exc

        translated = data.get("translatedText") if isinstance(data, dict) else None
        if not isinstance(translated, str):
            raise TranslatorError("LibreTranslate response did not include translatedText", reason="malformed")
        return translated


class LibreTranslateLanguagesParser:
    def parse(self, response: requests.Response) -> tuple[TranslatorLanguage, ...]:
        try:
            data = response.json()
        except ValueError as exc:
            raise TranslatorError("LibreTranslate languages response was not valid JSON", reason="malformed") from exc

        if not isinstance(data, list):
            raise TranslatorError("LibreTranslate languages response did not include a language list", reason="malformed")

        return tuple(self._parse_language_entry(entry) for entry in data)

    @staticmethod
    def _parse_language_entry(entry: object) -> TranslatorLanguage:
        if not isinstance(entry, dict):
            raise TranslatorError("LibreTranslate language entry was not an object", reason="malformed")

        code = entry.get("code")
        name = entry.get("name")
        if not isinstance(code, str) or not isinstance(name, str):
            raise TranslatorError("LibreTranslate language entry did not include code and name", reason="malformed")

        targets = entry.get("targets", ())
        if not isinstance(targets, list):
            targets = []
        target_codes = tuple(target for target in targets if isinstance(target, str))
        return TranslatorLanguage(code=code, name=name, targets=target_codes)


@dataclass
class LibreTranslateTranslator(Translator):
    url: str = "http://localhost:5000"
    timeout: float = 30.0
    retries: int = 2
    requests_per_second: float | None = None
    retry_backoff: float = 0.5
    retry_backoff_max: float = 8.0
    rate_limiter: RequestRateLimiter | None = None

    def __post_init__(self) -> None:
        self.endpoint = self._normalize_endpoint(self.url)
        self.session: HttpSession = requests.Session()
        self.retry_policy = RetryPolicy(
            self.retries,
            backoff=self.retry_backoff,
            max_backoff=self.retry_backoff_max,
        )
        self.rate_limiter = self.rate_limiter or RequestRateLimiter(self.requests_per_second)
        self.response_parser = LibreTranslateResponseParser()

    def translate(self, text: str, source: str, target: str) -> str:
        if not text:
            return text

        payload = LibreTranslatePayload(text=text, source=source, target=target)
        last_error: Exception | None = None

        for attempt in self.retry_policy.attempts():
            try:
                response = self._post(payload)
                if self._should_retry_response(response, attempt):
                    self._wait_before_retry(attempt)
                    continue
                response.raise_for_status()
                return self.response_parser.parse(response)
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if self._retry_after_exception(attempt):
                    continue
                raise TranslatorError(
                    f"Could not connect to LibreTranslate at {self.endpoint}. "
                    "Is the local translation server running?",
                    reason="connection",
                ) from exc
            except requests.exceptions.Timeout as exc:
                last_error = exc
                if self._retry_after_exception(attempt):
                    continue
                raise TranslatorError(
                    f"LibreTranslate request timed out after {self.timeout} seconds", reason="timeout"
                ) from exc
            except requests.exceptions.HTTPError as exc:
                raise self._http_error(exc) from exc
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if self._retry_after_exception(attempt):
                    continue
                raise TranslatorError(f"LibreTranslate request failed: {exc}", reason="request") from exc

        raise TranslatorError(f"LibreTranslate request failed: {last_error}", reason="request")

    def list_languages(self) -> tuple[TranslatorLanguage, ...]:
        endpoint = self._normalize_languages_endpoint(self.url)
        last_error: Exception | None = None

        for attempt in self.retry_policy.attempts():
            try:
                response = self._get_languages(endpoint)
                if self._should_retry_response(response, attempt):
                    self._wait_before_retry(attempt)
                    continue
                response.raise_for_status()
                return LibreTranslateLanguagesParser().parse(response)
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if self._retry_after_exception(attempt):
                    continue
                raise TranslatorError(
                    f"Could not connect to LibreTranslate at {endpoint}. "
                    "Is the local translation server running?",
                    reason="connection",
                ) from exc
            except requests.exceptions.Timeout as exc:
                last_error = exc
                if self._retry_after_exception(attempt):
                    continue
                raise TranslatorError(
                    f"LibreTranslate languages request timed out after {self.timeout} seconds", reason="timeout"
                ) from exc
            except requests.exceptions.HTTPError as exc:
                raise self._http_error(exc) from exc
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if self._retry_after_exception(attempt):
                    continue
                raise TranslatorError(f"LibreTranslate languages request failed: {exc}", reason="request") from exc

        raise TranslatorError(f"LibreTranslate languages request failed: {last_error}", reason="request")

    def _post(self, payload: LibreTranslatePayload) -> requests.Response:
        self.rate_limiter.wait()
        return self.session.post(self.endpoint, json=payload.as_json(), timeout=self.timeout)

    def _get_languages(self, endpoint: str) -> requests.Response:
        self.rate_limiter.wait()
        return self.session.get(endpoint, timeout=self.timeout)

    def _should_retry_response(self, response: requests.Response, attempt: int) -> bool:
        return self.retry_policy.should_retry_status(response.status_code, attempt)

    def _retry_after_exception(self, attempt: int) -> bool:
        if not self.retry_policy.can_retry(attempt):
            return False

        self._wait_before_retry(attempt)
        return True

    def _wait_before_retry(self, attempt: int) -> None:
        time.sleep(self.retry_policy.delay_for(attempt))

    @staticmethod
    def _http_error(exc: requests.exceptions.HTTPError) -> TranslatorError:
        response = exc.response
        if response is None:
            return TranslatorError(f"LibreTranslate HTTP error: {exc}", reason="http")
        return TranslatorError(
            f"LibreTranslate HTTP error {response.status_code}: {response.text[:300]}",
            reason="http",
            status_code=response.status_code,
        )

    @staticmethod
    def _normalize_endpoint(url: str) -> str:
        return f"{LibreTranslateTranslator._normalize_base_url(url)}/translate"

    @staticmethod
    def _normalize_languages_endpoint(url: str) -> str:
        return f"{LibreTranslateTranslator._normalize_base_url(url)}/languages"

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        clean = url.rstrip("/")
        if clean.endswith("/translate"):
            return clean[: -len("/translate")]
        return clean


def create_translator(
    name: str,
    url: str,
    timeout: float = 30.0,
    retries: int = 2,
    requests_per_second: float | None = None,
    retry_backoff: float = 0.5,
    retry_backoff_max: float = 8.0,
    rate_limiter: RequestRateLimiter | None = None,
) -> Translator:
    if name != "libretranslate":
        supported = ", ".join(SUPPORTED_TRANSLATORS)
        raise UnsupportedTranslatorError(f"Unsupported translator: {name}. Supported translators: {supported}.")
    return LibreTranslateTranslator(
        url=url,
        timeout=timeout,
        retries=retries,
        requests_per_second=requests_per_second,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
        rate_limiter=rate_limiter,
    )
