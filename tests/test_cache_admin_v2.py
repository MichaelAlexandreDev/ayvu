import hashlib
import json

import pytest

from ayvu.cache_admin import (CacheRef, FileRef, CacheFilter, CacheNamespace, CacheScopeMapping,
                              ImportScopeMap, ImportRequest, ExecuteImportRequest,
                              InspectRequest, PreviewCleanRequest, ExecuteCleanRequest)
from ayvu.cache_admin_store import CacheStorageError
from ayvu.cache_admin_v2_store import SQLiteCacheV2Store
from ayvu.cache_schema import prepare_cache, participating_connection


def test_import_preview_execution_and_stale_revision(tmp_path):
    cache = tmp_path / 'cache.sqlite'
    prepare_cache(cache)
    source = tmp_path / 'input.json'
    entry = dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(b'hello').hexdigest(),
                 original_text='hello', translated_text='ola', created_at='2026-01-01')
    source.write_text(json.dumps({'version': 2, 'namespaces': [{'kind': 'legacy-v1', 'entries': [entry]}]}))
    namespace = CacheNamespace.legacy()
    scope_map = ImportScopeMap((CacheScopeMapping(namespace, namespace),))
    store = SQLiteCacheV2Store()
    preview = store.preview_import(ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=scope_map))
    assert (preview.count, preview.imported, preview.skipped) == (1, 1, 0)
    with participating_connection(cache) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', ('fr', 'pt', 'x', 'x', 'x', '2026-01-01'))
        conn.commit()
    with pytest.raises(CacheStorageError, match='stale_preview'):
        store.execute_import(ExecuteImportRequest(preview))
    preview = store.preview_import(ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=scope_map))
    assert store.execute_import(ExecuteImportRequest(preview)).imported == 1
    summary = store.inspect(InspectRequest(CacheRef(str(cache)), namespace=namespace))
    assert summary.count == 2
    clean = store.preview_clean(PreviewCleanRequest(CacheRef(str(cache)), CacheFilter(all_entries=True), namespace=namespace))
    assert store.execute_clean(ExecuteCleanRequest(clean)) == 2


