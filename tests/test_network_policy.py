"""Security boundary tests: exercise production policy, Requests and socket connection code."""
from __future__ import annotations

import ast
import io
import json
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from ayvu.network_policy import NetworkPolicy, NetworkPolicyError, resolve_network_policy
from ayvu.network_http import NetworkAuthorization
from ayvu.translator import LibreTranslateTranslator, TranslatorError, TranslatorPolicyError


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


def answer(host="127.0.0.1", port=5000):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port, 0, 0) if family == socket.AF_INET6 else (host, port))


@pytest.mark.parametrize("url,host,port", [
    ("http://localhost:5000", "localhost", 5000), ("HTTP://LOCALHOST.:80/translate/", "localhost.", 80),
    ("http://127.0.0.1", "127.0.0.1", 80), ("http://127.2.3.4:65535/prefix", "127.2.3.4", 65535),
    ("http://[::1]:1", "::1", 1), ("http://[0:0:0:0:0:0:0:1]:5000", "::1", 5000),
])
def test_canonical_loopback_forms(url, host, port):
    decision = resolve_network_policy("loopback", url)
    assert decision.allowed and decision.host == host and decision.port == port
    assert decision.defaulted is False
    with pytest.raises(FrozenInstanceError):
        decision.port = 123


@pytest.mark.parametrize("url", [
    "http://192.168.1.1", "http://10.0.0.1", "http://172.16.0.1", "http://8.8.8.8", "http://169.254.169.254",
    "http://169.254.1.2", "http://224.0.0.1", "http://0.0.0.0", "http://255.255.255.255", "http://[::]",
    "http://[fe80::1]", "http://[fd00::1]", "http://[ff02::1]", "http://[2001:db8::1]", "http://[::ffff:127.0.0.1]",
    "http://[::1%lo]", "http://[::1%25lo]", "http://arbitrary.invalid", "http://foo.localhost", "http://localhost.evil",
    "http://localhost..", "http://127.1", "http://2130706433", "http://0177.0.0.1", "http://0x7f000001",
    "https://localhost:5000", "ftp://127.0.0.1", "file:///localhost", "//localhost:5000", "localhost:5000", "",
    "http://localhost:", "http://localhost:0", "http://localhost:65536", "http://localhost:-1", "http://localhost:+80",
    "http://[::1", "http://::1", "http://[127.0.0.1]", "http://localhost:80:90", "http://localhost@127.0.0.1",
    "http://user:secret@localhost", "http://localhost/path?token=secret", "http://localhost/#fragment", "http://localhost?",
    " http://localhost", "http://localhost\n", "http://localhost\\evil", "http://local\thost", "http://locаlhost",
])
def test_unsupported_destinations_never_reach_resolver(url):
    def forbidden(*args):
        pytest.fail("resolver reached for rejected URL")
    authorization = NetworkAuthorization(resolve_network_policy("loopback", url), forbidden)
    with pytest.raises(NetworkPolicyError) as caught:
        authorization.authorize("languages")
    assert caught.value.code != "strict_offline"
    assert url not in repr(authorization) if url else True


def test_closed_vocabulary_and_visible_compatibility_default():
    assert {item.value for item in NetworkPolicy} == {"none", "loopback"}
    for value in ("remote", "NONE", "", 3, object()):
        denied = resolve_network_policy(value, "http://localhost:5000")
        assert not denied.allowed and denied.reason == "unknown_network_policy"
    default = resolve_network_policy(None, "http://localhost:5000")
    assert default.allowed and default.policy is NetworkPolicy.LOOPBACK and default.defaulted
    assert "padrao_compativel" in repr(default)
    assert not resolve_network_policy(None, "http://192.168.1.2").allowed


