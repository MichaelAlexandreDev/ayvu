"""Requests transport pinned to an authorized numeric sockaddr, without a second resolver."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from threading import Lock
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from .network_policy import NetworkDecision, NetworkPolicyError


@dataclass(frozen=True, repr=False)
class ValidatedAddress:
    family: int
    sockaddr: tuple
    alternatives: tuple[ValidatedAddress, ...] = ()


class NetworkAuthorization:
    """One immutable policy snapshot and one DNS observation shared by worker factories.

    Sessions remain per translator/thread. Resolution is lazy so dry-run and none
    create neither sessions nor sockets. Failed resolution is also remembered.
    """

    def __init__(self, decision: NetworkDecision, resolver: Callable | None = None) -> None:
        self.decision = decision
        self._resolver = resolver
        self._lock = Lock()
        self._address: ValidatedAddress | None = None
        self._failure: str | None = None
        self._resolution_failed = False

    def __repr__(self) -> str:
        return f"NetworkAuthorization({self.decision!r})"

    def authorize(self, operation: str) -> ValidatedAddress:
        self.decision.require(operation)
        with self._lock:
            if self._failure:
                raise NetworkPolicyError(self.decision, operation, self._failure)
            if self._resolution_failed:
                raise requests.exceptions.ConnectionError("Local translator resolution failed")
            if self._address is not None:
                return self._address
            host, port = self.decision.host, self.decision.port
            if self.decision.destination_class == "loopback_literal":
                address = ipaddress.ip_address(host)
                family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
                self._address = ValidatedAddress(family, (host, port, 0, 0) if address.version == 6 else (host, port))
                return self._address
            try:
                answers = (self._resolver or socket.getaddrinfo)(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except OSError:
                # This is resolution failure, not evidence that a remote address was allowed.
                self._resolution_failed = True
                raise requests.exceptions.ConnectionError("Local translator resolution failed") from None
            validated = []
            try:
                for family, kind, protocol, _name, sockaddr in answers:
                    if not isinstance(sockaddr[0], str) or "%" in sockaddr[0]:
                        raise ValueError
                    address = ipaddress.ip_address(sockaddr[0])
                    if (family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM
                            or protocol not in (0, socket.IPPROTO_TCP) or not address.is_loopback
                            or (family == socket.AF_INET) != (address.version == 4)
                            or type(sockaddr[1]) is not int or sockaddr[1] != port
                            or (family == socket.AF_INET and len(sockaddr) != 2)
                            or (family == socket.AF_INET6 and (len(sockaddr) != 4 or sockaddr[2:] != (0, 0)))):
                        raise ValueError
                    canonical = (address.compressed, port, 0, 0) if address.version == 6 else (address.compressed, port)
                    validated.append(ValidatedAddress(family, canonical))
                if not validated:
                    raise ValueError
            except (ValueError, TypeError, IndexError):
                self._failure = "unsafe_resolution"
                raise NetworkPolicyError(self.decision, operation, self._failure) from None
            self._address = ValidatedAddress(validated[0].family, validated[0].sockaddr, tuple(validated[1:]))
            return self._address


class PinnedLoopbackAdapter(HTTPAdapter):
    """Only the validated sockaddr reaches connect(); urllib3 never resolves host."""

    def __init__(self, decision: NetworkDecision, address: ValidatedAddress) -> None:
        self._decision = decision

        class PinnedConnection(HTTPConnection):
            def _new_conn(self):
                timed_out = False
                for target in (address, *address.alternatives):
                    connection = None
                    try:
                        connection = socket.socket(target.family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                        connection.settimeout(self.timeout)
                        for option in self.socket_options or ():
                            connection.setsockopt(*option)
                        connection.connect(target.sockaddr)
                        return connection
                    except OSError as error:
                        if connection is not None:
                            connection.close()
                        timed_out = isinstance(error, TimeoutError)
                if timed_out:
                    raise ConnectTimeoutError(self, "Local translator connection timed out") from None
                raise NewConnectionError(self, "Local translator connection failed") from None

        class PinnedPool(HTTPConnectionPool):
            ConnectionCls = PinnedConnection

        self._pool = PinnedPool(decision.host, decision.port, maxsize=1, block=True)
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self._pool

    def get_connection(self, url, proxies=None):
        return self._pool

    def send(self, request, **kwargs):
        # Never let a mutated request/session introduce a different hop or credentials.
        if (request.url not in (self._decision.base_url + "/translate", self._decision.base_url + "/languages")
                or kwargs.get("proxies") or kwargs.get("cert") or "Authorization" in request.headers
                or "Proxy-Authorization" in request.headers):
            raise NetworkPolicyError(self._decision, "provider", "unsupported_destination")
        return super().send(request, **kwargs)

    def close(self):
        self._pool.close()
        super().close()


def create_http_session(decision: NetworkDecision, address: ValidatedAddress) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.mount("http://", PinnedLoopbackAdapter(decision, address))
    return session
