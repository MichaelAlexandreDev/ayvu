"""The ``remote`` policy's HTTPS transport (#379), against an instrumented wire in place of ``HTTPAdapter.send``.

Every check of ``network.remote_http`` runs; only the socket is replaced. Redirects, proxies and
credentials are what an adapter cannot get wrong, because the transport decides them.
"""
from __future__ import annotations

import pytest
import requests

from ayvu.network import NetworkPolicyError, resolve_network_policy
from ayvu.network.remote_http import RemoteHttpClient, RemoteTransportError, create_remote_session
from tests.support.remote_fakes import wire  # noqa: F401

HOSTS = ("api.provider.test", "files.provider.test")
KEY = {"Authorization": "Bearer sk-wire-secret"}


def client(url="https://api.provider.test/v1", hosts=HOSTS, policy="remote"):
    return RemoteHttpClient(resolve_network_policy(policy, url, hosts), timeout=5)


def redirect(status, location):
    return lambda body: (status, {}, {"Location": location})


def ok(body):
    return 200, {"ok": True}, {}


def test_an_allowed_host_is_reached_directly_with_its_credential(wire):
    response = client().post_json("/chat", {"q": 1}, headers={"Accept": "application/json"}, credential_headers=KEY)
    assert response.status_code == 200 and response.content == b'{"ok": true}'
    [sent] = wire.requests
    assert sent["url"] == "https://api.provider.test/v1/chat" and sent["method"] == "POST" and sent["body"] == {"q": 1}
    assert sent["headers"]["Authorization"] == KEY["Authorization"] and sent["verify"] is True and sent["proxies"] == {}


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"])
def test_an_environment_proxy_is_present_and_ignored(wire, monkeypatch, variable):
    monkeypatch.setenv(variable, "http://proxy-user:proxy-secret@proxy.invalid:3128")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/ca.pem")
    client().post_json("/chat", {}, credential_headers=KEY)
    [sent] = wire.requests
    assert sent["proxies"] == {} and sent["verify"] is True
    assert "Proxy-Authorization" not in sent["headers"] and sent["url"].startswith("https://api.provider.test/")
    assert create_remote_session(client().network_decision).trust_env is False


def test_a_redirect_to_a_declared_host_is_followed_without_the_credential(wire):
    wire.script = [redirect(307, "https://files.provider.test/v1/chat"), ok]
    assert client().post_json("/chat", {"q": 1}, credential_headers=KEY).status_code == 200
    first, second = wire.requests
    assert second["url"] == "https://files.provider.test/v1/chat" and second["body"] == {"q": 1}
    assert first["headers"]["Authorization"] == KEY["Authorization"] and "Authorization" not in second["headers"]


def test_a_relative_redirect_on_the_same_host_keeps_the_credential(wire):
    wire.script = [redirect(308, "/v2/chat"), ok]
    client().post_json("/chat", {}, credential_headers=KEY)
    assert [item["url"] for item in wire.requests] == ["https://api.provider.test/v1/chat",
                                                       "https://api.provider.test/v2/chat"]
    assert wire.requests[1]["headers"]["Authorization"] == KEY["Authorization"]


@pytest.mark.parametrize("location", [
    "https://evil.test/collect", "http://api.provider.test/v1/chat", "http://localhost:5000/translate",
    "https://user:secret@api.provider.test/", "https://api.provider.test/v1/chat?leak=1", "https://198.51.100.9/",
])
def test_a_redirect_off_the_list_is_refused_and_never_sent(wire, location):
    wire.script = [redirect(307, location)]
    with pytest.raises(RemoteTransportError) as caught:
        client().post_json("/chat", {}, credential_headers=KEY)
    assert (caught.value.reason, caught.value.code) == ("policy", "redirect_refused")
    assert len(wire.requests) == 1 and "secret" not in str(caught.value) and location not in str(caught.value)


@pytest.mark.parametrize("status", [301, 302, 303])
def test_a_redirect_that_changes_the_method_is_not_followed(wire, status):
    wire.script = [redirect(status, "https://api.provider.test/v1/other")]
    with pytest.raises(RemoteTransportError) as caught:
        client().post_json("/chat", {})
    assert caught.value.reason == "redirect" and len(wire.requests) == 1


