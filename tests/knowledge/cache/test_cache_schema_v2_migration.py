import errno
from contextlib import closing
import json
import os
import sqlite3
import threading

import pytest

from ayvu.knowledge.cache import schema as schema
from ayvu.v1.cache import text_hash

from tests.knowledge.cache.cache_schema_v2_support import Cancel, assert_v2, contents, make_v1
from tests.knowledge.cache.cache_schema_v2_support import state


def test_classification(tmp_path):
    p = tmp_path / "cache.sqlite"
    assert schema.classify_cache(p) == schema.SchemaState.ABSENT
    sqlite3.connect(p).close()
    assert schema.classify_cache(p) == schema.SchemaState.EMPTY
    make_v1(p)
    assert schema.classify_cache(p) == schema.SchemaState.V1
    schema.prepare_cache(p)
    assert schema.classify_cache(p) == schema.SchemaState.V2
    with closing(sqlite3.connect(p)) as db, db:
        db.execute("DROP TABLE cache_entries_v2")
    assert schema.classify_cache(p) == schema.SchemaState.PARTIAL
    foreign = tmp_path / "foreign.sqlite"
    with closing(sqlite3.connect(foreign)) as db, db:
        db.execute("CREATE TABLE stranger (x)")
    assert schema.classify_cache(foreign) == schema.SchemaState.FOREIGN
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not sqlite")
    assert schema.classify_cache(corrupt) == schema.SchemaState.CORRUPT


@pytest.mark.parametrize("phase", schema.PHASES)
@pytest.mark.parametrize("failure", [RuntimeError, Cancel])
def test_phase_failure_and_reopen(tmp_path, phase, failure):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    seen = []
    def checkpoint(current):
        seen.append(current)
        if current == phase:
            raise failure()
    with pytest.raises(failure):
        schema.prepare_cache(p, checkpoint=checkpoint)
    assert seen == list(schema.PHASES[:schema.PHASES.index(phase) + 1])
    record = json.loads(schema._marker(p).read_text())
    assert record["phase"] == phase
    assert state(p) == (schema.SchemaState.V2 if phase == "committed" else schema.SchemaState.V1)
    assert contents(p) == rows
    if phase != "classified":
        assert state(schema._backup(p)) == schema.SchemaState.V1
        assert contents(schema._backup(p)) == rows
    schema.prepare_cache(p)
    assert_v2(p, rows)
    assert not schema._marker(p).exists()
    assert contents(schema._backup(p)) == rows
    schema.prepare_cache(p)
    assert_v2(p, rows)


