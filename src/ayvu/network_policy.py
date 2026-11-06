"""Closed, request-scoped HTTP authority (#159). Parsing never performs I/O."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import StrEnum


class NetworkPolicy(StrEnum):
    NONE = "none"
    LOOPBACK = "loopback"


NETWORK_DENIAL_CODES = frozenset({
    "strict_offline", "unsupported_destination", "unsupported_scheme", "userinfo_unsupported", "malformed_destination",
    "malformed_authority", "unsupported_port", "unsafe_resolution", "unknown_network_policy",
})


class NetworkPolicyError(ValueError):
    """Only bounded diagnostic fields, never caller strings or an endpoint."""

    def __init__(self, decision: NetworkDecision, operation: str, reason: str | None = None) -> None:
        self.code = reason or decision.reason
        self.operation = operation if operation in {"translate", "languages"} else "provider"
        self.policy = decision.policy
        self.destination_class = decision.destination_class
        self.defaulted = decision.defaulted
        super().__init__(decision.diagnostic(self.operation, self.code))


@dataclass(frozen=True, repr=False)
class NetworkDecision:
    policy: NetworkPolicy
    defaulted: bool
    destination_class: str
    reason: str
    host: str = field(default="", repr=False)
    port: int = 80
    base_url: str = field(default="", repr=False)

    @property
    def allowed(self) -> bool:
        return self.policy is NetworkPolicy.LOOPBACK and self.reason == "loopback_permitted"

    def require(self, operation: str) -> None:
        if not self.allowed:
            raise NetworkPolicyError(self, operation)

    def diagnostic(self, operation: str, reason: str | None = None) -> str:
        default = "; origem=padrao_compativel" if self.defaulted else "; origem=explicita"
        return (f"operacao={operation}; politica={self.policy.value}; destino={self.destination_class}; "
                f"motivo={reason or self.reason}{default}")

    def __repr__(self) -> str:
        return f"NetworkDecision({self.diagnostic('provider')})"


def resolve_network_policy(value: NetworkPolicy | str | None, url: str) -> NetworkDecision:
    """Compatibility composition boundary. A missing value means loopback, never remote.

    Under none even URL parsing is unnecessary: configuration is already a string,
    and no DNS, transport, credential helper or request body needs to be touched.
    """
    try:
        policy = NetworkPolicy.LOOPBACK if value is None else NetworkPolicy(value)
    except (ValueError, TypeError):
        # Unknown selections fail closed and are never echoed.
        return NetworkDecision(NetworkPolicy.NONE, False, "unclassified", "unknown_network_policy")
    if policy is NetworkPolicy.NONE:
        return NetworkDecision(policy, False, "not_evaluated", "strict_offline")

    def denied(reason: str, classification: str = "unsupported") -> NetworkDecision:
        return NetworkDecision(policy, value is None, classification, reason)

    if (not isinstance(url, str) or not url or not url.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in "\\%?#")):
        return denied("malformed_destination")
    try:
        parts = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)(/.*)?", url)
        if parts is None:
            return denied("malformed_destination")
        scheme, authority, path = parts.groups()
        if scheme.lower() != "http":
            return denied("unsupported_scheme")
        if "@" in authority:
            return denied("userinfo_unsupported")
        if authority.startswith("["):
            match = re.fullmatch(r"\[([0-9a-fA-F:]+)\](?::([0-9]+))?", authority)
        else:
            match = re.fullmatch(r"([A-Za-z0-9.]+)(?::([0-9]+))?", authority)
        if match is None:
            return denied("malformed_authority")
        host, port_text = match.groups()
        port = int(port_text) if port_text is not None else 80
        if not 1 <= port <= 65535:
            return denied("unsupported_port")
        host = host.lower()
        if host in ("localhost", "localhost."):
            classification = "loopback_hostname"
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return denied("unsupported_destination", "non_loopback_host")
            if not address.is_loopback:
                return denied("unsupported_destination", "non_loopback_address")
            classification = "loopback_literal"
            host = address.compressed
        formatted_host = f"[{host}]" if ":" in host else host
        path = (path or "").rstrip("/")
        if path.endswith("/translate"):
            path = path[:-len("/translate")]
        base = f"http://{formatted_host}"
        if port_text is not None:
            base += f":{port}"
        return NetworkDecision(policy, value is None, classification, "loopback_permitted", host, port, base + path)
    except ValueError:
        return denied("malformed_destination")


def network_next_step(code: str) -> str | None:
    if code == "strict_offline":
        return ("A política none bloqueia o HTTP do Ayvu antes de DNS e sockets. "
                "Use --network-policy loopback para acessar o tradutor HTTP local, ou --cache-only/--dry-run. "
                "Um leitor externo é outro processo e fica fora dessa garantia.")
    if code in {"unsupported_destination", "unsupported_scheme", "userinfo_unsupported", "malformed_destination",
                "malformed_authority", "unsupported_port", "unsafe_resolution", "unknown_network_policy"}:
        return ("Use --network-policy loopback com uma URL http de localhost, localhost., 127.0.0.0/8 ou [::1], "
                "porta 1–65535, sem credenciais. HTTPS e destinos não loopback exigem uma futura política explícita; "
                "a URL anterior não foi migrada.")
    return None
