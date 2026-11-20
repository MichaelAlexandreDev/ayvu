"""Integration tests for SQLite and atomic cache administration."""
import errno
import json
import sqlite3

import pytest

from ayvu.knowledge.cache import admin_store as cache_admin_store
from tests.support.legacy_cache_file import CacheKey, LegacyCacheFile
from ayvu.knowledge.cache.admin import CacheRef, FileRef, CacheFilter, PreviewCleanRequest, ExecuteCleanRequest, ExportRequest, ImportRequest
from ayvu.composition.builders.cache_admin import cache_admin_services
from ayvu.kernel import LanguagePair

from tests.support.cache_admin_integration_support import CancelAt
from tests.support.cache_admin_integration_support import inspect, key, seed


def test_absent_inspection_and_export_do_not_create_cache(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "absent" / "cache.sqlite"
    result = inspect(services, path)
    assert result.value.exists is False and result.value.count == 0
    target = tmp_path / "export.json"
    result = services.export.execute(ExportRequest(CacheRef(str(path)), FileRef(str(target))))
    assert result.value == 0
    assert json.loads(target.read_text()) == {"version": 1, "entries": []}
    assert not path.parent.exists()


def test_clean_preview_stale_and_success(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    ref = CacheRef(str(path))
    preview = services.preview.execute(PreviewCleanRequest(ref, CacheFilter(source_lang="en"))).value
    assert preview.count == 1 and inspect(services, path).value.count == 1
    seed(path, "two")
    stale = services.clean.execute(ExecuteCleanRequest(preview))
    assert stale.failure.code == "stale_preview"
    assert inspect(services, path).value.count == 2
    preview = services.preview.execute(PreviewCleanRequest(ref, CacheFilter(all_entries=True))).value
    assert services.clean.execute(ExecuteCleanRequest(preview)).value == 2
    assert inspect(services, path).value.count == 0


def test_clean_cancel_before_commit_rolls_back(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one", "two")
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True))).value
    cancel = CancelAt("cache_before_commit")
    result = services.clean.execute(ExecuteCleanRequest(preview, cancellation=cancel))
    assert result.failure.code == "cancelled" and result.value is None
    assert cancel.seen[-1] == "cache_before_commit"
    assert inspect(services, path).value.count == 2


def test_export_atomic_replacement_and_failure(tmp_path, monkeypatch):
    services = cache_admin_services()
    path, target = tmp_path / "cache.sqlite", tmp_path / "export.json"
    seed(path, "one")
    target.write_bytes(b"previous")
    request = ExportRequest(CacheRef(str(path)), FileRef(str(target)), overwrite=True)
    import ayvu.knowledge.cache.admin_store as store
    def fail(*args):
        raise OSError(errno.ENOSPC, "sensitive environment")
    with monkeypatch.context() as patch:
        patch.setattr(store.os, "replace", fail)
        result = services.export.execute(request)
    assert result.failure.code == "disk_full"
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".ayvu-cache-*"))
    cancel = CancelAt("cache_before_publish")
    result = services.export.execute(ExportRequest(request.cache, request.destination, overwrite=True, cancellation=cancel))
    assert result.failure.code == "cancelled" and cancel.seen[-1] == "cache_before_publish"
    assert target.read_bytes() == b"previous"
    assert services.export.execute(request).value == 1
    assert json.loads(target.read_text())["entries"][0]["original_text"] == "one"


def test_replacement_failure_has_distinct_code(tmp_path, monkeypatch):
    services = cache_admin_services()
    path, target = tmp_path / "cache.sqlite", tmp_path / "export.json"
    seed(path, "one")
    target.write_bytes(b"previous")
    import ayvu.knowledge.cache.admin_store as store
    def fail(*args):
        raise OSError(errno.EIO, "private")
    monkeypatch.setattr(store.os, "replace", fail)
    result = services.export.execute(ExportRequest(CacheRef(str(path)), FileRef(str(target)), overwrite=True))
    assert result.failure.code == "publication_failed"
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".ayvu-cache-*"))


