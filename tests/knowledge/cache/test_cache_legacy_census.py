"""REF-18 step 5: the old cache is counted without being created, changed or migrated."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from ayvu.knowledge.cache import LEGACY_SCHEMA
from ayvu.knowledge.cache.legacy_census import legacy_cache_census


def v1_cache(path, rows=(("en", "pt", "Hello"), ("en", "pt", "World"), ("fr", "pt", "Bonjour"), ("en", "es", "Hi"))):
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.executemany(
        "INSERT INTO translations (source_lang, target_lang, original_text_hash, original_text, translated_text) "
        "VALUES (?, ?, ?, ?, ?)",
        [(source, target, hashlib.sha256(text.encode()).hexdigest(), text, "x") for source, target, text in rows])
    connection.commit()
    connection.close()
    return path


def state(directory):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}


def test_the_texts_of_the_language_pair_are_counted_and_nothing_changes(tmp_path):
    path = v1_cache(tmp_path / "cache.sqlite")
    before = state(tmp_path)
    census = legacy_cache_census(path, source="en", target="pt")
    assert census.readable and census.texts == 2 and census.path == path
    assert legacy_cache_census(path, target="pt").texts == 3  # A batch may infer each book's source.
    assert state(tmp_path) == before


@pytest.mark.parametrize("prepare", ["absent", "empty", "other pair", "no v1 table"])
def test_there_is_nothing_to_say_without_old_texts_for_the_languages(tmp_path, prepare):
    path = tmp_path / "cache.sqlite"
    if prepare == "empty":
        path.write_bytes(b"")
    elif prepare == "other pair":
        v1_cache(path, rows=(("en", "es", "Hi"),))
    elif prepare == "no v1 table":
        sqlite3.connect(path).execute("CREATE TABLE other (x)").connection.close()
    before = state(tmp_path)
    assert legacy_cache_census(path, source="en", target="pt") is None
    assert state(tmp_path) == before


@pytest.mark.parametrize("beside", ["-journal", "-wal", "-shm"])
def test_a_cache_in_use_or_interrupted_is_unreadable_and_not_opened(tmp_path, beside):
    path = v1_cache(tmp_path / "cache.sqlite")
    (tmp_path / f"cache.sqlite{beside}").write_bytes(b"left by another writer")
    before = state(tmp_path)
    census = legacy_cache_census(path, source="en", target="pt")
    assert census is not None and not census.readable
    assert state(tmp_path) == before


def test_a_wal_database_is_unreadable_rather_than_given_a_new_shared_memory_file(tmp_path):
    path = v1_cache(tmp_path / "cache.sqlite")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    before = state(tmp_path)
    assert not legacy_cache_census(path, source="en", target="pt").readable
    assert state(tmp_path) == before


@pytest.mark.parametrize("content", [b"not a database at all", b"SQLite format 3\x00" + b"\x00" * 84 + b"broken" * 200])
def test_a_file_that_is_not_a_readable_database_is_reported_and_left_alone(tmp_path, content):
    path = tmp_path / "cache.sqlite"
    path.write_bytes(content)
    before = state(tmp_path)
    assert not legacy_cache_census(path, source="en", target="pt").readable
    assert state(tmp_path) == before