def test_v2_export_remap_and_conflict(tmp_path):
    from ayvu.cache_admin import ExportRequest
    from ayvu.cache_identity import CacheAuthorizationPolicy
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    source_cache, target_cache, payload = (tmp_path / n for n in ('source.sqlite', 'target.sqlite', 'data.json'))
    item = entry()
    SQLiteExactCache(source_cache).put_if_absent(item)
    prepare_cache(target_cache)
    source_ns = CacheNamespace.v2(item.key.scope)
    target_ns = CacheNamespace.v2(CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004322').resolve())
    store = SQLiteCacheV2Store()
    assert store.export_interchange(ExportRequest(CacheRef(str(source_cache)), FileRef(str(payload)), namespace=source_ns)).count == 1
    mapping = ImportScopeMap((CacheScopeMapping(source_ns, target_ns),))
    request = ImportRequest(CacheRef(str(target_cache)), FileRef(str(payload)), scope_map=mapping)
    preview = store.preview_import(request)
    assert store.execute_import(ExecuteImportRequest(preview)).imported == 1
    from dataclasses import replace
    target_key = replace(item.key, scope=target_ns.scope)
    assert SQLiteExactCache(target_cache).lookup(target_key).value == item.value
    second = store.preview_import(request)
    assert (second.skipped, second.conflicts) == (1, 0)
    assert store.execute_import(ExecuteImportRequest(second)).skipped == 1
    assert store.inspect(InspectRequest(CacheRef(str(target_cache)), namespace=target_ns)).count == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_direct_adapter_export_satisfies_application_port(tmp_path, legacy):
    from ayvu.cache_admin import ExportCache, ExportRequest
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    cache, output = tmp_path / 'cache.sqlite', tmp_path / 'data.json'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    namespace = CacheNamespace.legacy() if legacy else CacheNamespace.v2(item.key.scope)
    result = ExportCache(SQLiteCacheV2Store(), None).execute(
        ExportRequest(CacheRef(str(cache)), FileRef(str(output)), namespace=namespace))
    assert result.failure is None
    assert result.value == (0 if legacy else 1)
    assert result.format_version == (1 if legacy else 2)
    assert json.loads(output.read_text())['version'] == result.format_version


def test_empty_v2_export_and_clean_do_not_create_cache(tmp_path):
    from ayvu.cache_admin import ExportRequest
    from ayvu.cache_identity import CacheAuthorizationPolicy
    cache, output = tmp_path / 'missing.sqlite', tmp_path / 'out.json'
    ns = CacheNamespace.v2(CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004321').resolve())
    store = SQLiteCacheV2Store()
    assert not store.inspect(InspectRequest(CacheRef(str(cache)), namespace=ns)).exists
    clean = store.preview_clean(PreviewCleanRequest(CacheRef(str(cache)), CacheFilter(all_entries=True), namespace=ns))
    assert store.execute_clean(ExecuteCleanRequest(clean)) == 0
    assert store.export_interchange(ExportRequest(CacheRef(str(cache)), FileRef(str(output)), namespace=ns)).count == 0
    assert json.loads(output.read_text())['namespaces'] == [{'kind': 'v2', 'scope_kind': 'project', 'scope_id': '00000000-0000-0000-0000-000000004321', 'entries': []}]
    assert not cache.exists()


def test_clean_preview_detects_concurrent_insert(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    cache = tmp_path / 'cache.sqlite'
    prepare_cache(cache)
    ns = CacheNamespace.legacy()
    store = SQLiteCacheV2Store()
    selection = store.preview_clean(PreviewCleanRequest(CacheRef(str(cache)), CacheFilter(all_entries=True), namespace=ns))
    ready, done = Event(), Event()
    def writer():
        ready.wait(timeout=5)
        with participating_connection(cache) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', ('en', 'pt', 'x', 'x', 'x', '2026'))
            conn.commit()
        done.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        ready.set()
        assert done.wait(timeout=5)
        with pytest.raises(CacheStorageError, match='stale_preview'):
            store.execute_clean(ExecuteCleanRequest(selection))
        future.result()
    assert store.inspect(InspectRequest(CacheRef(str(cache)), namespace=ns)).count == 1


def test_changed_input_and_cancellation_roll_back(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'data.json'
    prepare_cache(cache)
    ns = CacheNamespace.legacy()
    mapping = ImportScopeMap((CacheScopeMapping(ns, ns),))
    entry = dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(b'one').hexdigest(), original_text='one', translated_text='um', created_at='2026')
    source.write_text(json.dumps({'version': 2, 'namespaces': [{'kind': 'legacy-v1', 'entries': [entry]}]}))
    store = SQLiteCacheV2Store()
    req = ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping)
    preview = store.preview_import(req)
    source.write_text(source.read_text() + ' ')
    with pytest.raises(CacheStorageError, match='stale_preview'):
        store.execute_import(ExecuteImportRequest(preview))
    preview = store.preview_import(req)
    class Cancel:
        def checkpoint(self, point):
            return True if point == 'cache_before_commit' else None
    with pytest.raises(Exception, match='cancelled'):
        store.execute_import(ExecuteImportRequest(preview, cancellation=Cancel()))
    assert store.inspect(InspectRequest(CacheRef(str(cache)), namespace=ns)).count == 0


def test_legacy_export_matches_existing_serializer_and_rejects_cache_alias(tmp_path):
    from ayvu.cache_admin import ExportRequest
    cache, out = tmp_path / 'cache.sqlite', tmp_path / 'out.json'
    prepare_cache(cache)
    entry = dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(b'a').hexdigest(), original_text='a', translated_text='á', created_at='2026')
    with participating_connection(cache) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', tuple(entry.values()))
        conn.commit()
    ns = CacheNamespace.legacy()
    store = SQLiteCacheV2Store()
    assert store.export_interchange(ExportRequest(CacheRef(str(cache)), FileRef(str(out)), namespace=ns)).count == 1
    assert out.read_bytes() == (json.dumps({'version': 1, 'entries': [entry]}, ensure_ascii=False, indent=2) + '\n').encode()
    with pytest.raises(CacheStorageError):
        store.export_interchange(ExportRequest(CacheRef(str(cache)), FileRef(str(cache) + '-wal'), namespace=ns))


def test_wal_import_rejected_before_creating_stage(tmp_path):
    import sqlite3
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    prepare_cache(cache)
    ns = CacheNamespace.legacy()
    mapping = ImportScopeMap((CacheScopeMapping(ns, ns),))
    source.write_text('{"version":2,"namespaces":[{"kind":"legacy-v1","entries":[]}]}')
    with sqlite3.connect(cache) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    before = cache.read_bytes()
    with pytest.raises(CacheStorageError, match='unsupported_audit_mode'):
        SQLiteCacheV2Store().preview_import(ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping))
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('.ayvu-cache-import-*'))