def test_import_conflicts_and_cancel_before_commit(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one", "two")
    seed(dest, "one")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 2
    request = ImportRequest(CacheRef(str(dest)), FileRef(str(output)))
    cancel = CancelAt("cache_before_commit")
    result = services.import_cache.execute(ImportRequest(request.cache, request.source, cancellation=cancel))
    assert result.failure.code == "cancelled" and cancel.seen[-1] == "cache_before_commit"
    assert inspect(services, dest).value.count == 1
    counts = services.import_cache.execute(request).value
    assert (counts.imported, counts.skipped, counts.replaced) == (1, 1, 0)
    counts = services.import_cache.execute(ImportRequest(request.cache, request.source, replace=True)).value
    assert (counts.imported, counts.skipped, counts.replaced) == (0, 0, 2)


def test_import_progress_failure_rolls_back_prior_insert(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one", "two")
    seed(dest, "existing")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 2
    class Sink:
        def __init__(self): self.seen = []
        def report(self, progress):
            self.seen.append(progress.completed)
            if progress.completed == 1: raise RuntimeError("private")
    sink = Sink()
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(output)), progress=sink))
    assert sink.seen == [0, 1]
    assert result.failure.code == "progress_sink_failed"
    assert inspect(services, dest).value.count == 1


def test_import_cancellation_during_payload_validation_precedes_mutation(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 1
    cancel = CancelAt("cache_between_entries")
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(output)), cancellation=cancel))
    assert result.failure.code == "cancelled"
    assert cancel.seen == ["cache_before_read", "cache_between_entries"]
    assert not dest.exists()


def test_busy_lock_is_distinct_and_keeps_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_admin_store, "BUSY_TIMEOUT_SECONDS", 0.1)
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True))).value
    with sqlite3.connect(path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        result = services.clean.execute(ExecuteCleanRequest(preview))
        assert result.failure.code == "cache_busy"
    assert inspect(services, path).value.count == 1


def test_permission_failure_is_sanitized(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    import ayvu.knowledge.cache.admin_store as store
    def fail(*args, **kwargs):
        raise PermissionError("secret path")
    monkeypatch.setattr(store.tempfile, "mkstemp", fail)
    result = services.export.execute(ExportRequest(CacheRef(str(path)), FileRef(str(tmp_path / "out"))))
    assert result.failure.code == "permission_denied"
    assert "secret" not in repr(result)


def test_import_sql_failure_rolls_back_prior_insert(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one", "two")
    seed(dest, "existing")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 2
    with sqlite3.connect(dest) as conn:
        conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON translations WHEN NEW.original_text = 'two' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(output))))
    assert result.failure is not None and result.value is None
    with LegacyCacheFile(dest) as cache:
        assert cache.get(key("one")) is None
        assert cache.get(key("two")) is None
        assert cache.get(key("existing")) == "translated existing"


@pytest.mark.parametrize("data,expected", [(b"{", "malformed_json"),
    (b'{"version":2,"entries":[]}', "unsupported_version"),
    (b'{"version":1,"entries":[{}]}', "malformed_entry")])
def test_bad_import_never_creates_cache(tmp_path, data, expected):
    services = cache_admin_services()
    source, dest = tmp_path / "bad.json", tmp_path / "absent" / "cache.sqlite"
    source.write_bytes(data)
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(source))))
    assert result.failure.code == expected
    assert not dest.parent.exists()


def test_bounded_file_read_rejects_oversized_input(tmp_path, monkeypatch):
    import ayvu.knowledge.cache.admin_store as store
    services = cache_admin_services()
    source, dest = tmp_path / "large.json", tmp_path / "cache.sqlite"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 5)
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(source))))
    assert result.failure.code == "limit_exceeded"
    assert not dest.exists()


@pytest.mark.parametrize("filters,expected", [
    (CacheFilter(all_entries=True), 3),
    (CacheFilter(source_lang="en"), 2),
    (CacheFilter(target_lang="pt"), 2),
    (CacheFilter(before="2024-02-01"), 1),
    (CacheFilter(source_lang="en", target_lang="pt", before="2024-02-01"), 1),
])
def test_clean_filters_select_exact_rows(tmp_path, filters, expected):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    with LegacyCacheFile(path) as cache:
        cache.set(key("one"), "um")
        cache.set(key("two"), "dois")
        cache.set(CacheKey(text="three", language_pair=LanguagePair(source="es", target="es")), "tres")
        cache.connection.execute("UPDATE translations SET created_at = CASE WHEN original_text = 'one' THEN '2024-01-01' ELSE '2024-03-01' END")
        cache.connection.commit()
    ref = CacheRef(str(path))
    preview = services.preview.execute(PreviewCleanRequest(ref, filters)).value
    assert preview.count == expected
    assert services.clean.execute(ExecuteCleanRequest(preview)).value == expected
    assert inspect(services, path).value.count == 3 - expected