def deny_network(monkeypatch):
    reached = []
    def forbidden(*args, **kwargs):
        reached.append("forbidden")
        raise AssertionError("strict offline reached DNS/socket/HTTP/body")
    for target, name in [(socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                         (requests.Session, "send"), (requests.Session, "__init__"),
                         (requests.adapters.HTTPAdapter, "send")]:
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr("ayvu.translator.LibreTranslatePayload", forbidden)
    monkeypatch.setattr(requests.models.PreparedRequest, "prepare_body", forbidden)
    return reached


@pytest.mark.parametrize("operation", ["translate", "languages"])
def test_none_denies_before_every_production_network_and_body_boundary(monkeypatch, operation, caplog):
    reached = deny_network(monkeypatch)
    translator = LibreTranslateTranslator(url="https://user:credential@arbitrary.invalid", network_policy="none")
    with pytest.raises(TranslatorPolicyError) as caught:
        translator.translate("SOURCE_TEXT_CANARY", "en", "pt") if operation == "translate" else translator.list_languages()
    assert caught.value.reason == "policy" and caught.value.code == "strict_offline"
    diagnostic = str(caught.value) + repr(translator) + caplog.text
    assert f"operacao={operation}" in diagnostic and "politica=none" in diagnostic and "destino=not_evaluated" in diagnostic
    assert all(secret not in diagnostic for secret in ("SOURCE_TEXT_CANARY", "credential", "arbitrary.invalid"))
    assert reached == [] and translator._session is None


def test_none_precedes_translation_body_serialization(monkeypatch):
    serialized = []
    def serialize(payload):
        serialized.append(payload)
        raise AssertionError("source serialized")
    monkeypatch.setattr("ayvu.translator.LibreTranslatePayload.as_json", serialize)
    with pytest.raises(TranslatorPolicyError, match="motivo=strict_offline"):
        LibreTranslateTranslator(network_policy="none").translate("PRIVATE_SOURCE", "en", "pt")
    assert serialized == []


def test_none_also_rejects_an_empty_translation_without_dns(monkeypatch):
    reached = deny_network(monkeypatch)
    with pytest.raises(TranslatorPolicyError) as caught:
        LibreTranslateTranslator(network_policy="none").translate("", "en", "pt")
    assert caught.value.code == "strict_offline" and reached == []


@pytest.mark.parametrize("answers", [[], [answer("127.0.0.1"), answer("192.168.1.2")], [answer("::1"), answer("8.8.8.8")],
                                    [answer("0.0.0.0")], [answer("::1%lo")], [answer("127.0.0.1", 80)],
                                    [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 5000, 0, 1))],
                                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5000, 0))],
                                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5000.0))]])
def test_all_dns_answers_must_be_loopback_and_scope_free(answers, monkeypatch):
    calls = []
    authorization = NetworkAuthorization(resolve_network_policy(None, "http://localhost:5000"), lambda *args: calls.append(args) or answers)
    monkeypatch.setattr(socket, "socket", lambda *args: pytest.fail("unsafe answer reached socket"))
    with pytest.raises(NetworkPolicyError) as caught:
        authorization.authorize("translate")
    assert caught.value.code == "unsafe_resolution" and len(calls) == 1
    with pytest.raises(NetworkPolicyError):
        authorization.authorize("languages")
    assert len(calls) == 1


class WireSocket:
    """A socket-shaped wire, with real Requests/urllib3 parsing, framing and adapter dispatch."""
    def __init__(self, replies, connections, family):
        self.replies = replies
        self.connections = connections
        self.family = family
        self.sent = []
        self.timeout = None

    def settimeout(self, timeout): self.timeout = timeout
    def setsockopt(self, *args): pass
    def connect(self, sockaddr): self.connections.append((self.family, sockaddr, self.timeout))
    def sendall(self, data): self.sent.append(bytes(data))
    def makefile(self, *args, **kwargs): return io.BytesIO(self.replies.pop(0))
    def close(self): pass


