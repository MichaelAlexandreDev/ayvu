"""Pre-open refusal, race and existing-lock proofs for the read-only adapter."""
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from ayvu.knowledge.cache import read_only
from ayvu.knowledge.cache import read_only_queries
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.schema import CacheSchemaError, LEGACY_SCHEMA
from tests.support.cache_repository_v2_support import entry
from .read_only_support import change, prepared, read_guard, reader_records, snapshot


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal", ".migration", ".migration-next",
                                  ".v1-backup.sqlite.partial"])
def test_artifacts_are_refused_before_sqlite_open(prepared, read_guard, suffix):
    path, _, item = prepared
    path.with_name(path.name + suffix).write_bytes(b"CANARY pending recovery")
    with read_guard() as sql:
        reader = ReadOnlyExactCache(path)
        for operation in (lambda: reader.lookup(item.key), lambda: reader_records(reader, item)):
            with pytest.raises(CacheSchemaError, match="cache_recovery_required") as error:
                operation()
            assert "CANARY" not in repr(error.value)
    assert not sql


def test_wal_header_without_sidecars_is_refused_before_sqlite_open(prepared, read_guard):
    path, _, item = prepared
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.close()
    assert not path.with_name(path.name + "-wal").exists()
    assert path.read_bytes()[18:20] == b"\x02\x02"
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_recovery_required"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not sql


@pytest.mark.parametrize("sql", ["DROP TABLE cache_schema", "DROP INDEX cache_entries_v2_compatible",
                                 "PRAGMA user_version=1"])
