import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ayvu.cache_admin import (CacheRef, FileRef, CacheFilter, CacheNamespace, CacheScopeMapping,
    ImportScopeMap, ImportRequest, ExecuteImportRequest, PreviewCleanRequest, ExecuteCleanRequest,
    ExportRequest)
from ayvu.cache_admin_store import CacheStorageError
from ayvu.cache_admin_v2_store import SQLiteCacheV2Store
from ayvu.cache_repository import SQLiteExactCache
from ayvu.cache_schema import prepare_cache, participating_connection
from test_cache_repository_v2 import entry


def _capture(call):
    try:
        return ("ok", call())
    except CacheStorageError as exc:
        return ("error", str(exc))


def _legacy_payload(path, translation):
    row = dict(source_lang="en", target_lang="pt", original_text_hash=hashlib.sha256(b"hello").hexdigest(),
               original_text="hello", translated_text=translation, created_at="2026-01-01")
    path.write_text(json.dumps({"version": 2, "namespaces": [{"kind": "legacy-v1", "entries": [row]}]}))


def _preview(store, cache, source):
    ns = CacheNamespace.legacy()
    mapping = ImportScopeMap((CacheScopeMapping(ns, ns),))
    return store.preview_import(ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping))


def test_runtime_put_after_clean_preview_is_stale(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteExactCache(path)
    first = entry()
    cache.put_if_absent(first)
    store = SQLiteCacheV2Store()
    selection = store.preview_clean(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True), namespace=CacheNamespace.v2(first.key.scope)))
    cache.put_if_absent(entry(source="new"))
    with pytest.raises(CacheStorageError, match="stale_preview"):
        store.execute_clean(ExecuteCleanRequest(selection))
    assert cache.lookup(first.key).value == first.value


def test_two_clean_executions_have_one_winner(tmp_path):
    path = tmp_path / "cache.sqlite"
    first = entry()
    SQLiteExactCache(path).put_if_absent(first)
    store = SQLiteCacheV2Store()
    selection = store.preview_clean(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True), namespace=CacheNamespace.v2(first.key.scope)))
    barrier = Barrier(2)
    def execute():
        barrier.wait()
        return _capture(lambda: store.execute_clean(ExecuteCleanRequest(selection)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert sorted(results) == [("error", "stale_preview"), ("ok", 1)]


def test_two_divergent_imports_have_one_winner(tmp_path):
    cache, a, b = (tmp_path / name for name in ("cache.sqlite", "a.json", "b.json"))
    prepare_cache(cache)
    _legacy_payload(a, "one")
    _legacy_payload(b, "two")
    store = SQLiteCacheV2Store()
    previews = [_preview(store, cache, source) for source in (a, b)]
    barrier = Barrier(2)
    def execute(preview):
        barrier.wait()
        return _capture(lambda: store.execute_import(ExecuteImportRequest(preview)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, previews))
    assert [status for status, _ in results].count("ok") == 1
    assert [value for status, value in results if status == "error"] == ["stale_preview"]
    with participating_connection(cache) as conn:
        assert conn.execute("SELECT count(*) FROM translations").fetchone()[0] == 1


class ExchangeAt:
    def __init__(self, point, path):
        self.point, self.path, self.done = point, path, False
    def checkpoint(self, point):
        if point == self.point and not self.done:
            self.done = True
            replacement = self.path.with_name(self.path.name + ".replacement")
            replacement.write_bytes(self.path.read_bytes())
            os.replace(replacement, self.path)


@pytest.mark.parametrize("target", ["source", "stage", "cache"])
def test_import_identity_exchange_before_commit_rolls_back(tmp_path, target):
    cache, source = tmp_path / "cache.sqlite", tmp_path / "input.json"
    prepare_cache(cache)
    _legacy_payload(source, "one")
    store = SQLiteCacheV2Store()
    preview = _preview(store, cache, source)
    path = {"source": source, "stage": __import__("pathlib").Path(preview.stage.value), "cache": cache}[target]
    with pytest.raises(CacheStorageError):
        store.execute_import(ExecuteImportRequest(preview, cancellation=ExchangeAt("cache_before_commit", path)))
    with participating_connection(cache) as conn:
        assert conn.execute("SELECT count(*) FROM translations").fetchone()[0] == 0


def test_export_destination_exchange_before_publish_is_preserved(tmp_path):
    cache, output = tmp_path / "cache.sqlite", tmp_path / "output.json"
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    output.write_text("existing")
    request = ExportRequest(CacheRef(str(cache)), FileRef(str(output)), overwrite=True,
                            namespace=CacheNamespace.v2(item.key.scope),
                            cancellation=ExchangeAt("cache_before_publish", output))
    with pytest.raises(CacheStorageError):
        SQLiteCacheV2Store().export_interchange(request)
    assert output.read_text() == "existing"