def wire_reply(body, status=200, extra=""):
    payload = body.encode()
    return (f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n{extra}\r\n".encode() + payload)


def install_wire(monkeypatch, replies):
    sockets, connections = [], []
    def create(family, kind, protocol=0):
        assert kind == socket.SOCK_STREAM
        wire = WireSocket(replies, connections, family)
        sockets.append(wire)
        return wire
    monkeypatch.setattr(socket, "socket", create)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("uncontrolled create_connection"))
    return sockets, connections


@pytest.mark.parametrize("host", ["127.9.8.7", "::1"])
def test_socket_connect_is_pinned_and_dns_rebinding_cannot_change_it(monkeypatch, host):
    lookups = []
    def rebinding(*args):
        lookups.append(args)
        return [answer(host), answer("127.0.0.1")] if len(lookups) == 1 else [answer("192.168.1.9")]
    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    replies = [wire_reply("[]"), wire_reply('{"translatedText":"Olá"}')]
    sockets, connections = install_wire(monkeypatch, replies)
    translator = LibreTranslateTranslator(retries=0, timeout=3.25)
    assert translator.list_languages() == ()
    assert translator.translate("Hello", "en", "pt") == "Olá"
    assert len(lookups) == 1 and len(connections) == 2
    assert all(item[1] == answer(host)[4] and item[2] == 3.25 for item in connections)
    requests_on_wire = b"".join(b"".join(wire.sent) for wire in sockets)
    assert b"GET /languages HTTP/1.1" in requests_on_wire and b"POST /translate HTTP/1.1" in requests_on_wire
    assert b"/health" not in requests_on_wire
    translator.close()


@pytest.mark.parametrize("url", ["http://127.0.0.1:5000", "http://[::1]:5000"])
def test_literal_connection_needs_no_dns(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("literal reached DNS"))
    _, connections = install_wire(monkeypatch, [wire_reply("[]")])
    assert LibreTranslateTranslator(url=url, retries=0).list_languages() == ()
    assert connections[0][1] == (("::1", 5000, 0, 0) if "[" in url else ("127.0.0.1", 5000))


def test_connection_fallback_uses_only_addresses_from_the_one_validated_observation(monkeypatch):
    lookups = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: lookups.append(args) or [answer("::1"), answer("127.0.0.1")])
    sockets, connections = install_wire(monkeypatch, [wire_reply("[]")])
    original_connect = WireSocket.connect
    def connect(wire, sockaddr):
        original_connect(wire, sockaddr)
        if wire.family == socket.AF_INET6:
            raise ConnectionRefusedError("IPv6 local server is not listening")
    monkeypatch.setattr(WireSocket, "connect", connect)
    assert LibreTranslateTranslator(retries=0).list_languages() == ()
    assert len(lookups) == 1 and len(sockets) == 2
    assert [item[1] for item in connections] == [("::1", 5000, 0, 0), ("127.0.0.1", 5000)]


def test_failed_resolution_is_a_connection_error_and_is_not_repeated(monkeypatch):
    calls = []
    def resolver(*args):
        calls.append(args)
        raise socket.gaierror("sensitive resolver detail")
    authorization = NetworkAuthorization(resolve_network_policy(None, "http://localhost:5000"), resolver)
    translator = LibreTranslateTranslator(network_authorization=authorization)
    for operation in (translator.list_languages, lambda: translator.translate("source", "en", "pt")):
        with pytest.raises(TranslatorError) as caught:
            operation()
        assert caught.value.reason == "connection" and "sensitive" not in str(caught.value)
    assert len(calls) == 1 and translator._session is None


@pytest.mark.parametrize("operation", ["translate", "languages"])
@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308, 399])
@pytest.mark.parametrize("location", ["http://localhost:5000/again", "http://user:redirectsecret@remote.invalid/steal"])
def test_every_redirect_is_typed_and_never_followed(monkeypatch, operation, status, location):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [answer()])
    sockets, connections = install_wire(monkeypatch, [wire_reply("secretbody", status, f"Location: {location}\r\n")])
    translator = LibreTranslateTranslator(retries=2)
    with pytest.raises(TranslatorError) as caught:
        translator.translate("SOURCE_CANARY", "en", "pt") if operation == "translate" else translator.list_languages()
    assert caught.value.reason == "redirect" and caught.value.status_code == status
    assert len(sockets) == len(connections) == 1
    wire = b"".join(sockets[0].sent)
    assert b"redirectsecret" not in wire and b"remote.invalid" not in wire and b"secretbody" not in wire
    assert "secretbody" not in str(caught.value) and "redirectsecret" not in str(caught.value)


