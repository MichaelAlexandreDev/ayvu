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
from tests.support.paths import TESTS_ROOT


@pytest.mark.parametrize("phase", schema.PHASES)
def test_process_crash_at_every_phase(tmp_path, phase):
    import subprocess
    import sys

    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    script = """import os, sys
from ayvu.knowledge.cache.schema import prepare_cache
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
        fixture = TESTS_ROOT / "fixtures" / "migrations" / "version-4.sql"
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
    from ayvu.knowledge.cache.repository import SQLiteExactCache
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


def _lstat_reporting(monkeypatch, target, nlink):
    """lstat of ``target`` reports a regular file with ``nlink`` links, as during a concurrent unlink."""
    real = type(target).lstat

    def lstat(path):
        if os.fspath(path) == os.fspath(target):
            import stat as stat_module
            return os.stat_result((stat_module.S_IFREG | 0o600, 1234, 1, nlink, os.getuid(), os.getgid(), 0, 0, 0, 0))
        return real(path)
    monkeypatch.setattr(type(target), "lstat", lstat)


def test_a_journal_unlinked_while_checked_reads_as_absent(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    schema.prepare_cache(p)
    # SQLite deletes the rollback journal at commit; another worker's lstat can catch it with no links left.
    _lstat_reporting(monkeypatch, p.with_name(p.name + "-journal"), nlink=0)
    with schema.participating_connection(p) as connection:
        assert connection.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (0,)


def test_a_hard_linked_journal_is_still_refused(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    schema.prepare_cache(p)
    _lstat_reporting(monkeypatch, p.with_name(p.name + "-journal"), nlink=2)
    with pytest.raises(schema.CacheSchemaError) as exc:
        with schema.participating_connection(p):
            pass
    assert exc.value.code == "unsafe_cache_path"
