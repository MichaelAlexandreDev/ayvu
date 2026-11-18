"""Closed, request-scoped HTTP authority (#159, #379). Parsing never performs I/O.

``loopback`` allows plain HTTP to this machine only. ``remote`` adds HTTPS to the hosts one remote
adapter declares, and nothing else; it is never a default, and loopback stays allowed under it.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import StrEnum


class NetworkPolicy(StrEnum):
    NONE = "none"
    LOOPBACK = "loopback"
    REMOTE = "remote"


NETWORK_DENIAL_CODES = frozenset({
    "strict_offline", "unsupported_destination", "unsupported_scheme", "userinfo_unsupported", "malformed_destination",
    "malformed_authority", "unsupported_port", "unsafe_resolution", "unknown_network_policy", "redirect_refused",
    "proxy_refused",
})
LOOPBACK_PERMITTED, REMOTE_PERMITTED = "loopback_permitted", "remote_permitted"
_REMOTE_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")


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
    # The ``host:port`` entries the remote adapter declared, which every redirect must match; empty for loopback.
    allowed_hosts: tuple[str, ...] = field(default=(), repr=False)

    @property
    def allowed(self) -> bool:
        if self.reason == LOOPBACK_PERMITTED:
            return self.policy in (NetworkPolicy.LOOPBACK, NetworkPolicy.REMOTE)
        return self.policy is NetworkPolicy.REMOTE and self.reason == REMOTE_PERMITTED

    @property
    def remote(self) -> bool:
        """An allowed HTTPS destination off this machine, on the adapter's declared list."""
        return self.allowed and self.reason == REMOTE_PERMITTED

    def require(self, operation: str) -> None:
        if not self.allowed:
            raise NetworkPolicyError(self, operation)

    def diagnostic(self, operation: str, reason: str | None = None) -> str:
        default = "; origem=padrao_compativel" if self.defaulted else "; origem=explicita"
        return (f"operacao={operation}; politica={self.policy.value}; destino={self.destination_class}; "
                f"motivo={reason or self.reason}{default}")

    def __repr__(self) -> str:
        return f"NetworkDecision({self.diagnostic('provider')})"


def _allowlist(allowed_hosts: tuple[str, ...]) -> dict[str, int]:
    """``host`` (port 443) or ``host:port`` entries in lowercase. Anything else is dropped and allows nothing."""
    entries: dict[str, int] = {}
    for entry in allowed_hosts if type(allowed_hosts) is tuple else ():
        match = re.fullmatch(r"([a-z0-9.-]+)(?::([0-9]{1,5}))?", entry) if isinstance(entry, str) else None
        if match is not None and 1 <= int(match.group(2) or 443) <= 65535:
            entries[match.group(1)] = int(match.group(2) or 443)
    return entries


def _remote(policy: NetworkPolicy, value, host: str, port_text: str | None, path: str | None,
            allowed_hosts: tuple[str, ...]) -> NetworkDecision:
    """HTTPS to a declared host only. An address literal passes only when it is itself declared."""
    allowlist = _allowlist(allowed_hosts)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None and not _REMOTE_HOST.fullmatch(host):
        return NetworkDecision(policy, value is None, "unsupported", "malformed_authority")
    canonical = host if literal is None else literal.compressed
    if canonical not in allowlist:
        classification = "undeclared_address" if literal is not None else "undeclared_host"
        return NetworkDecision(policy, value is None, classification, "unsupported_destination")
    port = int(port_text) if port_text is not None else 443
    if port != allowlist[canonical]:
        return NetworkDecision(policy, value is None, "declared_host", "unsupported_port")
    base = f"https://{canonical}" + (f":{port}" if port != 443 else "") + (path or "").rstrip("/")
    return NetworkDecision(policy, value is None, "declared_host", REMOTE_PERMITTED, canonical, port, base,
                           tuple(sorted(f"{name}:{entry}" for name, entry in allowlist.items())))


def _loopback(policy: NetworkPolicy, value, host: str, port_text: str | None, path: str | None) -> NetworkDecision:
    port = int(port_text) if port_text is not None else 80
    if host in ("localhost", "localhost."):
        classification = "loopback_hostname"
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return NetworkDecision(policy, value is None, "non_loopback_host", "unsupported_destination")
        if not address.is_loopback:
            return NetworkDecision(policy, value is None, "non_loopback_address", "unsupported_destination")
        classification = "loopback_literal"
        host = address.compressed
    formatted_host = f"[{host}]" if ":" in host else host
    path = (path or "").rstrip("/")
    if path.endswith("/translate"):
        path = path[:-len("/translate")]
    base = f"http://{formatted_host}"
    if port_text is not None:
        base += f":{port}"
    return NetworkDecision(policy, value is None, classification, LOOPBACK_PERMITTED, host, port, base + path)