def test_environment_proxy_and_netrc_cannot_supply_routing_or_credentials(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    netrc = home / ".netrc"
    netrc.write_text("machine localhost login ambientuser password ambientpassword\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://proxyuser:proxypassword@proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [answer()])
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda *a, **k: pytest.fail("netrc helper reached"))
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", lambda *a, **k: pytest.fail("ambient proxies reached"))
    sockets, connections = install_wire(monkeypatch, [wire_reply("[]"), wire_reply('{"translatedText":"Olá"}')])
    translator = LibreTranslateTranslator(retries=0)
    translator.list_languages()
    translator.translate("Hello", "en", "pt")
    assert translator.session.trust_env is False
    assert all(item[1] == ("127.0.0.1", 5000) for item in connections)
    wire = b"".join(b"".join(item.sent) for item in sockets)
    assert b"Authorization:" not in wire and b"Proxy-Authorization:" not in wire
    assert all(secret not in wire for secret in (b"ambientpassword", b"proxypassword", b"proxy.invalid"))


@pytest.mark.parametrize("command", ["languages", "test-translator"])
def test_cli_none_and_legacy_destination_denial_are_actionable_and_zero_io(monkeypatch, command):
    from ayvu.cli import app
    reached = deny_network(monkeypatch)
    result = CliRunner().invoke(app, [command, "--network-policy", "none"])
    assert result.exit_code == 1, result.output
    assert "strict_offline" in result.output and "politica=none" in result.output and "operacao=" in result.output
    assert "leitor externo" in result.output.lower()
    for url in ("http://10.0.0.1:5000", "https://localhost:5000"):
        result = CliRunner().invoke(app, [command, "--url", url])
        assert result.exit_code == 1 and ("unsupported_destination" in result.output or "unsupported_scheme" in result.output)
        assert "futura política" in result.output and url not in result.output
    assert reached == []


@pytest.mark.parametrize("selection", [[], ["--network-policy", "loopback"], ["--network-policy", "none"]])
@pytest.mark.parametrize("command", ["languages", "test-translator"])
def test_cli_selection_uses_real_composition_and_requests(monkeypatch, command, selection):
    from ayvu.cli import app
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [answer()])
    sockets, _ = install_wire(monkeypatch, [wire_reply("[]" if command == "languages" else '{"translatedText":"Olá"}')])
    result = CliRunner().invoke(app, [command, *selection])
    if "none" in selection:
        assert result.exit_code == 1 and "strict_offline" in result.output and sockets == []
    else:
        assert result.exit_code == 0, result.output
        assert len(sockets) == 1


@pytest.mark.parametrize("args", [[], ["translate"], ["resume"], ["languages"], ["test-translator"]])
def test_help_explains_request_policy_and_external_reader(args):
    from ayvu.cli import app
    result = CliRunner().invoke(app, [*args, "--help"])
    assert result.exit_code == 0 and "--network-policy" in result.output
    assert "loopback" in result.output and "none" in result.output and "externos" in result.output


def test_import_contract_has_no_presentation_or_desktop_dependency():
    root = Path(__file__).resolve().parents[1] / "src" / "ayvu"
    for name in ("network_policy", "network_http", "provider_network", "provider_discovery", "provider_composition", "translator"):
        tree = ast.parse((root / f"{name}.py").read_text())
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(part in module.split(".") for module in imports for part in ("typer", "rich", "desktop", "PySide6"))
