"""Pesquisa #160, sem imports Ayvu. Nunca usar como adaptador de produção."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import tempfile
import tomllib
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parent
IDENTITY = "keyring.backends.SecretService.Keyring"
CASES = ("native", "missing-bus", "missing-service", "null", "fail", "plaintext", "alt-encrypted",
         "environment", "property", "chain", "unexpected", "spoofed")


class Refusal(Exception):
    """Somente códigos constantes podem sair da prova."""


def reject_overrides(environment):
    if any(key.startswith(("PYTHON_KEYRING", "KEYRING_")) for key in environment):
        raise Refusal("override_rejected")


def approve(candidate, approved_class, environment):
    reject_overrides(environment)
    # Identidade do objeto classe: nome, prioridade e discovery não autorizam.
    if type(candidate) is not approved_class:
        raise Refusal("backend_rejected")


def child_environment(work, case):
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for key, folder in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                        ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"),
                        ("TMPDIR", "tmp")):
        path = work / folder
        path.mkdir()
        env[key] = str(path)
    # Somente o endpoint necessário. Não herda HOME, loaders, proxies ou Pythonpath.
    if case == "native" and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    if case == "missing-bus":
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(work / "absent-bus")
    return env


def inventory():
    """Somente distribuições da pesquisa; não consulta configuração do usuário."""
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    packages = []
    for requirement in expected:
        name, version = requirement.split("==")
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            packages.append({"name": name, "expected": version, "state": "missing"})
            continue
        licenses = []
        for file in dist.files or ():
            if any(part.lower() in ("licenses", "license", "license.txt", "copying", "notice")
                   for part in file.parts):
                content = dist.locate_file(file).read_bytes()
                licenses.append({"file": str(file), "sha256": hashlib.sha256(content).hexdigest(),
                                 "text": content.decode("utf-8", errors="replace")})
        packages.append({"name": name, "version": dist.version, "expected": version,
                         "matches_pin": dist.version == version,
                         "license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
                         "requires": dist.requires or [],
                         "official_urls": dist.metadata.get_all("Project-URL") or [],
                         "licenses": licenses,
                         "metadata_sha256": hashlib.sha256((dist.read_text("METADATA") or "").encode()).hexdigest()})
    return {"python": platform.python_version(), "architecture": platform.machine(),
            "os": platform.freedesktop_os_release(), "packages": packages,
            "scope": "installed_metadata_not_resolution_or_redistribution_approval"}


def negative(case):
    """Casos de política; não são evidência de operação nativa."""
    from keyring.backends.SecretService import Keyring
    environment = dict(os.environ)
    if case == "null":
        from keyring.backends.null import Keyring as Other
    elif case == "fail":
        from keyring.backends.fail import Keyring as Other
    elif case == "chain":
        from keyring.backends.chainer import ChainerBackend as Other
    elif case in ("plaintext", "alt-encrypted"):
        try:
            from keyrings.alt.file import PlaintextKeyring, EncryptedKeyring
        except ImportError:
            return {"outcome": "dependency_missing", "native_evidence": False}
        Other = PlaintextKeyring if case == "plaintext" else EncryptedKeyring
    elif case == "environment":
        environment["PYTHON_KEYRING_BACKEND"] = IDENTITY
        Other = Keyring
    elif case == "property":
        environment["KEYRING_PROPERTY_PREFERRED_COLLECTION"] = "/untrusted"
        Other = Keyring
    elif case == "spoofed":
        Other = type("Keyring", (), {"__module__": "keyring.backends.SecretService"})
    else:
        Other = type("Unexpected", (), {})
    # Sem discovery: nem os objetos recusados são inicializados.
    candidate = object.__new__(Other)
    try:
        approve(candidate, Keyring, environment)
    except Refusal as error:
        return {"outcome": str(error), "native_evidence": False}
    return {"outcome": "UNSAFE_ACCEPT", "native_evidence": False}


def no_collection_mutation(*args, **kwargs):
    raise Refusal("collection_mutation_or_prompt_forbidden")


def no_item_unlock(item):
    if item.is_locked():
        raise Refusal("locked")


def existing_owner(bus):
    from jeepney.bus_messages import message_bus
    from jeepney.wrappers import DBusErrorResponse, unwrap_msg

    try:
        owner, = unwrap_msg(bus.send_and_get_reply(
            message_bus.GetNameOwner("org.freedesktop.secrets"), timeout=5))
    except DBusErrorResponse as error:
        if error.name == "org.freedesktop.DBus.Error.NameHasNoOwner":
            raise Refusal("missing_service") from None
        raise
    if not isinstance(owner, str) or not owner.startswith(":"):
        raise Refusal("unexpected_service_owner")
    return owner


def lifecycle(backend, service, account, events):
    """Só consulta a chave exata recém-gerada. Limpeza mesmo após erro de escrita."""
    first, second = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    attempted = False
    try:
        attempted = True
        backend.set_password(service, account, first)
        events.append("create")
        if backend.get_password(service, account) != first:
            raise Refusal("read_mismatch")
        events.append("read")
        backend.set_password(service, account, second)
        if backend.get_password(service, account) != second:
            raise Refusal("rotate_mismatch")
        events.append("rotate")
        backend.delete_password(service, account)
        if backend.get_password(service, account) is not None:
            raise Refusal("delete_mismatch")
        events.append("delete")
    finally:
        if attempted:
            # Não executa get_secret na limpeza, nem busca por serviço genérico.
            backend.cleanup_exact(service, account)
            events.append("cleanup_verified")


def native(events):
    reject_overrides(os.environ)
    from keyring.backends.SecretService import Keyring
    import secretstorage
    from secretstorage.collection import Collection
    from secretstorage.item import Item
    from secretstorage.exceptions import LockedException

    # Sem autoativação de serviço, sem ReadAlias/listagem de coleções/itens.
    with closing(secretstorage.dbus_init()) as bus:
        owner = existing_owner(bus)
    backend = Keyring()
    approve(backend, Keyring, os.environ)
    service, account = "ayvu-test-" + uuid.uuid4().hex, "canary-" + uuid.uuid4().hex

    def cleanup_exact(service, account):
        with closing(secretstorage.dbus_init()) as bus:
            current = Collection(bus)
            if current.is_locked():
                raise Refusal("cleanup_unverified_locked")
            for item in current.search_items(backend._query(service, account)):
                item.delete()
            if list(current.search_items(backend._query(service, account))):
                raise Refusal("cleanup_unverified")

    backend.cleanup_exact = cleanup_exact
    # O backend exato continua fazendo CRUD. Guardas recusam caminhos upstream
    # que criariam/destrancariam coleções ou exibiriam prompts. Não simulam CRUD.
    def existing_collection():
        bus = secretstorage.dbus_init()
        try:
            current = Collection(bus)
            if current.is_locked():
                raise LockedException("locked")
            return current
        except BaseException:
            bus.close()
            raise

    backend.get_preferred_collection = existing_collection
    with ExitStack() as stack:
        # Destino único fixa o dono desta execução e impede autoativação/troca
        # entre a consulta de presença e as operações. Não atesta GNOME.
        stack.enter_context(patch.object(secretstorage.util, "BUS_NAME", owner))
        for owner, method in ((Collection, "unlock"), (Collection, "lock"),
                              (Collection, "delete"),
                              (secretstorage, "create_collection"),
                              (secretstorage, "get_default_collection"),
                              (secretstorage.collection, "exec_prompt"),
                              (secretstorage.item, "exec_prompt")):
            stack.enter_context(patch.object(owner, method, no_collection_mutation))
        stack.enter_context(patch.object(Item, "unlock", no_item_unlock))
        with closing(secretstorage.dbus_init()) as bus:
            collection = Collection(bus)
            if collection.is_locked():
                raise Refusal("locked")
        lifecycle(backend, service, account, events)
    return {"outcome": "crud_pass", "backend": IDENTITY, "native_evidence": True,
            "guarded_upstream": True, "service_identity": "not_attested",
            "packaged": False}


def failure_code(error):
    # Nomes/mensagens upstream nunca são retornados. Só comparações fechadas.
    if type(error).__name__ == "DBusErrorResponse":
        name = getattr(error, "name", None)
        return {"org.freedesktop.DBus.Error.AccessDenied": "denied",
                "org.freedesktop.Secret.Error.IsLocked": "locked",
                "org.freedesktop.DBus.Error.ServiceUnknown": "missing_service",
                "org.freedesktop.DBus.Error.NameHasNoOwner": "missing_service"}.get(name, "service_error")
    return {"PermissionError": "access_denied", "LockedException": "locked",
            "KeyringLocked": "locked", "PromptDismissedException": "denied",
            "ItemNotFoundException": "missing_collection",
            "SecretServiceNotAvailableException": "unavailable",
            "FileNotFoundError": "unavailable", "TimeoutError": "timeout"}.get(
                type(error).__name__, "probe_failed")


def worker(case):
    events = []
    try:
        if case == "inventory":
            result = inventory()
        elif case in ("native", "missing-bus", "missing-service"):
            result = native(events)
        else:
            result = negative(case)
    except Refusal as error:
        result = {"outcome": str(error), "native_evidence": False}
    except ImportError:
        result = {"outcome": "dependency_missing", "native_evidence": False}
    except Exception as error:
        # Nunca reflete mensagens, paths, namespace ou valores de bibliotecas.
        result = {"outcome": failure_code(error),
                  "native_evidence": False}
    result["events"] = events
    result["case"] = case
    result["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("outcome") in ("crud_pass", "backend_rejected", "override_rejected") or case == "inventory" and "packages" in result else 1


def launch(case, interpreter, work_root):
    with tempfile.TemporaryDirectory(prefix="ayvu-secret-probe-", dir=work_root) as folder:
        work = Path(folder)
        env = child_environment(work, case)
        command = [interpreter, "-I", str(Path(__file__).resolve()), "--worker", case]
        if case == "missing-service":
            command = ["/usr/bin/dbus-run-session", "--", *command]
        completed = subprocess.run(command,
                                   env=env, cwd=work, capture_output=True, text=True)
        # O trabalhador só imprime JSON sanitizado. stderr nunca é exportado.
        try:
            result = json.loads(completed.stdout)
        except (ValueError, TypeError):
            result = {"case": case, "outcome": "worker_failed", "native_evidence": False}
        result["isolated_files"] = sum(p.is_file() for p in work.rglob("*"))
        if result["isolated_files"]:
            result["outcome"] = "unexpected_files"
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return completed.returncode or (1 if result["isolated_files"] else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(*CASES, "inventory"), default="native")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--worker", choices=(*CASES, "inventory"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if not args.work_root or not args.work_root.is_dir():
        parser.error("--work-root exige uma pasta temporária existente")
    # Overrides herdados são recusados, não silenciados antes da decisão.
    try:
        reject_overrides(os.environ)
    except Refusal:
        print(json.dumps({"outcome": "override_rejected", "native_evidence": False}))
        return 1
    return launch(args.case, args.python, args.work_root)


if __name__ == "__main__":
    raise SystemExit(main())
