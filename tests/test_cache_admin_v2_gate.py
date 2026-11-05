"""The legacy administrative API must not touch v2 or interrupted migrations."""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from ayvu.cache import SCHEMA
from ayvu.cache_admin import (CacheRef, FileRef, CacheFilter, CleanSelection,
    InspectRequest, PreviewCleanRequest, ExecuteCleanRequest, ExportRequest, ImportRequest)
from ayvu.cache_admin_composition import cache_admin_services


def _v1(path):
    with sqlite3.connect(path) as conn:
        conn.execute(SCHEMA)
        conn.execute("INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)",
                     ("en", "pt", hashlib.sha256(b"hello").hexdigest(), "hello", "ola", "2024-01-01"))


def test_legacy_inspect_export_leave_cache_identical(tmp_path):
    path = tmp_path / "cache.sqlite"
    _v1(path)
    before = path.read_bytes()
    services = cache_admin_services()
    ref = CacheRef(str(path))
    assert services.inspect.execute(InspectRequest(ref)).value.count == 1
    output = tmp_path / "out.json"
    assert services.export.execute(ExportRequest(ref, FileRef(str(output)))).value == 1
    assert json.loads(output.read_text())["entries"][0]["original_text"] == "hello"
    assert path.read_bytes() == before


def test_absent_read_does_not_create_cache_directory(tmp_path):
    cache = tmp_path / "absent" / "cache.sqlite"
    ref = CacheRef(str(cache))
    services = cache_admin_services()
    assert not services.inspect.execute(InspectRequest(ref)).value.exists
    assert services.export.execute(ExportRequest(ref, FileRef(str(tmp_path / "out.json")))).value == 0
    assert not cache.parent.exists()


@pytest.mark.parametrize("suffix", ["", ".migration", ".migration-next"])
def test_absent_read_rejects_dangling_alias(tmp_path, suffix):
    cache = tmp_path / "cache.sqlite"
    cache.with_name(cache.name + suffix).symlink_to(tmp_path / "missing-target")
    services = cache_admin_services()
    ref = CacheRef(str(cache))
    output = tmp_path / "out.json"
    for result in (services.inspect.execute(InspectRequest(ref)),
                   services.export.execute(ExportRequest(ref, FileRef(str(output))))):
        assert result.failure.code == "unsafe_cache_path"
    assert not cache.exists() and not output.exists()


@pytest.mark.parametrize("suffix", [".migration", ".migration-next"])
def test_missing_cache_with_migration_marker_is_not_treated_as_empty(tmp_path, suffix):
    cache = tmp_path / "cache.sqlite"
    marker = tmp_path / ("cache.sqlite" + suffix)
    marker.write_text("interrupted")
    ref = CacheRef(str(cache))
    services = cache_admin_services()
    result = services.inspect.execute(InspectRequest(ref))
    assert result.failure.code == "v2-admin-required"
    output = tmp_path / "out.json"
    result = services.export.execute(ExportRequest(ref, FileRef(str(output))))
    assert result.failure.code == "v2-admin-required"
    assert not cache.exists() and not output.exists()


@pytest.mark.parametrize("state", ["v2", "partial", "view", "trigger"])
def test_v2_and_partial_refuse_all_legacy_operations(tmp_path, state):
    from ayvu.cache_schema import SchemaState, classify_cache, prepare_cache
    path = tmp_path / "cache.sqlite"
    _v1(path)
    prepare_cache(path)
    if state == "partial":
        path.with_name(path.name + ".migration").write_text("interrupted")
    elif state in ("view", "trigger"):
        with sqlite3.connect(path) as conn:
            if state == "view":
                conn.execute("CREATE VIEW extra_view AS SELECT * FROM translations")
            else:
                conn.execute("CREATE TRIGGER extra_trigger AFTER INSERT ON translations BEGIN SELECT 1; END")
        assert classify_cache(path) is SchemaState.PARTIAL
    before = path.read_bytes()
    source = tmp_path / "in.json"
    source.write_text('{"version": 1, "entries": []}')
    output = tmp_path / "out.json"
    output.write_bytes(b"do not replace")
    ref = CacheRef(str(path))
    filters = CacheFilter(all_entries=True)
    selection = CleanSelection(ref, filters, 0, hashlib.sha256(b"").hexdigest())
    services = cache_admin_services()
    results = [services.inspect.execute(InspectRequest(ref)),
               services.preview.execute(PreviewCleanRequest(ref, filters)),
               services.clean.execute(ExecuteCleanRequest(selection)),
               services.export.execute(ExportRequest(ref, FileRef(str(output)), overwrite=True)),
               services.import_cache.execute(ImportRequest(ref, FileRef(str(source))))]
    assert [result.failure.code for result in results] == ["v2-admin-required"] * 5
    assert path.read_bytes() == before
    assert output.read_bytes() == b"do not replace"


def test_admin_read_waits_for_migration_and_rechecks_schema(tmp_path, monkeypatch):
    from ayvu import cache_schema as schema
    path = tmp_path / "cache.sqlite"
    _v1(path)
    migration_paused = threading.Event()
    release_migration = threading.Event()
    shared_requested = threading.Event()
    read_completed = threading.Event()
    errors, results = [], []
    original_lock = schema._application_lock

    @contextmanager
    def observed_lock(path, *, exclusive=False, create=False):
        if not exclusive:
            shared_requested.set()
        with original_lock(path, exclusive=exclusive, create=create) as verify_lock:
            yield verify_lock

    monkeypatch.setattr(schema, "_application_lock", observed_lock)

    def checkpoint(phase):
        if phase == "classified":
            migration_paused.set()
            if not release_migration.wait(10):
                raise AssertionError("migration was not released")

    def migrate():
        try:
            schema.prepare_cache(path, checkpoint=checkpoint)
        except BaseException as exc:
            errors.append(exc)

    def inspect():
        try:
            results.append(cache_admin_services().inspect.execute(InspectRequest(CacheRef(str(path)))))
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_completed.set()

    migration = threading.Thread(target=migrate)
    reader = threading.Thread(target=inspect)
    migration.start()
    try:
        assert migration_paused.wait(10)
        reader.start()
        assert shared_requested.wait(10)
        assert not read_completed.is_set()
    finally:
        release_migration.set()
        migration.join(10)
        if reader.ident is not None:
            reader.join(10)
    assert not migration.is_alive() and not reader.is_alive()
    assert not errors
    assert results[0].failure.code == "v2-admin-required"


def test_cli_gate_uses_isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner
    from ayvu.cli import app
    from ayvu.cache_schema import prepare_cache
    path = tmp_path / "cache.sqlite"
    _v1(path)
    prepare_cache(path)
    before = path.read_bytes()
    result = CliRunner().invoke(app, ["cache", "export", "out.json", "--cache", str(path)])
    assert result.exit_code != 0
    assert "v2-admin-required" in result.output
    assert not (tmp_path / "out.json").exists()
    assert path.read_bytes() == before
