import hashlib
import json
import os
import sqlite3
import threading

import pytest

from ayvu.knowledge.cache import admin as cache_admin, admin_store as cache_admin_store
from tests.support.legacy_cache_file import LegacyCacheFile
from ayvu.knowledge.cache.admin import CacheRef, FileRef, CacheFilter, PreviewCleanRequest, ExecuteCleanRequest, ImportRequest
from ayvu.composition.builders.cache_admin import cache_admin_services

from tests.support.cache_admin_integration_support import _empty_sqlite_file, _export, _sqlite_with_another_table
from tests.support.cache_admin_integration_support import inspect, seed


@pytest.mark.parametrize("make", [_empty_sqlite_file, _sqlite_with_another_table], ids=["empty_file", "no_table"])
def test_a_cache_without_the_table_is_empty_and_is_left_alone(tmp_path, make):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    make(path)
    before = path.read_bytes()

    summary = inspect(services, path)
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True)))
    cleaned = services.clean.execute(ExecuteCleanRequest(preview.value))
    exported = _export(services, path, tmp_path / "out.json")

    assert summary.failure is None and summary.value.count == 0
    assert preview.value.count == 0 and cleaned.value == 0
    assert exported.value == 0 and json.loads((tmp_path / "out.json").read_text())["entries"] == []
    assert path.read_bytes() == before


def test_something_that_is_not_sqlite_is_still_reported_as_corrupt(tmp_path):
    path = tmp_path / "cache.sqlite"
    path.write_bytes(b"not sqlite at all" * 100)

    assert inspect(cache_admin_services(), path).failure.code == "corrupt_cache"


def test_a_failed_first_import_leaves_a_cache_that_still_opens(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    entry = {"source_lang": "en", "target_lang": "pt", "original_text": "Hi", "translated_text": "Oi",
             "original_text_hash": hashlib.sha256(b"Hi").hexdigest(), "created_at": "2024-01-01T00:00:00+00:00"}
    payload = tmp_path / "in.json"
    payload.write_text(json.dumps({"version": 1, "entries": [entry]}))
    source = CacheRef(str(path))

    class Stop:
        def checkpoint(self, point):
            return object() if point == "cache_before_commit" else None

    failed = services.import_cache.execute(ImportRequest(source, FileRef(str(payload)), False, cancellation=Stop()))

    assert failed.failure.code == "cancelled"
    summary = inspect(services, path)
    assert summary.failure is None and summary.value.count == 0


def test_a_lock_held_for_a_moment_is_waited_for(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True))).value
    holding, release = threading.Event(), threading.Event()

    def hold():
        connection = sqlite3.connect(path)
        connection.execute("BEGIN IMMEDIATE")
        holding.set()
        release.wait(5)
        connection.rollback()
        connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(5)
    threading.Timer(0.4, release.set).start()

    result = services.clean.execute(ExecuteCleanRequest(preview))
    holder.join()

    assert result.failure is None and result.value == 1


def test_cleaning_is_the_way_out_of_a_row_above_the_interchange_limits(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one", "two")
    monkeypatch.setattr(cache_admin_store, "MAX_TEXT_LENGTH", 1)

    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True)))
    cleaned = services.clean.execute(ExecuteCleanRequest(preview.value))

    assert preview.failure is None and cleaned.value == 2


def test_both_layers_agree_on_the_limits():
    assert cache_admin.MAX_FILE_BYTES == cache_admin_store.MAX_FILE_BYTES
    assert cache_admin.MAX_ENTRIES == cache_admin_store.MAX_ENTRIES
    assert cache_admin.MAX_TEXT_LENGTH == cache_admin_store.MAX_TEXT_LENGTH


def test_a_cache_of_a_real_library_round_trips(tmp_path):
    """Ten or so books. The first limits, 64 MiB and 100 thousand entries, refused it."""
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    count = 110_000
    with LegacyCacheFile(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO translations (source_lang, target_lang, original_text_hash, original_text, translated_text, created_at) "
            "VALUES ('en', 'pt', ?, ?, ?, '2024-01-01T00:00:00+00:00')",
            ((hashlib.sha256(str(index).encode()).hexdigest(), str(index), "PT") for index in range(count)))
    out = tmp_path / "out.json"

    exported = _export(services, path, out)
    imported = services.import_cache.execute(ImportRequest(CacheRef(str(tmp_path / "other.sqlite")), FileRef(str(out))))

    assert exported.value == count
    assert imported.value.imported == count


def test_an_export_follows_the_umask_like_any_other_output(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    out = tmp_path / "out.json"
    previous = os.umask(0o027)
    try:
        assert _export(services, path, out).failure is None
    finally:
        os.umask(previous)

    assert out.stat().st_mode & 0o777 == 0o640
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-")] == []


def test_replacing_an_export_keeps_what_the_old_file_allowed(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    out = tmp_path / "out.json"
    out.write_text("old")
    out.chmod(0o604)

    assert _export(services, path, out, overwrite=True).failure is None

    assert out.stat().st_mode & 0o777 == 0o604
    assert out.read_text() != "old"
