"""Read-only v2 exact lookup and bounded canonical evidence contracts."""
from dataclasses import replace
import sqlite3
from uuid import UUID

import pytest

from ayvu.knowledge.cache import (
    CACHE_ENTRY_V2_LIMITS as LIMITS, MAX_CACHE_ONLY_CANDIDATES, CacheAuthorizationPolicy,
    CacheIdentityError, CacheValueKind, CanonicalCacheRecord,
)
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.schema import LEGACY_SCHEMA
from tests.support.cache_repository_v2_support import entry
from .read_only_support import (
    assert_empty, change, prepared, read_guard, reader_records, variant,
)


def test_absent_parent_creates_nothing(tmp_path, read_guard):
    with read_guard() as sql:
        assert_empty(ReadOnlyExactCache(tmp_path / "missing" / "cache.v2.sqlite"), entry())
    assert not sql and not list(tmp_path.iterdir())


def test_v1_without_v2_sibling_is_never_consulted(tmp_path, read_guard):
    path = tmp_path / "cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
    with read_guard() as sql:
        assert_empty(ReadOnlyExactCache(path.with_name(path.name + ".v2.sqlite")), entry())
    assert not sql


def test_hit_matches_writer_and_returns_validated_evidence(prepared, read_guard):
    path, writer, item = prepared
    expected = writer.lookup(item.key)
    with read_guard() as sql:
        reader = ReadOnlyExactCache(path)
        assert reader.lookup(item.key) == expected
        records = reader_records(reader, item)
        assert len(records) == 1 and type(records[0]) is CanonicalCacheRecord
        record = records[0]
        assert (record.key, record.source_text, record.value, record.canonical_identity, record.metadata) == (
            item.key, item.source_text, item.value, item.canonical_identity, item.metadata)
        assert "CANARY" not in repr((reader, records))
    assert sql.count("BEGIN") == sql.count("ROLLBACK") == 2


def test_miss_matches_writer(prepared, read_guard):
    path, writer, _ = prepared
    item = entry(source="missing")
    expected = writer.lookup(item.key)
    with read_guard():
        reader = ReadOnlyExactCache(path)
        assert reader.lookup(item.key) == expected
        assert_empty(reader, item)


@pytest.mark.parametrize("lock_exists", [False, True])
def test_readonly_directory_and_lock_need_no_write_access(prepared, read_guard, lock_exists):
    path, _, item = prepared
    lock = path.with_name(path.name + ".lock")
    if not lock_exists:
        lock.unlink()
    else:
        lock.chmod(0o400)
    path.chmod(0o400)
    path.parent.chmod(0o500)
    try:
        with read_guard():
            assert ReadOnlyExactCache(path).lookup(item.key).value == item.value
    finally:
        path.parent.chmod(0o700)


@pytest.mark.parametrize("column,data", [
    ("value", "tampered"), ("source_text", "tampered"), ("canonical_identity", b"{}"),
    ("metadata", b'{"secret":1}'), ("provenance", b"{}"), ("created_at", "invalid"),
    ("provenance_digest", "broken"), ("entry_digest", "broken"),
])
def test_corrupt_payload_or_checksum_matches_writer(prepared, read_guard, column, data):
    path, writer, item = prepared
    change(path, f"UPDATE cache_entries_v2 SET {column}=?", (data,))
    with pytest.raises(CacheIdentityError) as expected:
        writer.lookup(item.key)
    with read_guard():
        reader = ReadOnlyExactCache(path)
        for operation in (lambda: reader.lookup(item.key), lambda: reader_records(reader, item)):
            with pytest.raises(CacheIdentityError) as error:
                operation()
            assert error.value.code == expected.value.code == "cache_entry_corrupt"
            assert "CANARY" not in str(error.value) + repr(error.value)


@pytest.mark.parametrize("column,bound", [
    ("source_text", LIMITS.source_text), ("value", LIMITS.value),
    ("canonical_identity", LIMITS.canonical_identity), ("metadata", LIMITS.metadata),
    ("provenance", LIMITS.provenance),
])
def test_oversized_rows_fail_before_payload_materialization(prepared, read_guard, column, bound):
    path, _, item = prepared
    change(path, f"UPDATE cache_entries_v2 SET {column}=zeroblob(?)", (bound + 1,))
    with read_guard() as sql:
        reader = ReadOnlyExactCache(path)
        for operation in (lambda: reader.lookup(item.key), lambda: reader_records(reader, item)):
            with pytest.raises(CacheIdentityError, match="cache_entry_corrupt"):
                operation()
    assert not any(statement.startswith("SELECT source_text,value,") for statement in sql)


@pytest.mark.parametrize("count,limit", [(2, 3), (3, 3), (3, 2),
                                         (MAX_CACHE_ONLY_CANDIDATES, MAX_CACHE_ONLY_CANDIDATES),
                                         (MAX_CACHE_ONLY_CANDIDATES + 1, MAX_CACHE_ONLY_CANDIDATES)])
def test_candidate_limits_are_explicit_and_overflow_is_an_error(prepared, read_guard, count, limit):
    path, writer, item = prepared
    for index in range(1, count):
        writer.put_if_absent(variant(item, index))
    with read_guard() as sql:
        reader = ReadOnlyExactCache(path)
        if count > limit:
            with pytest.raises(CacheIdentityError, match="cache_candidate_limit_exceeded"):
                reader_records(reader, item, limit=limit)
        else:
            records = reader_records(reader, item, limit=limit)
            assert len(records) == count
            assert len({record.key for record in records}) == count
    if count > limit:
        assert not any(statement.startswith("SELECT source_text,value,") for statement in sql)


def test_foreign_scope_and_source_are_not_accessible(prepared, read_guard):
    path, _, item = prepared
    foreign = CacheAuthorizationPolicy(str(UUID(int=0x9999)))
    with read_guard():
        reader = ReadOnlyExactCache(path)
        with pytest.raises(CacheIdentityError, match="cache_scope_denied"):
            foreign.resolve(item.key.scope.scope)
        assert_empty(reader, replace(item, key=replace(item.key, scope=foreign.resolve())))
        assert_empty(reader, entry(source="different source"))
        with pytest.raises(CacheIdentityError, match="invalid_identity_resolution"):
            reader.canonical_records(item.key.scope.scope, item.key.value_kind,
                                     item.key.source_content_digest, limit=1)


@pytest.mark.parametrize("limit", [0, -1, True, MAX_CACHE_ONLY_CANDIDATES + 1])
def test_invalid_limit_never_opens_sqlite(prepared, read_guard, limit):
    path, _, item = prepared
    with read_guard() as sql:
        with pytest.raises(CacheIdentityError, match="invalid_identity_resolution"):
            reader_records(ReadOnlyExactCache(path), item, limit=limit)
    assert not sql


def test_invalid_key_kind_and_digest_never_open_sqlite(prepared, read_guard):
    path, _, item = prepared
    with read_guard() as sql:
        reader = ReadOnlyExactCache(path)
        with pytest.raises(CacheIdentityError, match="invalid_exact_entry_key"):
            reader.lookup(object())
        for kind, digest in ((CacheValueKind("other", 1), item.key.source_content_digest),
                             (item.key.value_kind, "invalid")):
            with pytest.raises(CacheIdentityError, match="invalid_identity_resolution"):
                reader.canonical_records(item.key.scope, kind, digest, limit=1)
    assert not sql
