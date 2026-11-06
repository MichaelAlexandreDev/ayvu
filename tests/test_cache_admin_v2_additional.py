import errno
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ayvu.cache import SCHEMA
from ayvu.cache_admin import (
    CacheFilter, CacheInterchangeLimits, CacheNamespace, CacheRef, CacheScopeMapping, ExecuteCleanRequest,
    ExecuteImportRequest, ExportRequest, FileRef, ImportRequest, ImportScopeMap,
    PreviewCleanRequest,
)
from ayvu.cache_admin_composition import cache_admin_services
from ayvu.cache_admin_store import CacheStorageError, LocalCacheFiles, SQLiteCacheStore
from ayvu.cache_admin_v2_store import SQLiteCacheV2Store
from ayvu.cache_repository import SQLiteExactCache
from test_cache_repository_v2 import entry


def test_explicit_legacy_export_never_uses_whole_payload_ports(tmp_path, monkeypatch):
    cache, output = tmp_path / 'cache.sqlite', tmp_path / 'export.json'
    SQLiteExactCache(cache).put_if_absent(entry())
    def forbidden(*args, **kwargs):
        raise AssertionError('whole-payload port used')
    monkeypatch.setattr(SQLiteCacheStore, 'export_payload', forbidden)
    monkeypatch.setattr(LocalCacheFiles, 'publish', forbidden)
    result = cache_admin_services().export.execute(ExportRequest(
        CacheRef(str(cache)), FileRef(str(output)), namespace=CacheNamespace.legacy()))
    assert result.failure is None
    assert result.format_version == 1
    assert json.loads(output.read_text())['version'] == 1


@pytest.mark.parametrize('selection', ['implicit', 'legacy', 'all', 'v2'])
def test_mismatched_export_format_preserves_existing_destination(tmp_path, selection):
    path, output = tmp_path / 'cache.sqlite', tmp_path / 'export.json'
    item = entry()
    if selection == 'implicit':
        with sqlite3.connect(path) as conn:
            conn.execute(SCHEMA)
        namespace, version = None, 2
    else:
        SQLiteExactCache(path).put_if_absent(item)
        namespace = {'legacy': CacheNamespace.legacy(), 'all': CacheNamespace.all(),
                     'v2': CacheNamespace.v2(item.key.scope)}[selection]
        version = 2 if selection == 'legacy' else 1
    output.write_bytes(b'existing export')
    result = cache_admin_services().export.execute(ExportRequest(
        CacheRef(str(path)), FileRef(str(output)), overwrite=True,
        namespace=namespace, format_version=version))
    assert result.failure.code == 'unsupported_version'
    assert output.read_bytes() == b'existing export'
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


class PauseBeforeMutation:
    def __init__(self):
        self.reached, self.resume = Event(), Event()

    def checkpoint(self, point):
        if point == 'cache_before_mutation' and not self.reached.is_set():
            self.reached.set()
            assert self.resume.wait(10)


@pytest.mark.parametrize('operation', ['clean', 'import'])
def test_runtime_put_between_preview_and_transaction_is_not_lost(tmp_path, operation):
    path, source = tmp_path / 'cache.sqlite', tmp_path / 'source.json'
    runtime = SQLiteExactCache(path)
    original, concurrent = entry(), entry(source='concurrent runtime entry')
    runtime.put_if_absent(original)
    store = SQLiteCacheV2Store()
    namespace = CacheNamespace.v2(original.key.scope)
    if operation == 'clean':
        preview = store.preview_clean(PreviewCleanRequest(
            CacheRef(str(path)), CacheFilter(all_entries=True), namespace=namespace))
    else:
        store.export_interchange(ExportRequest(CacheRef(str(path)), FileRef(str(source)), namespace=namespace))
        mapping = ImportScopeMap((CacheScopeMapping(namespace, namespace),))
        preview = store.preview_import(ImportRequest(CacheRef(str(path)), FileRef(str(source)), scope_map=mapping))
    pause = PauseBeforeMutation()
    def execute():
        if operation == 'clean':
            return store.execute_clean(ExecuteCleanRequest(preview, cancellation=pause))
        return store.execute_import(ExecuteImportRequest(preview, cancellation=pause))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        try:
            assert pause.reached.wait(10)
            runtime.put_if_absent(concurrent)
        finally:
            pause.resume.set()
        with pytest.raises(CacheStorageError, match='stale_preview'):
            future.result(timeout=10)
    assert runtime.lookup(original.key).value == original.value
    assert runtime.lookup(concurrent.key).value == concurrent.value


def _import_fixture(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'source.json'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    namespace = CacheNamespace.v2(item.key.scope)
    store = SQLiteCacheV2Store()
    store.export_interchange(ExportRequest(CacheRef(str(cache)), FileRef(str(source)), namespace=namespace))
    mapping = ImportScopeMap((CacheScopeMapping(namespace, namespace),))
    return cache, source, namespace, mapping


def test_staging_sqlite_limit_produces_real_disk_full_and_cleans_up(tmp_path, monkeypatch):
    cache, source, _, mapping = _import_fixture(tmp_path)
    before = cache.read_bytes()
    limits = CacheInterchangeLimits(raw_input=65536)
    real_connect = sqlite3.connect
    observed = []

    class StageConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql.startswith('CREATE TABLE rows '):
                page_size = super().execute('PRAGMA page_size').fetchone()[0]
                maximum = super().execute('PRAGMA max_page_count').fetchone()[0]
                observed.append((page_size, maximum))
                try:
                    super().execute('INSERT INTO rows VALUES (?, ?, zeroblob(?))',
                                    ('v2', 'overflow', maximum * page_size + page_size))
                except sqlite3.OperationalError as exc:
                    assert exc.sqlite_errorcode == sqlite3.SQLITE_FULL
                    raise
                pytest.fail('SQLite accepted data beyond its staging page limit')
            return result

    def connect(database, *args, **kwargs):
        if '.ayvu-cache-import-' in str(database):
            kwargs['factory'] = StageConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr('ayvu.cache_admin_v2_store.sqlite3.connect', connect)
    request = ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping, limits=limits)
    result = cache_admin_services().preview_import.execute(request)
    assert result.failure.code == 'disk_full'
    assert len(observed) == 1
    page_size, maximum = observed[0]
    assert maximum == (limits.raw_input * 4 + 1024 * 1024) // page_size
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('.ayvu-cache-import-*'))


@pytest.mark.parametrize('operation', ['import', 'export'])
def test_permission_errors_are_sanitized_and_preserve_files(tmp_path, monkeypatch, caplog, operation):
    cache, source, namespace, mapping = _import_fixture(tmp_path)
    target = tmp_path / 'target.json'
    target.write_bytes(b'existing target')
    before_cache, before_source = cache.read_bytes(), source.read_bytes()
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'SECRET-PRIVATE-PATH')
    monkeypatch.setattr('ayvu.cache_admin_v2_store.tempfile.mkstemp', denied)
    services = cache_admin_services()
    if operation == 'import':
        result = services.preview_import.execute(ImportRequest(
            CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping))
    else:
        result = services.export.execute(ExportRequest(
            CacheRef(str(cache)), FileRef(str(target)), overwrite=True, namespace=namespace))
    assert result.failure.code == 'permission_denied'
    assert 'SECRET-PRIVATE-PATH' not in repr(result) + repr(result.failure) + caplog.text
    assert cache.read_bytes() == before_cache
    assert source.read_bytes() == before_source
    assert target.read_bytes() == b'existing target'
    assert not list(tmp_path.glob('.ayvu-cache-import-*'))
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))
