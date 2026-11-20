"""What a remote provider adapter declares, and the opaque credential it is handed (#379). No I/O here.

A remote adapter states, in code, the hosts it may reach (``ALLOWED_HOSTS``), what the provider's
published terms say about retention (``RETENTION_CLAIM``) and the standard environment variable that
holds its key (``CREDENTIAL_ENV``). Composition reads that variable when a run starts and passes a
``RemoteCredential``: it never prints, never serializes and gives its value only to ``reveal``, which
the adapter calls when it builds a request header.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from .errors import ProviderSetupError

MAX_CREDENTIAL_CHARS = 4096
MAX_RETENTION_CLAIM_CHARS = 200
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_ADAPTER_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")


class RemoteCredential:
    """An opaque reference to a secret. Equality is identity; ``repr`` and ``str`` name only its source."""

    __slots__ = ("_secret", "source")

    def __init__(self, secret: str, source: str) -> None:
        if (not isinstance(secret, str) or not secret or len(secret) > MAX_CREDENTIAL_CHARS
                or any(ord(char) < 33 or ord(char) == 127 for char in secret)):
            raise ProviderSetupError("invalid_credential")
        if not isinstance(source, str) or not _ENVIRONMENT_NAME.fullmatch(source):
            raise ProviderSetupError("invalid_credential")
        self._secret, self.source = secret, source

    def reveal(self) -> str:
        return self._secret

    def __repr__(self) -> str:
        return f"RemoteCredential(source={self.source!r})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("A credential is never serialized.")


def credential_from_environment(name: str, environ: Mapping[str, str]) -> RemoteCredential:
    """The value of the provider's standard variable, read now. A missing or empty one is ``credential_missing``."""
    value = environ.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise ProviderSetupError("credential_missing")
    return RemoteCredential(value.strip(), name)


class RemoteModelAdapter(Protocol):
    """A remote model behind the LLM core. Composition builds its HTTPS client under the ``remote`` policy.

    ``producer`` gives the run's ``ProducerIdentity``, ``snapshot`` its ``ProviderCapabilitySnapshot`` and
    ``model`` the ``providers.llm.ModelPort`` that talks to ``client``, a ``network.remote_http.RemoteHttpClient``.
    """

    ADAPTER_ID: str
    BASE_URL: str
    ALLOWED_HOSTS: tuple[str, ...]
    RETENTION_CLAIM: str
    CREDENTIAL_ENV: str
    DECLARED_CODES: frozenset[str]

    def producer(self, model: str) -> object: ...

    def snapshot(self, model: str, observed_at: datetime, *, timeout_seconds: float) -> object: ...

    def model(self, client: object, credential: RemoteCredential, model: str) -> object: ...


def check_remote_adapter(adapter: object) -> None:
    """Refuse an adapter whose declarations are missing or malformed, before anything is read or sent."""
    hosts = getattr(adapter, "ALLOWED_HOSTS", None)
    claim = getattr(adapter, "RETENTION_CLAIM", None)
    if (not isinstance(getattr(adapter, "ADAPTER_ID", None), str) or not _ADAPTER_ID.fullmatch(adapter.ADAPTER_ID)
            or type(hosts) is not tuple or not hosts or any(not isinstance(host, str) or not host for host in hosts)
            or not isinstance(claim, str) or not claim.strip() or len(claim) > MAX_RETENTION_CLAIM_CHARS
            or any(ord(char) < 32 for char in claim)
            or not isinstance(getattr(adapter, "CREDENTIAL_ENV", None), str)
            or not _ENVIRONMENT_NAME.fullmatch(adapter.CREDENTIAL_ENV)
            or not isinstance(getattr(adapter, "BASE_URL", None), str) or not adapter.BASE_URL.startswith("https://")
            or type(getattr(adapter, "DECLARED_CODES", None)) is not frozenset):
        raise ProviderSetupError("invalid_remote_adapter")