def test_redirects_are_bounded(wire):
    wire.script = [redirect(307, "/v1/chat")] * 8
    with pytest.raises(RemoteTransportError) as caught:
        client().post_json("/chat", {})
    assert caught.value.code == "too_many_redirects" and len(wire.requests) == 4


@pytest.mark.parametrize("url,policy", [
    ("https://api.provider.test", "loopback"), ("https://api.provider.test", "none"),
    ("https://other.test", "remote"), ("http://localhost:5000", "remote"),
])
def test_no_request_without_a_remote_decision(wire, url, policy):
    with pytest.raises(RemoteTransportError) as caught:
        client(url, policy=policy).post_json("/chat", {}, credential_headers=KEY)
    assert caught.value.reason == "policy" and wire.requests == []


def test_the_last_check_refuses_a_mutated_request_and_plain_http(wire):
    session = create_remote_session(client().network_decision)
    with pytest.raises(NetworkPolicyError) as caught:
        session.post("https://other.test/v1/chat", json={}, allow_redirects=False)
    assert caught.value.code == "unsupported_destination"
    with pytest.raises(NetworkPolicyError) as caught:
        session.post("https://api.provider.test/v1/chat", json={}, proxies={"https": "http://proxy.invalid"})
    assert caught.value.code == "proxy_refused"
    with pytest.raises(NetworkPolicyError):
        session.post("https://api.provider.test/v1/chat", json={}, verify=False)
    with pytest.raises(NetworkPolicyError) as caught:
        session.post("http://localhost:5000/translate", json={})
    assert caught.value.code == "unsupported_scheme"
    assert wire.requests == []


@pytest.mark.parametrize("error,reason", [
    (requests.exceptions.ConnectTimeout, "timeout"), (requests.exceptions.ReadTimeout, "timeout"),
    (requests.exceptions.SSLError, "connection"), (requests.exceptions.ConnectionError, "connection"),
    (requests.exceptions.ChunkedEncodingError, "request"),
])
def test_transport_failures_are_typed_without_their_message(monkeypatch, error, reason):
    def fail(adapter, request, **kwargs):
        raise error("https://api.provider.test/v1/chat?secret")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fail)
    with pytest.raises(RemoteTransportError) as caught:
        client().post_json("/chat", {})
    assert caught.value.reason == reason and "secret" not in str(caught.value)


def test_connection_failures_and_5xx_are_retried_within_the_delays_and_the_limiter_is_waited(monkeypatch, wire):
    waits, sleeps, calls = [], [], []
    limiter = type("Limiter", (), {"wait": lambda self: waits.append(1)})()
    decision = client().network_decision
    retrying = RemoteHttpClient(decision, timeout=5, rate_limiter=limiter, retry_delays=(0.5, 1.0), sleep=sleeps.append)
    wire.script = [lambda body: (503, {}, {}), ok]
    assert retrying.post_json("/chat", {}).status_code == 200
    assert sleeps == [0.5] and len(waits) == len(wire.requests) == 2

    def flaky(adapter, request, **kwargs):
        calls.append(request.url)
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", flaky)
    with pytest.raises(RemoteTransportError, match="^connection$"):
        retrying.post_json("/chat", {})
    assert len(calls) == 3 and sleeps == [0.5, 0.5, 1.0]


@pytest.mark.parametrize("status", [429, 400, 401])
def test_client_errors_are_returned_once_for_the_adapter_to_map(wire, status):
    sleeps = []
    retrying = RemoteHttpClient(client().network_decision, timeout=5, retry_delays=(0.5,), sleep=sleeps.append)
    wire.script = [lambda body: (status, {}, {"Retry-After": "3"})]
    response = retrying.post_json("/chat", {})
    assert (response.status_code, response.retry_after) == (status, "3") and sleeps == [] and len(wire.requests) == 1


def test_a_timeout_is_not_retried(monkeypatch):
    calls = []

    def slow(adapter, request, **kwargs):
        calls.append(1)
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", slow)
    retrying = RemoteHttpClient(client().network_decision, timeout=5, retry_delays=(0.5,), sleep=lambda _: None)
    with pytest.raises(RemoteTransportError, match="^timeout$"):
        retrying.post_json("/chat", {})
    assert calls == [1]
