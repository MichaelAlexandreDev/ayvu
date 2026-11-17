"""Ollama's HTTP client against a fake loopback server: discovery, the endpoint allowlist, policy and retries (#376)."""
from __future__ import annotations

import socket

import pytest

from ayvu.network import NetworkPolicyError, resolve_network_policy
from ayvu.network.http import NetworkAuthorization, create_http_session
from ayvu.providers.ollama.client import InstalledModel, OllamaClient, OllamaError
from tests.support.ollama_fakes import DIGEST_A, DIGEST_B, MODEL, FakeOllama, fake_ollama, reply, slow  # noqa: F401


def client(url, policy="loopback", **options):
    options.setdefault("timeout", 5.0)
    return OllamaClient(NetworkAuthorization(resolve_network_policy(policy, url)), **options)


def test_discovery_lists_each_installed_model_with_its_digest():
    server = FakeOllama(models=((MODEL, "sha256:" + DIGEST_A), ("llama3.2:3b", DIGEST_B))).start()
    try:
        models = client(server.url).list_models()
    finally:
        server.close()
    assert models == (InstalledModel(MODEL, DIGEST_A), InstalledModel("llama3.2:3b", DIGEST_B))
    assert server.requests == [("GET", "/api/tags")]


@pytest.mark.parametrize("entry", [{"name": MODEL}, {"name": MODEL, "digest": "short"}, {"digest": DIGEST_A}, "x"])
def test_a_model_without_a_sha256_digest_makes_the_list_malformed(fake_ollama, entry):
    fake_ollama._tags = lambda: (200, {"models": [entry]}, {})
    with pytest.raises(OllamaError) as raised:
        client(fake_ollama.url).list_models()
    assert raised.value.reason == "malformed"


def test_the_session_reaches_only_the_two_ollama_endpoints(fake_ollama):
    ollama = client(fake_ollama.url)
    session = ollama._open("languages")
    with pytest.raises(NetworkPolicyError) as raised:
        session.post(fake_ollama.url + "/api/pull", json={"name": MODEL}, timeout=1, allow_redirects=False)
    assert raised.value.code == "unsupported_destination"
    assert fake_ollama.requests == []


def test_a_libretranslate_session_still_refuses_the_ollama_endpoints(fake_ollama):
    decision = resolve_network_policy("loopback", fake_ollama.url)
    session = create_http_session(decision, NetworkAuthorization(decision).authorize("translate"))
    with pytest.raises(NetworkPolicyError):
        session.post(fake_ollama.url + "/api/chat", json={}, timeout=1, allow_redirects=False)
    assert fake_ollama.requests == []


def test_network_policy_none_refuses_before_any_socket(fake_ollama, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a socket was opened under --network-policy none")

    monkeypatch.setattr(socket, "socket", forbidden)
    with pytest.raises(OllamaError) as raised:
        client(fake_ollama.url, policy="none").chat({"model": MODEL})
    assert (raised.value.reason, raised.value.code) == ("policy", "strict_offline")
    assert fake_ollama.requests == []


def test_a_server_error_is_retried_within_the_delays(fake_ollama):
    fake_ollama.script = [reply(503)]
    waits = []
    answer = client(fake_ollama.url, retry_delays=(0.25,), sleep=waits.append).chat(
        {"model": MODEL, "messages": [{}, {"content": '{"segments": []}'}]})
    assert answer["done"] is True and waits == [0.25] and fake_ollama.chat_count() == 2


def test_a_timeout_is_not_retried(fake_ollama):
    fake_ollama.script = [slow(1.0)]
    with pytest.raises(OllamaError) as raised:
        client(fake_ollama.url, timeout=0.2, retry_delays=(0.0, 0.0), sleep=lambda _: None).chat({"model": MODEL})
    assert raised.value.reason == "timeout" and fake_ollama.chat_count() == 1


def test_a_429_is_left_to_the_core_with_its_retry_after(fake_ollama):
    fake_ollama.script = [reply(429, headers={"Retry-After": "3"})]
    with pytest.raises(OllamaError) as raised:
        client(fake_ollama.url, retry_delays=(0.0,), sleep=lambda _: None).chat({"model": MODEL})
    assert (raised.value.status_code, raised.value.retry_after_seconds) == (429, 3.0)
    assert fake_ollama.chat_count() == 1


def test_a_redirect_is_refused_without_following_it(fake_ollama):
    fake_ollama.script = [reply(307, headers={"Location": "http://example.com/"})]
    with pytest.raises(OllamaError) as raised:
        client(fake_ollama.url).chat({"model": MODEL})
    assert raised.value.reason == "redirect" and fake_ollama.chat_count() == 1
