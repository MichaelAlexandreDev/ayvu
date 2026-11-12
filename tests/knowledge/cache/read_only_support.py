"""Real-cache fixtures and filesystem/SQL side-effect assertions."""
from contextlib import contextmanager
from dataclasses import replace
import re
import sqlite3

import pytest

from ayvu.knowledge.cache import exact_entry_key
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.repository import SQLiteExactCache
from tests.support.cache_repository_v2_support import entry
from tests.support.translation_cache_v2_support import make_plan


def snapshot(root):
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_size, info.st_mode, info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
    return result


@pytest.fixture
def read_guard(tmp_path, monkeypatch):
    @contextmanager
    def guard():
        before, statements, opened = snapshot(tmp_path), [], []
        original = sqlite3.connect
        def connect(database, *args, **kwargs):
            assert kwargs.get("uri") is True
            assert database.endswith("?mode=ro") and "immutable" not in database
            connection = original(database, *args, **kwargs)
            connection.set_trace_callback(statements.append)
            opened.append(connection)
            return connection
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            try:
                yield statements
            finally:
                assert snapshot(tmp_path) == before
                forbidden = r"\b(INSERT|UPDATE|DELETE|CREATE|REPLACE|VACUUM|ATTACH|DETACH)\b|PRAGMA\s+journal_mode\s*=|BEGIN\s+IMMEDIATE"
                assert not any(re.search(forbidden, sql, re.I) for sql in statements), statements
                for connection in opened:
                    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                        connection.execute("SELECT 1")
    return guard


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / "CANARY cache?#.v2.sqlite"
    writer = SQLiteExactCache(path)
    item = entry(source="CANARY-SOURCE", value="CANARY-VALUE")
    writer.put_if_absent(item)
    return path, writer, item


def reader_records(reader, item, *, limit=256):
    return reader.canonical_records(item.key.scope, item.key.value_kind, item.key.source_content_digest, limit=limit)


def variant(item, index):
    operation = make_plan(chunk_limit=3000 + index).semantic_operation
    key = exact_entry_key(item.key.scope, item.key.value_kind, operation.fingerprint, item.source_text)
    return replace(item, key=key, canonical_identity=operation.canonical_payload())


def change(path, sql, args=()):
    with sqlite3.connect(path) as connection:
        connection.execute(sql, args)


def assert_empty(reader, item):
    from ayvu.knowledge.cache import CacheLookupState, CacheMissReason
    result = reader.lookup(item.key)
    assert result.state is CacheLookupState.MISS
    assert result.reason is CacheMissReason.ENTRY_NOT_FOUND
    assert reader_records(reader, item) == ()