def _destination(policy: NetworkPolicy, value, url: str, allowed_hosts: tuple[str, ...]) -> NetworkDecision:
    def denied(reason: str, classification: str = "unsupported") -> NetworkDecision:
        return NetworkDecision(policy, value is None, classification, reason)

    # No query, fragment or escape anywhere: none of them may carry a second destination or a secret.
    if (not isinstance(url, str) or not url or not url.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in "\\%?#")):
        return denied("malformed_destination")
    parts = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)(/.*)?", url)
    if parts is None:
        return denied("malformed_destination")
    scheme, authority, path = parts.groups()
    scheme = scheme.lower()
    if scheme not in ("http", "https") or (scheme == "https" and policy is not NetworkPolicy.REMOTE):
        return denied("unsupported_scheme")
    if "@" in authority:
        return denied("userinfo_unsupported")
    if authority.startswith("["):
        match = re.fullmatch(r"\[([0-9a-fA-F:]+)\](?::([0-9]+))?", authority)
    else:
        match = re.fullmatch(r"([A-Za-z0-9.-]+)(?::([0-9]+))?", authority)
    if match is None or (scheme == "http" and "-" in authority):
        return denied("malformed_authority")
    host, port_text = match.groups()
    if port_text is not None and not 1 <= int(port_text) <= 65535:
        return denied("unsupported_port")
    if scheme == "https":
        return _remote(policy, value, host.lower(), port_text, path, allowed_hosts)
    loopback = _loopback(policy, value, host.lower(), port_text, path)
    if policy is NetworkPolicy.REMOTE and not loopback.allowed:
        # Plain HTTP off this machine is never remote egress: a remote provider needs HTTPS.
        return denied("unsupported_scheme", loopback.destination_class)
    return loopback


def resolve_network_policy(value: NetworkPolicy | str | None, url: str,
                           allowed_hosts: tuple[str, ...] = ()) -> NetworkDecision:
    """Compatibility composition boundary. A missing value means loopback, never remote.

    Under none even URL parsing is unnecessary: configuration is already a string,
    and no DNS, transport, credential helper or request body needs to be touched.
    ``allowed_hosts`` is what one remote adapter declares; only ``remote`` reads it.
    """
    try:
        policy = NetworkPolicy.LOOPBACK if value is None else NetworkPolicy(value)
    except (ValueError, TypeError):
        # Unknown selections fail closed and are never echoed.
        return NetworkDecision(NetworkPolicy.NONE, False, "unclassified", "unknown_network_policy")
    if policy is NetworkPolicy.NONE:
        return NetworkDecision(policy, False, "not_evaluated", "strict_offline")
    try:
        return _destination(policy, value, url, allowed_hosts)
    except ValueError:
        return NetworkDecision(policy, value is None, "unsupported", "malformed_destination")


def network_next_step(code: str) -> str | None:
    if code == "strict_offline":
        return ("A política none bloqueia o HTTP do Ayvu antes de DNS e sockets. "
                "Use --network-policy loopback para acessar o tradutor HTTP local, ou --cache-only/--dry-run. "
                "Um leitor externo é outro processo e fica fora dessa garantia.")
    if code in {"redirect_refused", "proxy_refused"}:
        return ("Um provedor remoto só é acessado por HTTPS, direto, nos hosts que ele declara: nenhum proxy do "
                "ambiente e nenhum redirecionamento para fora dessa lista.")
    if code in {"unsupported_destination", "unsupported_scheme", "userinfo_unsupported", "malformed_destination",
                "malformed_authority", "unsupported_port", "unsafe_resolution", "unknown_network_policy"}:
        return ("Use --network-policy loopback com uma URL http de localhost, localhost., 127.0.0.0/8 ou [::1], "
                "porta 1–65535, sem credenciais. HTTPS vale só para um provedor remoto, com --network-policy remote "
                "e os hosts que ele declara; a URL anterior não foi migrada.")
    return None