def test_partial_schema_requires_preparation_without_payload_reads(prepared, read_guard, sql):
    path, _, item = prepared
    change(path, sql)
    with read_guard() as statements:
        with pytest.raises(CacheSchemaError, match="cache_recovery_required"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not any(statement.startswith("SELECT source_text,value,") for statement in statements)


def test_old_v1_schema_at_v2_path_is_not_migrated(tmp_path, read_guard):
    path = tmp_path / "old.v2.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
    with read_guard():
        with pytest.raises(CacheSchemaError, match="cache_recovery_required"):
            ReadOnlyExactCache(path).lookup(entry().key)
    assert not path.with_name(path.name + ".lock").exists()


def test_exact_primary_key_prevents_duplicates(prepared, read_guard):
    path, _, item = prepared
    with pytest.raises(sqlite3.IntegrityError):
        change(path, "INSERT INTO cache_entries_v2 SELECT * FROM cache_entries_v2")
    with read_guard():
        assert ReadOnlyExactCache(path).lookup(item.key).value == item.value


def test_inode_replacement_between_validation_and_open_is_refused(prepared, read_guard, monkeypatch):
    path, _, item = prepared
    replacement = path.with_name("replacement.sqlite")
    shutil.copy2(path, replacement)
    original = read_only._connect
    old_inode = path.stat().st_ino
    def replace_and_open(target):
        # The actor exchanges byte-identical files while preserving all snapshot
        # fields. Only inode identity changes; the reader must detect that too.
        directory = path.parent.stat()
        temporary = path.with_name("exchange.sqlite")
        path.rename(temporary)
        replacement.rename(path)
        temporary.rename(replacement)
        os.utime(path.parent, ns=(directory.st_atime_ns, directory.st_mtime_ns))
        assert path.stat().st_ino != old_inode
        return original(target)
    monkeypatch.setattr(read_only, "_connect", replace_and_open)
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_identity_changed"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not sql


@pytest.mark.parametrize("suffix", [".migration", ".migration-next", ".lock", "-wal", "-journal"])
def test_artifact_appearing_after_validation_is_refused(prepared, read_guard, monkeypatch, suffix):
    path, _, item = prepared
    if suffix == ".lock":
        path.with_name(path.name + suffix).unlink()
    artifact = path.with_name(path.name + suffix)
    original = read_only._connect
    actor_state = []
    directory = path.parent.stat()
    def open_then_mark(target):
        connection = original(target)
        artifact.write_bytes(b"CANARY actor marker")
        actor_state.append(snapshot(path.parent))
        return connection
    monkeypatch.setattr(read_only, "_connect", open_then_mark)
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_identity_changed"):
            ReadOnlyExactCache(path).lookup(item.key)
        assert snapshot(path.parent) == actor_state[0]
        # Only the simulated actor's mutation is undone for the outer snapshot.
        artifact.unlink()
        os.utime(path.parent, ns=(directory.st_atime_ns, directory.st_mtime_ns))
    assert not sql


def test_existing_lock_is_shared_readonly_and_unchanged(prepared, read_guard, monkeypatch):
    path, _, item = prepared
    lock = path.with_name(path.name + ".lock")
    lock.chmod(0o440)
    acquired = []
    original = read_only.fcntl.flock
    def shared(descriptor, flags):
        assert flags == read_only.fcntl.LOCK_SH | read_only.fcntl.LOCK_NB
        assert read_only.fcntl.fcntl(descriptor, read_only.fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        acquired.append(1)
        return original(descriptor, flags)
    monkeypatch.setattr(read_only.fcntl, "flock", shared)
    with read_guard():
        assert ReadOnlyExactCache(path).lookup(item.key).value == item.value
    assert acquired == [1] and lock.stat().st_mode & 0o777 == 0o440


def test_exclusive_protocol_lock_is_a_typed_busy_refusal(prepared, read_guard):
    path, _, item = prepared
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_RDONLY)
    try:
        read_only.fcntl.flock(descriptor, read_only.fcntl.LOCK_EX | read_only.fcntl.LOCK_NB)
        with read_guard() as sql:
            with pytest.raises(CacheSchemaError, match="cache_busy"):
                ReadOnlyExactCache(path).lookup(item.key)
        assert not sql
    finally:
        os.close(descriptor)


def test_bad_database_header_never_opens_sqlite(prepared, read_guard):
    path, _, item = prepared
    path.write_bytes(b"CANARY not a database")
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_corrupt"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not sql


def test_real_hot_journal_is_not_recovered(prepared, read_guard):
    path, _, item = prepared
    script = (
        "import os, sqlite3, sys\n"
        "connection = sqlite3.connect(sys.argv[1])\n"
        "connection.execute('PRAGMA cache_size=1')\n"
        "connection.execute('BEGIN IMMEDIATE')\n"
        "connection.execute('UPDATE cache_entries_v2 SET value=zeroblob(4194304)')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=30)
    journal = path.with_name(path.name + "-journal")
    assert journal.stat().st_size > 512 and any(journal.read_bytes()[:8])
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_recovery_required"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not sql


def test_marker_appearing_during_payload_read_discards_the_result(prepared, read_guard, monkeypatch):
    path, _, item = prepared
    marker = path.with_name(path.name + ".migration")
    original = read_only_queries._record
    actor_state = []
    directory = path.parent.stat()
    def read_then_mark(connection, key):
        record = original(connection, key)
        marker.write_bytes(b"CANARY migration during read")
        actor_state.append(snapshot(path.parent))
        return record
    monkeypatch.setattr(read_only_queries, "_record", read_then_mark)
    with read_guard():
        with pytest.raises(CacheSchemaError, match="cache_identity_changed"):
            ReadOnlyExactCache(path).lookup(item.key)
        assert snapshot(path.parent) == actor_state[0]
        marker.unlink()
        os.utime(path.parent, ns=(directory.st_atime_ns, directory.st_mtime_ns))


def test_symlink_is_refused_without_touching_its_target(prepared, read_guard):
    path, _, item = prepared
    alias = path.with_name("alias.sqlite")
    alias.symlink_to(path.name)
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="unsafe_cache_path"):
            ReadOnlyExactCache(alias).lookup(item.key)
    assert not sql
