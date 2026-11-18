"""The ``remote`` policy decision (#379): HTTPS to declared hosts only; loopback stays; nothing else changes."""
from __future__ import annotations

import pytest

from ayvu.network import NetworkPolicy, NetworkPolicyError, resolve_network_policy
from ayvu.network.http import NetworkAuthorization

HOSTS = ("api.provider.test", "upload.provider.test:8443", "203.0.113.7")


def remote(url, hosts=HOSTS):
    return resolve_network_policy("remote", url, hosts)


@pytest.mark.parametrize("url,host,port,base", [
    ("https://api.provider.test", "api.provider.test", 443, "https://api.provider.test"),
    ("HTTPS://API.PROVIDER.TEST/v1/", "api.provider.test", 443, "https://api.provider.test/v1"),
    ("https://api.provider.test:443/v1", "api.provider.test", 443, "https://api.provider.test/v1"),
    ("https://upload.provider.test:8443", "upload.provider.test", 8443, "https://upload.provider.test:8443"),
    ("https://203.0.113.7/v1", "203.0.113.7", 443, "https://203.0.113.7/v1"),
])
def test_a_declared_https_host_passes(url, host, port, base):
    decision = remote(url)
    assert decision.allowed and decision.remote and decision.policy is NetworkPolicy.REMOTE
    assert (decision.host, decision.port, decision.base_url) == (host, port, base)
    assert decision.destination_class == "declared_host" and not decision.defaulted
    assert "api.provider.test:443" in decision.allowed_hosts


@pytest.mark.parametrize("url", ["http://localhost:5000", "http://127.0.0.1:11434", "http://[::1]:5000"])
def test_loopback_still_passes_under_remote_and_is_not_remote_egress(url):
    decision = remote(url)
    assert decision.allowed and not decision.remote and decision.reason == "loopback_permitted"
    assert decision.policy is NetworkPolicy.REMOTE
    NetworkAuthorization(decision, lambda *args: [(2, 1, 6, "", ("127.0.0.1", decision.port))]).authorize("translate")


@pytest.mark.parametrize("url,code", [
    ("https://evil.test", "unsupported_destination"),  # another host
    ("https://api.provider.test.evil.test", "unsupported_destination"),
    ("https://provider.test", "unsupported_destination"),
    ("http://api.provider.test", "unsupported_scheme"),  # plain HTTP, even to a declared host
    ("http://10.0.0.1", "unsupported_scheme"),
    ("https://user:secret@api.provider.test", "userinfo_unsupported"),  # credentials in the URL
    ("https://key@api.provider.test", "userinfo_unsupported"),
    ("https://api.provider.test/v1?key=secret", "malformed_destination"),
    ("https://api.provider.test/v1#fragment", "malformed_destination"),
    ("https://api.provider.test/%2e%2e", "malformed_destination"),
    ("https://198.51.100.1", "unsupported_destination"),  # a literal address that is not declared
    ("https://[2001:db8::1]", "unsupported_destination"),
    ("https://127.0.0.1", "unsupported_destination"),
    ("https://localhost", "malformed_authority"),
    ("https://api.provider.test:8443", "unsupported_port"),
    ("https://upload.provider.test", "unsupported_port"),
    ("https://-bad-.test", "malformed_authority"),
    ("ftp://api.provider.test", "unsupported_scheme"),
])
def test_everything_else_is_refused(url, code):
    decision = remote(url)
    assert not decision.allowed and not decision.remote and decision.reason == code
    with pytest.raises(NetworkPolicyError) as caught:
        decision.require("translate")
    assert caught.value.code == code and "secret" not in str(caught.value) and url not in repr(decision)


def test_a_host_is_allowed_only_by_the_adapter_that_declares_it():
    assert not remote("https://api.provider.test", ()).allowed
    assert not remote("https://api.provider.test", ["api.provider.test"]).allowed  # a list is not a declaration
    assert not remote("https://api.provider.test", ("API.PROVIDER.TEST:99999", "*.provider.test")).allowed


def test_none_and_loopback_are_unchanged_and_remote_is_never_the_default():
    for value in ("loopback", None):
        assert resolve_network_policy(value, "https://api.provider.test", HOSTS).reason == "unsupported_scheme"
        assert resolve_network_policy(value, "http://localhost:5000", HOSTS).policy is NetworkPolicy.LOOPBACK
    assert resolve_network_policy(None, "http://localhost:5000").defaulted
    assert resolve_network_policy("none", "https://api.provider.test", HOSTS).reason == "strict_offline"


def test_the_loopback_transport_refuses_a_remote_decision():
    def forbidden(*args):
        pytest.fail("a remote decision reached the loopback resolver")

    with pytest.raises(NetworkPolicyError) as caught:
        NetworkAuthorization(remote("https://api.provider.test"), forbidden).authorize("translate")
    assert caught.value.code == "unsupported_destination"