def test_discard_is_bound_to_issued_preview(tmp_path):
    from dataclasses import replace
    cache, source, other = tmp_path / 'cache.sqlite', tmp_path / 'input.json', tmp_path / 'other'
    prepare_cache(cache)
    ns = CacheNamespace.legacy()
    mapping = ImportScopeMap((CacheScopeMapping(ns, ns),))
    source.write_text('{"version":2,"namespaces":[{"kind":"legacy-v1","entries":[]}]}')
    store = SQLiteCacheV2Store()
    preview = store.preview_import(ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping))
    other.write_text('keep')
    assert store.discard_import(replace(preview, stage=FileRef(str(other)))) == 0
    assert other.read_text() == 'keep'
    assert store.discard_import(preview) == 1
    assert store.discard_import(preview) == 0


@pytest.mark.parametrize('divergent,reverse,replace_existing', [(a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)])
def test_many_to_one_remapping_checks_complete_row(tmp_path, divergent, reverse, replace_existing):
    from dataclasses import replace
    from datetime import UTC, datetime
    from ayvu.cache_admin import ExportRequest
    from ayvu.cache_identity import CacheAuthorizationPolicy
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    original = entry()
    second_scope = CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004322').resolve()
    destination = CacheNamespace.v2(CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004323').resolve())
    source_a, source_b = CacheNamespace.v2(original.key.scope), CacheNamespace.v2(second_scope)
    other = replace(original, key=replace(original.key, scope=second_scope), value='different' if divergent else original.value)
    src = tmp_path / 'src.sqlite'
    repo = SQLiteExactCache(src, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    repo.put_if_absent(original)
    repo.put_if_absent(other)
    out, target = tmp_path / 'payload.json', tmp_path / 'target.sqlite'
    prepare_cache(target)
    store = SQLiteCacheV2Store()
    store.export_interchange(ExportRequest(CacheRef(str(src)), FileRef(str(out)), namespace=CacheNamespace.all()))
    data = json.loads(out.read_text())
    data['namespaces'] = [section for section in data['namespaces'] if section['kind'] == 'v2']
    if reverse:
        data['namespaces'].reverse()
    out.write_text(json.dumps(data))
    scope_map = ImportScopeMap((CacheScopeMapping(source_a, destination), CacheScopeMapping(source_b, destination)))
    req = ImportRequest(CacheRef(str(target)), FileRef(str(out)), replace=replace_existing, scope_map=scope_map)
    if divergent:
        with pytest.raises(CacheStorageError, match='duplicate_conflict'):
            store.preview_import(req)
        assert store.inspect(InspectRequest(CacheRef(str(target)), namespace=destination)).count == 0
    else:
        preview = store.preview_import(req)
        assert (preview.count, preview.duplicates, preview.imported, preview.skipped) == (2, 1, 1, 1)
        result = store.execute_import(ExecuteImportRequest(preview))
        assert (result.imported, result.skipped) == (1, 1)


def test_timestamp_conflict_requires_replace_and_stage_tamper_rejected(tmp_path):
    import sqlite3
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    prepare_cache(cache)
    ns = CacheNamespace.legacy()
    mapping = ImportScopeMap((CacheScopeMapping(ns, ns),))
    item = dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(b'a').hexdigest(), original_text='a', translated_text='um', created_at='2026')
    with participating_connection(cache) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', (*list(item.values())[:-1], '2025'))
        conn.commit()
    source.write_text(json.dumps({'version': 2, 'namespaces': [{'kind': 'legacy-v1', 'entries': [item]}]}))
    store = SQLiteCacheV2Store()
    req = ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=mapping)
    preview = store.preview_import(req)
    assert preview.conflicts == 1
    with pytest.raises(CacheStorageError, match='import_conflict'):
        store.execute_import(ExecuteImportRequest(preview))
    from dataclasses import replace
    replace_req = replace(req, replace=True)
    preview = store.preview_import(replace_req)
    with sqlite3.connect(preview.stage.value) as stage:
        stage.execute("UPDATE rows SET row = row || ' '")
    with pytest.raises(CacheStorageError, match='stale_preview'):
        store.execute_import(ExecuteImportRequest(preview))
    preview = store.preview_import(replace_req)
    assert store.execute_import(ExecuteImportRequest(preview)).replaced == 1
    with participating_connection(cache) as conn:
        assert conn.execute('SELECT created_at FROM translations').fetchone()[0] == '2026'


