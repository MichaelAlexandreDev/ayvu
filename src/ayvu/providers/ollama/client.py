"""The HTTP transport to a local Ollama server, through the loopback policy of ``ayvu.network``.

Only two endpoints are reachable: ``GET /api/tags`` lists the installed models and ``POST /api/chat``
asks one. Failures are typed by ``OllamaError.reason`` and their messages are never read, since a
response body or a URL could be quoted in them. A connection failure or a 5xx is retried within the
configured delays; a timeout is not, because the server may still be generating, and a 429 is not,
because the LLM core waits it out.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import requests

from ...network import NetworkPolicyError
from ...network.http import NetworkAuthorization, create_http_session

TAGS_PATH, CHAT_PATH = "/api/tags", "/api/chat"
OLLAMA_ENDPOINTS = (TAGS_PATH, CHAT_PATH)
MAX_MODELS = 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


class OllamaError(Exception):
    """``reason``: policy, connection, timeout, request, http, redirect or malformed. Its message is a code only."""

    def __init__(self, reason: str, *, status_code: int | None = None, code: str | None = None,
                 retry_after_seconds: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.code = code or reason
        self.retry_after_seconds = retry_after_seconds


class RateLimiter(Protocol):
    def wait(self) -> None: ...


@dataclass(frozen=True)
class InstalledModel:
    """A model as ``/api/tags`` lists it. ``digest`` is the lowercase sha256 hex of its manifest."""

    name: str
    digest: str


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() and len(value) <= 9 else None


class OllamaClient:
    """One HTTP session per client. Each worker gets its own client; a rate limiter may be shared."""

    def __init__(self, authorization: NetworkAuthorization, *, timeout: float,
                 rate_limiter: RateLimiter | None = None, retry_delays: tuple[float, ...] = (),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._authorization = authorization
        self.network_decision = authorization.decision
        self.timeout = timeout
        self.rate_limiter = rate_limiter
        self._retry_delays, self._sleep = retry_delays, sleep
        self._session: requests.Session | None = None

    def __repr__(self) -> str:
        return f"OllamaClient({self.network_decision!r})"

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def list_models(self) -> tuple[InstalledModel, ...]:
        """Names and digests only. An entry without a sha256 digest makes the whole list malformed."""
        data = self._call("languages", TAGS_PATH)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list) or len(models) > MAX_MODELS:
            raise OllamaError("malformed")
        found = []
        for item in models:
            name = item.get("name") if isinstance(item, dict) else None
            digest = item.get("digest") if isinstance(item, dict) else None
            match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
            if not isinstance(name, str) or not name or match is None:
                raise OllamaError("malformed")
            found.append(InstalledModel(name, match.group(1)))
        return tuple(found)

    def chat(self, body: dict) -> dict:
        data = self._call("translate", CHAT_PATH, body)
        if not isinstance(data, dict):
            raise OllamaError("malformed")
        return data

    def _open(self, operation: str) -> requests.Session:
        try:
            address = self._authorization.authorize(operation)
        except NetworkPolicyError as error:
            raise OllamaError("policy", code=error.code) from None
        except requests.exceptions.ConnectionError:
            raise OllamaError("connection") from None
        if self._session is None:
            self._session = create_http_session(self.network_decision, address, OLLAMA_ENDPOINTS)
        return self._session

    def _call(self, operation: str, path: str, body: dict | None = None) -> object:
        url = self.network_decision.base_url + path
        attempt = 0
        while True:
            try:
                return self._answer(self._open(operation), url, body)
            except OllamaError as failure:
                retryable = failure.reason == "connection" or (failure.reason == "http" and failure.status_code >= 500)
                if not retryable or attempt == len(self._retry_delays):
                    raise
                self._sleep(self._retry_delays[attempt])
                attempt += 1

    def _answer(self, session: requests.Session, url: str, body: dict | None) -> object:
        if self.rate_limiter is not None:
            self.rate_limiter.wait()
        try:
            if body is None:
                response = session.get(url, timeout=self.timeout, allow_redirects=False)
            else:
                response = session.post(url, json=body, timeout=self.timeout, allow_redirects=False)
        except NetworkPolicyError as error:
            raise OllamaError("policy", code=error.code) from None
        except requests.exceptions.ConnectionError:
            raise OllamaError("connection") from None
        except requests.exceptions.Timeout:
            raise OllamaError("timeout") from None
        except requests.exceptions.RequestException:
            raise OllamaError("request") from None
        status = response.status_code
        if 300 <= status < 400:
            raise OllamaError("redirect", status_code=status)
        if status >= 400:
            raise OllamaError("http", status_code=status, retry_after_seconds=_retry_after(response))
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise OllamaError("malformed")
        try:
            return response.json()
        except ValueError:
            raise OllamaError("malformed") from None
