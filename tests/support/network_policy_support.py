from __future__ import annotations
import ast
import io
import json
import re
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path
import pytest
import requests
from typer.testing import CliRunner
from ayvu.network.policy import NetworkPolicy, NetworkPolicyError, resolve_network_policy
from ayvu.network.http import NetworkAuthorization
from ayvu.providers.libretranslate.client import LibreTranslateTranslator, TranslatorError, TranslatorPolicyError

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


def answer(host="127.0.0.1", port=5000):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port, 0, 0) if family == socket.AF_INET6 else (host, port))


def deny_network(monkeypatch):
    reached = []
    def forbidden(*args, **kwargs):
        reached.append("forbidden")
        raise AssertionError("strict offline reached DNS/socket/HTTP/body")
    for target, name in [(socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                         (requests.Session, "send"), (requests.Session, "__init__"),
                         (requests.adapters.HTTPAdapter, "send")]:
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr("ayvu.providers.libretranslate.client.LibreTranslatePayload", forbidden)
    monkeypatch.setattr(requests.models.PreparedRequest, "prepare_body", forbidden)
    return reached


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