def test_explicit_legacy_import_to_unmigrated_v1_does_not_promote(tmp_path):
    import sqlite3
    from ayvu.cache import SCHEMA
    from ayvu.cache_schema import classify_cache, SchemaState
    cache, source = tmp_path / 'v1.sqlite', tmp_path / 'input.json'
    with sqlite3.connect(cache) as conn:
        conn.execute(SCHEMA)
    item = dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(b'a').hexdigest(), original_text='a', translated_text='um', created_at='2026')
    source.write_text(json.dumps({'version': 1, 'entries': [item]}))
    ns = CacheNamespace.legacy()
    request = ImportRequest(CacheRef(str(cache)), FileRef(str(source)), scope_map=ImportScopeMap((CacheScopeMapping(ns, ns),)))
    store = SQLiteCacheV2Store()
    preview = store.preview_import(request)
    assert store.execute_import(ExecuteImportRequest(preview)).imported == 1
    assert classify_cache(cache) is SchemaState.V1
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT translated_text FROM translations').fetchone() == ('um',)


def test_export_rejects_replaced_staging_file(tmp_path):
    import os
    from ayvu.cache_admin import ExportRequest
    cache, out, hostile = tmp_path / 'cache.sqlite', tmp_path / 'out.json', tmp_path / 'hostile'
    prepare_cache(cache)
    hostile.write_text('hostile')
    class ReplaceStage:
        def checkpoint(self, point):
            if point == 'cache_before_publish':
                staged = next(tmp_path.glob('.ayvu-cache-export-*'))
                staged.unlink()
                os.symlink(hostile, staged)
    with pytest.raises(CacheStorageError):
        SQLiteCacheV2Store().export_interchange(ExportRequest(CacheRef(str(cache)), FileRef(str(out)), namespace=CacheNamespace.legacy(), cancellation=ReplaceStage()))
    assert not out.exists()
    assert hostile.read_text() == 'hostile'


def test_preview_binds_broader_scope_authority(tmp_path):
    from dataclasses import replace
    from ayvu.cache_admin import ExportRequest
    from ayvu.cache_identity import CacheAuthorizationPolicy, CacheScope, CacheScopeKind
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    scope = CacheScope(CacheScopeKind.WORK, 'shared')
    first = CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004321', (scope,)).resolve(scope)
    second = CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004322', (scope,)).resolve(scope)
    src, dst, out = tmp_path / 'src.sqlite', tmp_path / 'dst.sqlite', tmp_path / 'data.json'
    original = entry()
    SQLiteExactCache(src).put_if_absent(original)
    prepare_cache(dst)
    source_ns, dest_ns = CacheNamespace.v2(original.key.scope), CacheNamespace.v2(first)
    store = SQLiteCacheV2Store()
    store.export_interchange(ExportRequest(CacheRef(str(src)), FileRef(str(out)), namespace=source_ns))
    mapping = ImportScopeMap((CacheScopeMapping(source_ns, dest_ns),))
    preview = store.preview_import(ImportRequest(CacheRef(str(dst)), FileRef(str(out)), scope_map=mapping))
    altered = ImportScopeMap((CacheScopeMapping(source_ns, CacheNamespace.v2(second)),))
    with pytest.raises(CacheStorageError, match='stale_preview'):
        store.execute_import(ExecuteImportRequest(replace(preview, scope_map=altered)))
    assert store.inspect(InspectRequest(CacheRef(str(dst)), namespace=dest_ns)).count == 0


def test_explicit_v1_namespace_inspect_export_and_clean_without_promotion(tmp_path):
    import sqlite3
    from ayvu.cache import SCHEMA
    from ayvu.cache_admin import ExportRequest
    from ayvu.cache_schema import SchemaState, classify_cache
    cache, output = tmp_path / 'v1.sqlite', tmp_path / 'out.json'
    entry = dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(b'a').hexdigest(), original_text='a', translated_text='um', created_at='2026')
    with sqlite3.connect(cache) as conn:
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', tuple(entry.values()))
    store, namespace = SQLiteCacheV2Store(), CacheNamespace.all()
    summary = store.inspect(InspectRequest(CacheRef(str(cache)), namespace=namespace))
    assert (summary.count, summary.adapter, summary.layout) == (1, 'legacy_v1', 'sqlite_translations')
    assert store.export_interchange(ExportRequest(CacheRef(str(cache)), FileRef(str(output)), namespace=namespace)).count == 1
    assert json.loads(output.read_text()) == {'version': 1, 'entries': [entry]}
    selection = store.preview_clean(PreviewCleanRequest(CacheRef(str(cache)), CacheFilter(all_entries=True), namespace=namespace))
    assert store.execute_clean(ExecuteCleanRequest(selection)) == 1
    assert classify_cache(cache) is SchemaState.V1