def test_wal_snapshot_and_reader_blocks(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    holder = sqlite3.connect(p, isolation_level=None)
    try:
        assert holder.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        holder.execute("INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text) VALUES (?,?,?,?,?)", ("en", "pt", text_hash("wal"), "wal", "registro"))
        rows = contents(p)
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM translations").fetchall()
        with pytest.raises(schema.CacheSchemaError) as exc:
            schema.prepare_cache(p)
        assert exc.value.code == "cache_busy"
        assert contents(p) == rows
        holder.execute("ROLLBACK")
    finally:
        holder.close()
    schema.prepare_cache(p)
    assert_v2(p, rows)
    assert contents(schema._backup(p)) == rows


def test_concurrent_migrators_and_worker_gate(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    entered, release = threading.Event(), threading.Event()
    errors = []
    def checkpoint(phase):
        if phase == "classified":
            entered.set()
            assert release.wait(10)
    def migrate():
        try:
            schema.prepare_cache(p, checkpoint=checkpoint)
        except BaseException as e:
            errors.append(e)
    t = threading.Thread(target=migrate)
    t.start()
    try:
        assert entered.wait(10)
        with pytest.raises(schema.CacheSchemaError) as exc:
            schema.prepare_cache(p)
        assert exc.value.code == "cache_busy"
    finally:
        release.set()
        t.join(10)
    assert not t.is_alive() and not errors
    with schema.participating_connection(p) as db:
        assert db.execute("SELECT COUNT(*) FROM cache_entries_v2").fetchone() == (0,)
    assert_v2(p, rows)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal", ".lock", ".v1-backup.sqlite", ".v1-backup.sqlite-wal", ".v1-backup.sqlite.partial", ".v1-backup.sqlite.partial-journal", ".migration", ".migration-next"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_artifacts(tmp_path, suffix, kind):
    p = tmp_path / "cache.sqlite"
    victim = p.with_name(p.name + suffix)
    if suffix:
        make_v1(p)
    target = tmp_path / "target"
    target.write_bytes(b"target")
    if kind == "symlink":
        victim.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, victim)
    else:
        os.mkfifo(victim)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "unsafe_cache_path"
    assert target.read_bytes() == b"target"


def test_unauthorized_and_unsupported(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    before = p.read_bytes()
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p, authorized_directory=other)
    assert exc.value.code == "unsafe_cache_path"
    monkeypatch.setattr(schema, "_supported", lambda path: False)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_platform_unsupported"
    assert p.read_bytes() == before


@pytest.mark.parametrize("gate", ["after-application-lock", "after-sqlite-open", "before-backup", "before-promotion"])
def test_identity_exchange_at_gate(tmp_path, monkeypatch, gate):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    replacement = tmp_path / "replacement"
    make_v1(replacement)
    original = schema._revalidate
    def exchange(path, expected, current):
        if current == gate:
            os.replace(replacement, path)
        return original(path, expected, current)
    monkeypatch.setattr(schema, "_revalidate", exchange)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_identity_changed"
    assert state(p) == schema.SchemaState.V1


@pytest.mark.parametrize("number,code", [(errno.ENOSPC, "cache_disk_full"), (errno.EACCES, "cache_permission_denied")])
def test_snapshot_error_preserves_source_and_reopens(tmp_path, monkeypatch, number, code):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    original = schema._snapshot
    def fail(*args):
        raise OSError(number, "injected")
    monkeypatch.setattr(schema, "_snapshot", fail)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == code
    assert state(p) == schema.SchemaState.V1 and contents(p) == rows
    assert json.loads(schema._marker(p).read_text())["phase"] == "classified"
    monkeypatch.setattr(schema, "_snapshot", original)
    schema.prepare_cache(p)
    assert_v2(p, rows)


def test_existing_and_invalid_backup_refused(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    backup = schema._backup(p)
    backup.write_bytes(b"invalid")
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_backup_exists"
    assert contents(p) == rows and backup.read_bytes() == b"invalid"
    backup.unlink()
    def stop(phase):
        if phase == "classified":
            raise Cancel()
    with pytest.raises(Cancel):
        schema.prepare_cache(p, checkpoint=stop)
    backup.write_bytes(b"invalid")
    with pytest.raises(schema.CacheSchemaError):
        schema.prepare_cache(p)
    assert contents(p) == rows and backup.read_bytes() == b"invalid"


def test_project_files_untouched(tmp_path):
    project = tmp_path / "project.json"
    project.write_bytes(b'{"schema_version": 1}\n')
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    before = project.read_bytes()
    schema.prepare_cache(p)
    assert project.read_bytes() == before


def test_snapshot_includes_uncheckpointed_wal(tmp_path, monkeypatch):
    import subprocess
    import sys

    p = tmp_path / "cache.sqlite"
    make_v1(p)
    script = """import os, sqlite3, sys
p = sys.argv[1]
db = sqlite3.connect(p, isolation_level=None)
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text) VALUES (?,?,?,?,?)', ('en','pt',sys.argv[2],'wal-only','registro'))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(p), text_hash("wal-only")], check=True)
    assert p.with_name(p.name + "-wal").stat().st_size > 0
    trace = []
    connect = schema._connect
    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(trace.append)
        return conn
    monkeypatch.setattr(schema, "_connect", traced)
    schema.prepare_cache(p)
    assert any(sql.upper().startswith("VACUUM INTO") for sql in trace)
    assert [row[4] for row in contents(schema._backup(p))] == ["hello", "wal-only"]
    assert_v2(p, contents(schema._backup(p)))


def test_marker_flush_failure_keeps_v1_and_recovers(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    flush = schema._flush_directory
    def fail(path):
        raise OSError(errno.ENOSPC, "injected")
    monkeypatch.setattr(schema, "_flush_directory", fail)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_disk_full"
    assert state(p) == schema.SchemaState.V1 and contents(p) == rows
    assert json.loads(schema._marker(p).read_text())["phase"] == "classified"
    monkeypatch.setattr(schema, "_flush_directory", flush)
    schema.prepare_cache(p)
    assert_v2(p, rows)


def test_schema_build_failure_rolls_back_and_recovers(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    build = schema._build
    def fail(conn):
        build(conn)
        raise Cancel()
    monkeypatch.setattr(schema, "_build", fail)
    with pytest.raises(Cancel):
        schema.prepare_cache(p)
    assert state(p) == schema.SchemaState.V1 and contents(p) == rows
    assert json.loads(schema._marker(p).read_text())["phase"] == "migration-pending"
    monkeypatch.setattr(schema, "_build", build)
    schema.prepare_cache(p)
    assert_v2(p, rows)
