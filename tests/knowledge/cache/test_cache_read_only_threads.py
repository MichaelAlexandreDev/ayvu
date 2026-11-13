"""A descriptor validation must never release a sibling reader's POSIX lock."""
import sqlite3
import subprocess
import sys
from threading import Event, Thread

import pytest

from ayvu.knowledge.cache import read_only, read_only_journal
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.schema import CacheSchemaError
from tests.knowledge.cache.read_only_support import prepared


_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0)
for command in sys.stdin:
    if command.strip() == 'quit':
        break
    try:
        conn.execute('BEGIN EXCLUSIVE')
        conn.execute('PRAGMA user_version=42')
        conn.rollback()
        print('writable', flush=True)
    except sqlite3.OperationalError:
        conn.rollback()
        print('locked', flush=True)
conn.close()
"""


def test_two_thread_readers_preserve_shared_lock_against_external_writer(prepared, monkeypatch):
    path, _, item = prepared
    locked, release, finished = Event(), Event(), Event()
    connections, errors = [], []
    original = read_only.exact_record
    def record(connection, key):
        connections.append(connection)
        if len(connections) == 1:
            # _state has already acquired a real SQLite SHARED lock in BEGIN.
            locked.set()
            assert release.wait(10)
        return original(connection, key)
    monkeypatch.setattr(read_only, "exact_record", record)
    def read(done=None):
        try:
            assert ReadOnlyExactCache(path).lookup(item.key).value == item.value
        except BaseException as error:
            errors.append(error)
        finally:
            if done is not None:
                done.set()
    writer = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", _WRITER, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def attempt():
        writer.stdin.write("attempt\n")
        writer.stdin.flush()
        return writer.stdout.readline().strip()
    first, second = Thread(target=read), Thread(target=read, args=(finished,))
    first.start()
    try:
        assert locked.wait(5)
        assert attempt() == "locked"
        second.start()
        assert finished.wait(5)
        # The second reader has performed all inode/header checks AND closed its
        # SQLite connection. The first reader must still exclude this process.
        assert attempt() == "locked"
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        writer.stdin.write("quit\n")
        writer.stdin.flush()
        writer.communicate(timeout=5)
    assert not errors and not first.is_alive() and not second.is_alive()
    assert len(connections) == 2 and connections[0] is not connections[1]
    with sqlite3.connect(path, timeout=0) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()


def test_healthy_header_probe_runs_once_per_file_state_and_again_after_a_change(prepared, monkeypatch):
    path, _, item = prepared
    monkeypatch.setattr(read_only_journal, "_HEALTHY_HEADERS", set())
    probes = []
    original = read_only_journal.subprocess.run
    def run(*args, **kwargs):
        probes.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(read_only_journal.subprocess, "run", run)
    reader = ReadOnlyExactCache(path)
    for _ in range(3):
        assert reader.lookup(item.key).value == item.value
    assert len(probes) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.close()
    with pytest.raises(CacheSchemaError, match="cache_recovery_required"):
        reader.lookup(item.key)
    assert len(probes) == 2
