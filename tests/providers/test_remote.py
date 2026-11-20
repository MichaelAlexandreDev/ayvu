"""What a remote adapter declares, and the opaque credential it is handed (#379)."""
from __future__ import annotations

import copy
import pickle

import pytest

from ayvu.providers import ProviderSetupError, RemoteCredential, check_remote_adapter, credential_from_environment
from tests.support.remote_fakes import SECRET, FakeRemoteAdapter


def test_the_credential_never_shows_its_value():
    credential = credential_from_environment("FAKE_REMOTE_API_KEY", {"FAKE_REMOTE_API_KEY": f"  {SECRET}\n"})
    assert credential.reveal() == SECRET and credential.source == "FAKE_REMOTE_API_KEY"
    shown = f"{credential} {credential!r} {[credential]} {vars(type(credential))!r}"
    assert SECRET not in shown and "FAKE_REMOTE_API_KEY" in shown
    with pytest.raises(TypeError):
        pickle.dumps(credential)
    with pytest.raises(TypeError):
        copy.deepcopy(credential)
    assert credential != RemoteCredential(SECRET, "FAKE_REMOTE_API_KEY")


@pytest.mark.parametrize("environ", [{}, {"FAKE_REMOTE_API_KEY": ""}, {"FAKE_REMOTE_API_KEY": "   "}])
def test_a_missing_credential_is_a_code(environ):
    with pytest.raises(ProviderSetupError, match="^credential_missing$"):
        credential_from_environment("FAKE_REMOTE_API_KEY", environ)


@pytest.mark.parametrize("secret,source", [("has space", "KEY_NAME"), ("x" * 4097, "KEY_NAME"), ("ok", "lower"),
                                           ("ok", "A"), ("line\nbreak", "KEY_NAME")])
def test_a_malformed_credential_is_refused_without_echoing_it(secret, source):
    with pytest.raises(ProviderSetupError, match="^invalid_credential$") as caught:
        RemoteCredential(secret, source)
    assert secret not in str(caught.value)


def test_an_adapter_must_declare_hosts_retention_and_its_variable():
    check_remote_adapter(FakeRemoteAdapter())
    for name, value in (("ALLOWED_HOSTS", ()), ("ALLOWED_HOSTS", ["api.test"]), ("RETENTION_CLAIM", ""),
                        ("RETENTION_CLAIM", "x" * 201), ("RETENTION_CLAIM", "two\nlines"), ("CREDENTIAL_ENV", "key"),
                        ("BASE_URL", "http://api.test"), ("ADAPTER_ID", "Bad-Id"), ("DECLARED_CODES", {"en"})):
        adapter = type("Broken", (FakeRemoteAdapter,), {name: value})()
        with pytest.raises(ProviderSetupError, match="^invalid_remote_adapter$"):
            check_remote_adapter(adapter)
