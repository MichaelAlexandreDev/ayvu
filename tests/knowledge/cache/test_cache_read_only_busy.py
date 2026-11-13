"""A live rollback writer is busy; an abandoned journal requires recovery."""
import sqlite3
import subprocess
import sys

import pytest

from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.schema import CacheSchemaError
from tests.knowledge.cache.read_only_support import prepared, read_guard
from tests.support.transient_translation_support import active_writer


def test_live_writer_journal_is_busy_without_sqlite_open(prepared, read_guard):
    path, _, item = prepared
    with active_writer(path), read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_busy"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not sql
    with read_guard():
        assert ReadOnlyExactCache(path).lookup(item.key).value == item.value


def test_probe_preserves_a_writer_in_the_same_process(prepared, monkeypatch):
    from ayvu.knowledge.cache import read_only
    path, _, item = prepared
    def snapshot_in_child():
        # Reading the database for a snapshot in the writer process would itself
        # drop POSIX locks. Keep the instrumentation in another process as well.
        code = """
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
for p in sorted(root.rglob('*')):
    s = p.stat()
    print(p.relative_to(root), s.st_mode, s.st_size, s.st_mtime_ns,
          hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else '')
"""
        return subprocess.run([sys.executable, "-c", code, str(path.parent)],
                              capture_output=True, text=True, check=True).stdout
    def no_connect(*args, **kwargs):
        pytest.fail("SQLite opened with a live journal")
    monkeypatch.setattr(read_only, "_connect", no_connect)
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA user_version=42")
        before = snapshot_in_child()
        for _ in range(2):
            with pytest.raises(CacheSchemaError, match="cache_busy"):
                ReadOnlyExactCache(path).lookup(item.key)
        assert snapshot_in_child() == before
    finally:
        connection.rollback()
        connection.close()


def test_abandoned_journal_is_not_busy(prepared, read_guard):
    path, _, item = prepared
    path.with_name(path.name + "-journal").write_bytes(b"abandoned")
    with read_guard() as sql:
        with pytest.raises(CacheSchemaError, match="cache_recovery_required"):
            ReadOnlyExactCache(path).lookup(item.key)
    assert not sql
