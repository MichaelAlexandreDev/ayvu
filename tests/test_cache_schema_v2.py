import errno
from contextlib import closing
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from ayvu import cache_schema as schema
from ayvu.cache import SCHEMA, text_hash


class Cancel(BaseException):
    pass


def make_v1(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(SCHEMA)
        db.execute("INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text) VALUES (?,?,?,?,?)", ("en", "pt", text_hash("hello"), "hello", "olá"))


def contents(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT * FROM translations").fetchall()


def state(path):
    with schema._connection_closed(schema._connect(path)) as db:
        return schema._state(db)


def assert_v2(path, rows):
    assert state(path) == schema.SchemaState.V2
    assert contents(path) == rows
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (0,)
        assert db.execute("SELECT * FROM cache_schema").fetchall() == [(1, 2, "legacy-v1")]


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


@pytest.mark.parametrize("phase", schema.PHASES)
def test_process_crash_at_every_phase(tmp_path, phase):
    import subprocess
    import sys

    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    script = """import os, sys
from ayvu.cache_schema import prepare_cache
def checkpoint(phase):
    if phase == sys.argv[2]:
        os._exit(73)
prepare_cache(sys.argv[1], checkpoint=checkpoint)
"""
    result = subprocess.run([sys.executable, "-c", script, str(p), phase], check=False)
    assert result.returncode == 73
    assert json.loads(schema._marker(p).read_text())["phase"] == phase
    # Open read-write to allow SQLite to recover any hot rollback journal first.
    with closing(sqlite3.connect(p)) as db:
        db.execute("BEGIN IMMEDIATE")
        assert schema._state(db) == (schema.SchemaState.V2 if phase == "committed" else schema.SchemaState.V1)
        db.rollback()
    assert contents(p) == rows
    assert schema.classify_cache(p) == schema.SchemaState.PARTIAL
    schema.prepare_cache(p)
    assert_v2(p, rows)
    schema.prepare_cache(p)
    assert_v2(p, rows)


def test_pending_next_requires_recovery(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    schema._next_marker(p).write_text("interrupted publication")
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_recovery_required"
    assert contents(p) == rows
    assert schema._next_marker(p).read_text() == "interrupted publication"


def test_tampered_backup_and_marker_block_workers(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    def stop(phase):
        if phase == "backup-verified":
            raise Cancel()
    with pytest.raises(Cancel):
        schema.prepare_cache(p, checkpoint=stop)
    with pytest.raises(schema.CacheSchemaError) as exc:
        with schema.participating_connection(p):
            pytest.fail("worker must not read during recovery")
    assert exc.value.code == "cache_recovery_required"
    backup = schema._backup(p)
    with sqlite3.connect(backup) as db:
        db.execute("UPDATE translations SET translated_text='altered'")
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_backup_invalid"
    assert contents(p) == rows


def test_v1_later_write_cannot_be_exact_hit(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    schema.prepare_cache(p)
    with closing(sqlite3.connect(p)) as db, db:
        db.execute("INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text) VALUES (?,?,?,?,?)", ("en", "pt", text_hash("new"), "new", "novo"))
    with schema.participating_connection(p) as db:
        assert db.execute("SELECT count(*) FROM translations").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (0,)


def test_restricted_artifact_modes_and_project_ledger(tmp_path):
    import stat
    project = tmp_path / "project.sqlite"
    with closing(sqlite3.connect(project)) as db, db:
        fixture = Path(__file__).parent / "fixtures" / "migrations" / "version-4.sql"
        db.executescript(fixture.read_text())
        ledger = db.execute("SELECT * FROM ayvu_schema_migrations").fetchall()
    before = project.read_bytes()
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    def stop(phase):
        if phase == "backup-verified":
            raise Cancel()
    with pytest.raises(Cancel):
        schema.prepare_cache(p, checkpoint=stop)
    assert stat.S_IMODE(schema._marker(p).stat().st_mode) == 0o600
    assert stat.S_IMODE(schema._backup(p).stat().st_mode) == 0o600
    schema.prepare_cache(p)
    assert project.read_bytes() == before
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(project)
    assert exc.value.code == "cache_foreign"
    assert project.read_bytes() == before
    with closing(sqlite3.connect(project)) as db:
        assert db.execute("SELECT * FROM ayvu_schema_migrations").fetchall() == ledger


def test_shared_application_lock_alone_blocks_prepare(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    # No SQLite connection is held: exclusion must come from flock itself.
    with schema._application_lock(p, create=True):
        with pytest.raises(schema.CacheSchemaError) as exc:
            schema.prepare_cache(p)
        assert exc.value.code == "cache_busy"
    assert state(p) == schema.SchemaState.V1 and contents(p) == rows
    schema.prepare_cache(p)
    assert_v2(p, rows)


def test_backup_identity_is_rechecked_before_commit(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    check = schema._revalidate
    def exchange(path, expected, gate):
        if gate == "before-promotion":
            replacement = tmp_path / "same-backup-different-inode"
            replacement.write_bytes(schema._backup(path).read_bytes())
            os.replace(replacement, schema._backup(path))
        check(path, expected, gate)
    monkeypatch.setattr(schema, "_revalidate", exchange)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_identity_changed"
    assert state(p) == schema.SchemaState.V1 and contents(p) == rows


@pytest.mark.parametrize("cancel", [False, True])
def test_worker_waits_for_migration_and_checks_recovery(tmp_path, monkeypatch, cancel):
    from contextlib import contextmanager
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    reached, release, requested, completed = (threading.Event() for _ in range(4))
    errors, outcomes = [], []
    original = schema._application_lock
    @contextmanager
    def observed(path, *, exclusive=False, create=False):
        if not exclusive:
            requested.set()
        with original(path, exclusive=exclusive, create=create) as verify:
            yield verify
    monkeypatch.setattr(schema, "_application_lock", observed)
    def checkpoint(phase):
        if phase == "classified":
            reached.set()
            assert release.wait(10)
            if cancel:
                raise Cancel()
    def migrate():
        try:
            schema.prepare_cache(p, checkpoint=checkpoint)
        except Cancel:
            if not cancel:
                errors.append("unexpected cancellation")
        except BaseException as exc:
            errors.append(type(exc).__name__)
    def work():
        try:
            with schema.participating_connection(p) as db:
                outcomes.append(db.execute("SELECT count(*) FROM cache_entries_v2").fetchone()[0])
        except schema.CacheSchemaError as exc:
            outcomes.append(exc.code)
        finally:
            completed.set()
    migrator, worker = threading.Thread(target=migrate), threading.Thread(target=work)
    migrator.start()
    try:
        assert reached.wait(10)
        worker.start()
        assert requested.wait(10)
        assert not completed.is_set()
    finally:
        release.set()
        migrator.join(10)
        if worker.ident is not None:
            worker.join(10)
    assert not migrator.is_alive() and not worker.is_alive() and not errors
    assert outcomes == (["cache_recovery_required"] if cancel else [0])


def test_new_schema_failure_leaves_empty_reopenable_cache(tmp_path, monkeypatch):
    p = tmp_path / "new" / "cache.sqlite"
    build = schema._build
    def fail(conn):
        build(conn)
        raise Cancel()
    monkeypatch.setattr(schema, "_build", fail)
    with pytest.raises(Cancel):
        schema.prepare_cache(p)
    assert schema.classify_cache(p) == schema.SchemaState.EMPTY
    monkeypatch.setattr(schema, "_build", build)
    schema.prepare_cache(p)
    assert_v2(p, [])


def test_protocol_rejects_symlinked_authorized_parent(tmp_path):
    real, alias = tmp_path / "real", tmp_path / "alias"
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(alias / "cache.sqlite", authorized_directory=alias)
    assert exc.value.code == "unsafe_cache_path"
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("pragma", ["application_id=123", "user_version=7"])
@pytest.mark.parametrize("legacy_shape", [False, True])
def test_foreign_sqlite_ownership_is_not_relabeled(tmp_path, pragma, legacy_shape):
    p = tmp_path / "foreign.sqlite"
    if legacy_shape:
        make_v1(p)
    with closing(sqlite3.connect(p)) as db:
        db.execute("PRAGMA " + pragma)
    before = p.read_bytes()
    assert schema.classify_cache(p) == schema.SchemaState.FOREIGN
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_foreign"
    assert p.read_bytes() == before
    assert not p.with_name(p.name + ".lock").exists()


def test_classification_io_errors_are_bounded_and_redacted(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, "CANARY-SECRET https://private.invalid /unrelated/private")
    monkeypatch.setattr(schema, "_connect", fail)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.classify_cache(p)
    assert exc.value.code == "cache_permission_denied"
    assert "CANARY" not in repr(exc.value) + str(exc.value)
    assert "/unrelated" not in repr(exc.value) + str(exc.value)


def test_unsupported_source_and_supported_new_destination(tmp_path, monkeypatch):
    from ayvu.cache_repository import SQLiteExactCache
    source = tmp_path / "legacy.sqlite"
    make_v1(source)
    original = source.read_bytes()
    supported = schema._supported
    monkeypatch.setattr(schema, "_supported", lambda path: False if path == source else supported(path))
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(source)
    assert exc.value.code == "cache_platform_unsupported"
    destination = tmp_path / "new" / "v2.sqlite"
    SQLiteExactCache(destination)
    assert source.read_bytes() == original
    assert_v2(destination, [])


def test_globally_unsupported_creates_no_parent(tmp_path, monkeypatch):
    p = tmp_path / "absent" / "cache.sqlite"
    monkeypatch.setattr(schema, "_supported", lambda path: False)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)
    assert exc.value.code == "cache_platform_unsupported"
    assert not p.parent.exists()


def test_lock_replaced_before_promotion_rolls_back(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    lock = p.with_name(p.name + ".lock")
    replacement = tmp_path / "replacement-lock"
    replacement.write_bytes(b"")
    def checkpoint(phase):
        if phase == "integrity-verified":
            os.replace(replacement, lock)
    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p, checkpoint=checkpoint)
    assert exc.value.code == "cache_identity_changed"
    assert state(p) == schema.SchemaState.V1 and contents(p) == rows


def test_permissions_after_migration(tmp_path):
    import stat
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    schema.prepare_cache(p)
    for artifact in (p, schema._backup(p), p.with_name(p.name + ".lock")):
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def _classified_then_stopped(p):
    def stop(phase):
        if phase == "classified":
            raise KeyboardInterrupt
    with pytest.raises(BaseException):
        schema.prepare_cache(p, checkpoint=stop)
    assert not schema._backup(p).exists()


@pytest.mark.parametrize("sidecar", ["", "-journal"])
def test_a_crash_during_the_snapshot_is_redone_on_reopen(tmp_path, sidecar):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    _classified_then_stopped(p)
    partial = schema._partial_backup(p)
    partial.write_bytes(b"half a database")
    if sidecar:
        partial.with_name(partial.name + sidecar).write_bytes(b"journal")

    schema.prepare_cache(p)

    assert_v2(p, rows)
    assert contents(schema._backup(p)) == rows
    assert not any(item.exists() for item in schema._partial_backup_files(p))


def test_a_crash_right_before_publishing_the_snapshot_is_redone(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)

    def die(*_args, **_kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(schema.os, "rename", die)
        with pytest.raises(BaseException):
            schema.prepare_cache(p)
    assert schema._partial_backup(p).exists() and not schema._backup(p).exists()

    schema.prepare_cache(p)

    assert_v2(p, rows)
    assert contents(schema._backup(p)) == rows
    assert not any(item.exists() for item in schema._partial_backup_files(p))


def test_a_crash_right_after_publishing_the_snapshot_keeps_it(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    real_flush = schema._flush_directory

    def die_once_the_backup_exists(path):
        if schema._backup(p).exists():
            raise KeyboardInterrupt
        return real_flush(path)

    with monkeypatch.context() as patch:
        patch.setattr(schema, "_flush_directory", die_once_the_backup_exists)
        with pytest.raises(BaseException):
            schema.prepare_cache(p)
    assert schema._backup(p).exists() and schema._backup(p).stat().st_nlink == 1
    identity = schema._backup(p).stat().st_ino

    schema.prepare_cache(p)

    assert_v2(p, rows)
    assert schema._backup(p).stat().st_ino == identity


def test_a_partial_snapshot_without_a_marker_is_not_taken_as_ours(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    partial = schema._partial_backup(p)
    partial.write_bytes(b"someone else's file")

    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)

    assert exc.value.code == "cache_backup_exists"
    assert partial.read_bytes() == b"someone else's file"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_an_alias_as_partial_snapshot_is_refused_and_left_alone(tmp_path, kind):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    _classified_then_stopped(p)
    outside = tmp_path / "outside"
    outside.write_bytes(b"precious")
    partial = schema._partial_backup(p)
    if kind == "symlink":
        partial.symlink_to(outside)
    else:
        os.link(outside, partial)

    with pytest.raises(schema.CacheSchemaError):
        schema.prepare_cache(p)

    assert outside.read_bytes() == b"precious"
