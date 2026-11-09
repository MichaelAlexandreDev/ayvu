"""Guardas da pesquisa #160. Fakes aqui não constituem evidência de chaveiro."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes/secret-store"
spec = importlib.util.spec_from_file_location("secret_store_research", SPIKE / "probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Approved:
    pass


@pytest.mark.parametrize("candidate", [object(), type("Child", (Approved,), {})(),
    type("Keyring", (), {"__module__": "keyring.backends.SecretService"})()])
def test_backend_identity_rejects_subclasses_unexpected_and_spoofed(candidate):
    with pytest.raises(probe.Refusal, match="^backend_rejected$"):
        probe.approve(candidate, Approved, {})


def test_exact_candidate_class_is_only_a_policy_match():
    assert probe.approve(Approved(), Approved, {}) is None


@pytest.mark.parametrize("key", ["PYTHON_KEYRING_BACKEND", "PYTHON_KEYRING_OTHER",
    "KEYRING_PROPERTY_PREFERRED_COLLECTION", "KEYRING_PROPERTY_SCHEME", "KEYRING_PATH"])
def test_override_is_refused_even_with_approved_backend(key):
    with pytest.raises(probe.Refusal, match="^override_rejected$"):
        probe.approve(Approved(), Approved, {key: "untrusted"})


def test_child_isolates_home_config_and_does_not_inherit_runtime_overrides(tmp_path, monkeypatch):
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "PYTHONPATH", "LD_PRELOAD",
                "HTTPS_PROXY", "PYTHON_KEYRING_BACKEND", "KEYRING_PROPERTY_SCHEME"):
        monkeypatch.setenv(key, "private-sentinel")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "test-bus")
    environment = probe.child_environment(tmp_path, "native")
    assert "private-sentinel" not in environment.values()
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        assert Path(environment[key]).is_relative_to(tmp_path)
        assert Path(environment[key]).is_dir()
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "test-bus"
    assert set(environment) == {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "HOME",
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR", "DBUS_SESSION_BUS_ADDRESS"}


def test_missing_service_case_does_not_reuse_session_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "real-session-sentinel")
    assert "DBUS_SESSION_BUS_ADDRESS" not in probe.child_environment(tmp_path, "missing-service")


class CanaryStore:
    def __init__(self, fail_at=None):
        self.value = None
        self.calls = []
        self.values = []
        self.fail_at = fail_at
        self.cleaned = False

    def step(self, operation, service, account):
        assert service == "ayvu-test-generated"
        assert account == "canary-generated"
        self.calls.append(operation)
        if len(self.calls) == self.fail_at:
            raise RuntimeError("synthetic-failure")

    def set_password(self, service, account, value):
        self.step("set", service, account)
        self.value = value
        self.values.append(value)

    def get_password(self, service, account):
        self.step("get", service, account)
        return self.value

    def delete_password(self, service, account):
        self.step("delete", service, account)
        self.value = None

    def cleanup_exact(self, service, account):
        assert (service, account) == ("ayvu-test-generated", "canary-generated")
        self.value = None
        self.cleaned = True


def test_crud_verifies_rotation_deletion_and_cleanup_without_logging_canaries():
    store, events = CanaryStore(), []
    probe.lifecycle(store, "ayvu-test-generated", "canary-generated", events)
    assert events == ["create", "read", "rotate", "delete", "cleanup_verified"]
    assert store.calls == ["set", "get", "set", "get", "delete", "get"]
    assert len(store.values) == 2
    assert store.values[0] != store.values[1]
    assert not any(value in repr(events) for value in store.values)
    assert store.cleaned and store.value is None


@pytest.mark.parametrize("fail_at", range(1, 7))
def test_cleanup_runs_after_each_native_operation_failure(fail_at):
    store, events = CanaryStore(fail_at), []
    with pytest.raises(RuntimeError, match="^synthetic-failure$"):
        probe.lifecycle(store, "ayvu-test-generated", "canary-generated", events)
    assert len(store.calls) == fail_at
    assert store.cleaned and store.value is None
    assert events[-1] == "cleanup_verified"


@pytest.mark.parametrize("which,code", [(1, "read_mismatch"), (2, "rotate_mismatch"),
                                        (3, "delete_mismatch")])
def test_wrong_native_values_are_not_recorded_as_success(which, code):
    class WrongStore(CanaryStore):
        reads = 0

        def get_password(self, service, account):
            self.reads += 1
            return "wrong-value" if self.reads == which else super().get_password(service, account)

    store, events = WrongStore(), []
    with pytest.raises(probe.Refusal, match=f"^{code}$"):
        probe.lifecycle(store, "ayvu-test-generated", "canary-generated", events)
    assert store.cleaned
    assert events[-1] == "cleanup_verified"


def test_cleanup_failure_never_reports_verified():
    class FailedCleanup(CanaryStore):
        def cleanup_exact(self, *args):
            raise probe.Refusal("cleanup_unverified_locked")

    events = []
    with pytest.raises(probe.Refusal, match="^cleanup_unverified_locked$"):
        probe.lifecycle(FailedCleanup(), "ayvu-test-generated", "canary-generated", events)
    assert "cleanup_verified" not in events


def test_locked_item_refuses_without_unlocking():
    class Locked:
        def is_locked(self):
            return True

        def unlock(self):
            pytest.fail("unlock proibido")

    with pytest.raises(probe.Refusal, match="^locked$"):
        probe.no_item_unlock(Locked())


def test_unlocked_item_does_not_need_unlock():
    assert probe.no_item_unlock(type("Unlocked", (), {"is_locked": lambda self: False})()) is None


def test_collection_changes_and_prompts_are_always_refused():
    with pytest.raises(probe.Refusal, match="^collection_mutation_or_prompt_forbidden$"):
        probe.no_collection_mutation()


@pytest.mark.parametrize("response,expected", [("missing", "missing_service"),
    ("invalid", "unexpected_service_owner"), ("present", None)])
def test_existing_owner_unwraps_dbus_error_before_native_calls(response, expected, monkeypatch):
    class DBusError(Exception):
        name = "org.freedesktop.DBus.Error.NameHasNoOwner"

    marker, calls = object(), []

    def unwrap(reply):
        assert reply is marker
        calls.append("unwrap")
        if response == "missing":
            raise DBusError()
        return (":1.42" if response == "present" else "org.untrusted",)

    def request(name):
        assert name == "org.freedesktop.secrets"
        calls.append("GetNameOwner")
        return "request"

    def reply(message, *, timeout):
        assert message == "request" and timeout == 5
        return marker

    monkeypatch.setitem(sys.modules, "jeepney", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "jeepney.bus_messages", SimpleNamespace(
        message_bus=SimpleNamespace(GetNameOwner=request)))
    monkeypatch.setitem(sys.modules, "jeepney.wrappers", SimpleNamespace(
        DBusErrorResponse=DBusError, unwrap_msg=unwrap))
    bus = SimpleNamespace(send_and_get_reply=reply)
    if expected:
        with pytest.raises(probe.Refusal, match=f"^{expected}$"):
            probe.existing_owner(bus)
    else:
        assert probe.existing_owner(bus) == ":1.42"
    assert calls == ["GetNameOwner", "unwrap"]


@pytest.mark.parametrize("error,outcome", [(PermissionError("private-sentinel"), "access_denied"),
    (RuntimeError("private-sentinel"), "probe_failed"),
    (probe.Refusal("locked"), "locked")])
def test_worker_sanitizes_errors_without_claiming_native_evidence(error, outcome, monkeypatch, capsys):
    def failed(events):
        raise error
    monkeypatch.setattr(probe, "native", failed)
    assert probe.worker("native") == 1
    output = capsys.readouterr().out
    assert "private-sentinel" not in output
    result = json.loads(output)
    assert result["outcome"] == outcome
    assert result["native_evidence"] is False
    assert result["events"] == []


@pytest.mark.parametrize("name,outcome", [
    ("org.freedesktop.DBus.Error.AccessDenied", "denied"),
    ("org.freedesktop.Secret.Error.IsLocked", "locked"),
    ("org.freedesktop.DBus.Error.ServiceUnknown", "missing_service"),
    ("org.freedesktop.DBus.Error.NameHasNoOwner", "missing_service"),
    ("private-sentinel", "service_error")])
def test_dbus_failures_have_distinct_allowlisted_codes(name, outcome):
    error = type("DBusErrorResponse", (Exception,), {"name": name})("private-sentinel")
    assert probe.failure_code(error) == outcome


def test_launcher_passes_isolated_environment_and_cwd_and_detects_fallback_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def run(command, *, env, cwd, **kwargs):
        assert command[1] == "-I"
        assert cwd.is_relative_to(tmp_path)
        assert Path(env["HOME"]).is_relative_to(cwd)
        (Path(env["XDG_CONFIG_HOME"]) / "unapproved-file").write_text("synthetic")
        return type("Completed", (), {"stdout": '{"outcome":"backend_rejected"}', "returncode": 0})()

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.launch("null", "unused-python", tmp_path) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "unexpected_files"
    assert result["isolated_files"] == 1
    assert not list(tmp_path.iterdir())


def test_record_remains_proposed_and_no_platform_is_claimed_without_proof():
    document = (ROOT / "docs/architecture/adr-secret-store-adapters.md").read_text()
    assert "Status: `Proposed`" in document
    assert "Plataformas com suporte comprovado nesta revisão: **nenhuma**" in document
    results = json.loads((SPIKE / "results/sandbox-2026-11-06.json").read_text())
    assert all(not result.get("native_evidence", False) for result in results)
    assert all(result.get("isolated_files") == 0 for result in results)


@pytest.mark.parametrize("relative", ["docs/architecture/adr-secret-store-adapters.md",
                                    "spikes/secret-store/README.md"])
def test_research_local_links_resolve(relative):
    document = ROOT / relative
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
        parsed = urlsplit(target.strip())
        if parsed.scheme or not parsed.path:
            continue
        path = (document.parent / unquote(parsed.path)).resolve()
        assert path.is_relative_to(ROOT)
        assert path.is_file(), target
