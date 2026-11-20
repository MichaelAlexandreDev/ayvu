"""The HTTPS transport of the ``remote`` policy (#379): declared hosts only, no environment, no stray redirect.

Every hop is decided again by ``resolve_network_policy`` with the hosts the adapter declared, before
any socket opens: the first request, each redirect, and the adapter's own check right before the wire.
The session ignores environment proxies, ``.netrc`` and CA overrides (``trust_env=False``), verifies
TLS, and never follows a redirect by itself. Only a method-preserving redirect (307 or 308) to a
declared host is followed, and credential headers go only to the host the run was authorized for.
Failures are typed by ``RemoteTransportError.reason``; their messages are codes, never a URL or a body.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import urljoin

import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from .policy import NetworkDecision, NetworkPolicy, NetworkPolicyError, resolve_network_policy

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_FOLLOWED = frozenset({307, 308})


class RateLimiter(Protocol):
    def wait(self) -> None: ...


class RemoteTransportError(Exception):
    """``reason``: policy, connection, timeout, request, redirect or malformed. ``code`` is a bounded code."""

    def __init__(self, reason: str, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code or reason


@dataclass(frozen=True, repr=False)
class RemoteResponse:
    """A final, non-redirect answer. ``retry_after`` is the raw header, for the adapter to interpret."""

    status_code: int
    content: bytes
    retry_after: str | None = None

    def __repr__(self) -> str:
        return f"RemoteResponse(status_code={self.status_code})"


def _decide(decision: NetworkDecision, url: str) -> NetworkDecision:
    return resolve_network_policy(NetworkPolicy.REMOTE, url, decision.allowed_hosts)


class _DeclaredHostsAdapter(HTTPAdapter):
    """The last check before the wire: the prepared URL is decided again, and no proxy or TLS override passes."""

    def __init__(self, decision: NetworkDecision) -> None:
        self._decision = decision
        super().__init__(max_retries=0)

    def send(self, request, **kwargs):
        target = _decide(self._decision, request.url)
        if not target.remote:
            raise NetworkPolicyError(target, "translate")
        if (kwargs.get("proxies") or kwargs.get("cert") or kwargs.get("verify") is not True
                or "Proxy-Authorization" in request.headers):
            raise NetworkPolicyError(self._decision, "translate", "proxy_refused")
        return super().send(request, **kwargs)


class _RefusedAdapter(BaseAdapter):
    """Plain HTTP never leaves through a remote session; loopback has its own pinned transport."""

    def __init__(self, decision: NetworkDecision) -> None:
        super().__init__()
        self._decision = decision

    def send(self, request, **kwargs):
        raise NetworkPolicyError(self._decision, "translate", "unsupported_scheme")

    def close(self) -> None:
        pass


def create_remote_session(decision: NetworkDecision) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.verify = True
    session.mount("https://", _DeclaredHostsAdapter(decision))
    session.mount("http://", _RefusedAdapter(decision))
    return session


class RemoteHttpClient:
    """One HTTPS session for one remote adapter instance. Each worker gets its own client; a limiter may be shared.

    A connection failure or a 5xx is retried within ``retry_delays``; a timeout is not, because the provider
    may still be working (and charging), and a 429 is not, because the LLM core waits it out.
    """

    def __init__(self, decision: NetworkDecision, *, timeout: float, rate_limiter: RateLimiter | None = None,
                 retry_delays: tuple[float, ...] = (), sleep: Callable[[float], None] = time.sleep) -> None:
        self.network_decision = decision
        self.timeout = timeout
        self.rate_limiter = rate_limiter
        self._retry_delays, self._sleep = retry_delays, sleep
        self._session: requests.Session | None = None

    def __repr__(self) -> str:
        return f"RemoteHttpClient({self.network_decision!r})"

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def post_json(self, path: str, body: dict, *, headers: dict[str, str] | None = None,
                  credential_headers: dict[str, str] | None = None) -> RemoteResponse:
        """POST ``body`` to ``path`` under the decision's base URL, following only declared 307/308 hops."""
        for attempt in range(len(self._retry_delays) + 1):
            try:
                response = self._post_json(path, body, headers, credential_headers)
            except RemoteTransportError as failure:
                if failure.reason != "connection" or attempt == len(self._retry_delays):
                    raise
            else:
                if response.status_code < 500 or attempt == len(self._retry_delays):
                    return response
            self._sleep(self._retry_delays[attempt])
        raise AssertionError("unreachable")

    def _post_json(self, path: str, body: dict, headers: dict[str, str] | None,
                   credential_headers: dict[str, str] | None) -> RemoteResponse:
        decision = self.network_decision
        if not decision.remote:
            raise RemoteTransportError("policy", decision.reason if not decision.allowed else "unsupported_destination")
        url = decision.base_url + path
        for hop in range(MAX_REDIRECTS + 1):
            target = _decide(decision, url)
            if not target.remote:
                raise RemoteTransportError("policy", "redirect_refused" if hop else target.reason)
            # A credential belongs to the authorized host, never to another one, even a declared one.
            same_origin = (target.host, target.port) == (decision.host, decision.port)
            sent = {**(headers or {}), **((credential_headers or {}) if same_origin else {})}
            response = self._send(url, body, sent)
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                if response.status_code not in _FOLLOWED or not location:
                    raise RemoteTransportError("redirect", "provider_redirect_refused")
                url = urljoin(url, location)
                continue
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise RemoteTransportError("malformed", "response_too_large")
            return RemoteResponse(response.status_code, response.content, response.headers.get("Retry-After"))
        raise RemoteTransportError("redirect", "too_many_redirects")

    def _send(self, url: str, body: dict, headers: dict[str, str]) -> requests.Response:
        if self._session is None:
            self._session = create_remote_session(self.network_decision)
        if self.rate_limiter is not None:
            self.rate_limiter.wait()
        try:
            return self._session.post(url, json=body, headers=headers, timeout=self.timeout, allow_redirects=False)
        except NetworkPolicyError as error:
            raise RemoteTransportError("policy", error.code) from None
        except requests.exceptions.Timeout:
            raise RemoteTransportError("timeout") from None
        except requests.exceptions.ConnectionError:
            raise RemoteTransportError("connection") from None
        except requests.exceptions.RequestException:
            raise RemoteTransportError("request") from None
