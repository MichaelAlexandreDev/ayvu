import hashlib
import json
import sqlite3

import pytest

from ayvu.cache_admin import CacheRef, FileRef, ImportRequest, CacheAdminError
from ayvu.cache_admin_legacy_store import SQLiteLegacyStreamingStore
from ayvu.cache_schema import classify_cache, SchemaState, prepare_cache


def entry(text='a', translated='first', created='2026'):
    return dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(text.encode()).hexdigest(), original_text=text, translated_text=translated, created_at=created)


def request(cache, source, replace=False, cancellation=None):
    return ImportRequest(CacheRef(str(cache)), FileRef(str(source)), replace=replace, cancellation=cancellation)


def test_full_validation_precedes_cache_creation_and_preserves_unknown_fields(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    item = entry()
    source.write_text(json.dumps({'entries': [dict(item, extra={'x': 1})], 'unknown': [1, {'a': 2}], 'version': 1}))
    store = SQLiteLegacyStreamingStore()
    counts = store.import_cache(request(cache, source))
    assert (counts.imported, counts.skipped, counts.replaced) == (1, 0, 0)
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT translated_text FROM translations').fetchone() == ('first',)
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'entries': [item, dict(item, original_text_hash='bad')], 'version': 1}))
    before = cache.read_bytes()
    with pytest.raises(CacheAdminError):
        store.import_cache(request(cache, bad))
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))


@pytest.mark.parametrize('replace', [False, True])
def test_duplicate_input_entries_keep_legacy_order_and_counts(tmp_path, replace):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    source.write_text(json.dumps({'version': 1, 'entries': [entry(), entry(translated='second', created='2027')]}))
    store = SQLiteLegacyStreamingStore()
    result = store.import_cache(request(cache, source, replace))
    assert (result.imported, result.skipped, result.replaced) == ((1, 0, 1) if replace else (1, 1, 0))
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT translated_text,created_at FROM translations').fetchone() == (('second', '2027') if replace else ('first', '2026'))


def test_malformed_or_cancelled_input_does_not_mutate_database(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    source.write_text('{"version":1,"entries":[],"unknown":{"k":1,"k":2}}')
    store = SQLiteLegacyStreamingStore()
    with pytest.raises(CacheAdminError):
        store.import_cache(request(cache, source))
    assert not cache.exists()
    source.write_text(json.dumps({'version': 1, 'entries': [entry()]}))
    class Cancel:
        def checkpoint(self, point):
            return True if point == 'cache_before_commit' else None
    with pytest.raises(CacheAdminError, match='cancelled'):
        store.import_cache(request(cache, source, cancellation=Cancel()))
    with sqlite3.connect(cache) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='translations'").fetchone()[0] == 0
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))


def test_v2_gate_leaves_database_unchanged(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    prepare_cache(cache)
    before = cache.read_bytes()
    source.write_text('{"version":1,"entries":[]}')
    with pytest.raises(CacheAdminError, match='v2-admin-required'):
        SQLiteLegacyStreamingStore().import_cache(request(cache, source))
    assert cache.read_bytes() == before
    assert classify_cache(cache) is SchemaState.V2


def test_production_v1_services_never_use_whole_payload_ports(tmp_path, monkeypatch):
    from ayvu.cache import SCHEMA
    from ayvu.cache_admin import ExportRequest
    from ayvu.cache_admin_composition import cache_admin_services
    from ayvu.cache_admin_store import SQLiteCacheStore, LocalCacheFiles
    source, destination, payload = tmp_path / 'source.sqlite', tmp_path / 'destination.sqlite', tmp_path / 'data.json'
    row = entry()
    with sqlite3.connect(source) as conn:
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO translations(source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', tuple(row.values()))
    def forbidden(*args, **kwargs): raise AssertionError('whole-payload compatibility port must remain unused')
    for owner, name in ((SQLiteCacheStore, 'export_payload'), (SQLiteCacheStore, 'import_payload'),
                         (LocalCacheFiles, 'read'), (LocalCacheFiles, 'publish')):
        monkeypatch.setattr(owner, name, forbidden)
    services = cache_admin_services()
    exported = services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(payload))))
    assert exported.failure is None and exported.value == 1
    imported = services.import_cache.execute(request(destination, payload))
    assert imported.failure is None and imported.value.imported == 1


def test_dense_unknown_legacy_keys_are_staged_on_disk_and_discarded(tmp_path):
    source, cache = tmp_path / 'input.json', tmp_path / 'legacy.sqlite'
    with source.open('w') as stream:
        stream.write('{"unknown":{')
        for index in range(25_000):
            if index: stream.write(',')
            stream.write(json.dumps(str(index)) + ':0')
        stream.write('},"version":1,"entries":[' + json.dumps(entry()) + ']}')
    result = SQLiteLegacyStreamingStore().import_cache(request(cache, source))
    assert result.imported == 1
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT COUNT(*) FROM translations').fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='json_keys'").fetchone() is None
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))


def test_request_limits_can_only_narrow_legacy_file_and_entry_ceilings(tmp_path):
    from dataclasses import replace
    from ayvu.cache_admin import CacheInterchangeLimits
    cache, source = tmp_path / 'legacy.sqlite', tmp_path / 'data.json'
    source.write_text(json.dumps(dict(version=1, entries=[entry()])))
    store = SQLiteLegacyStreamingStore()
    size = source.stat().st_size
    req = request(cache, source)
    with pytest.raises(CacheAdminError, match='limit_exceeded'):
        store.import_cache(replace(req, limits=CacheInterchangeLimits(raw_input=size - 1)))
    assert not cache.exists()
    assert store.import_cache(replace(req, limits=CacheInterchangeLimits(raw_input=size, entries=1))).imported == 1
    before = cache.read_bytes()
    source.write_text(json.dumps(dict(version=1, entries=[entry(), entry('b')])))
    with pytest.raises(CacheAdminError, match='too_many_entries'):
        store.import_cache(replace(req, limits=CacheInterchangeLimits(entries=1)))
    assert cache.read_bytes() == before


def test_source_changed_before_transaction_leaves_existing_db_unchanged(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    store = SQLiteLegacyStreamingStore()
    source.write_text(json.dumps({'version': 1, 'entries': [entry('existing')]}))
    store.import_cache(request(cache, source))
    before = cache.read_bytes()
    source.write_text(json.dumps({'version': 1, 'entries': [entry('next')]}))
    class Change:
        def checkpoint(self, point):
            if point == 'cache_before_mutation':
                source.write_text(source.read_text() + ' ')
    with pytest.raises(CacheAdminError):
        store.import_cache(request(cache, source, cancellation=Change()))
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))


def test_existing_conflicts_skip_or_replace_without_collapsing(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    store = SQLiteLegacyStreamingStore()
    source.write_text(json.dumps({'version': 1, 'entries': [entry()]}))
    assert store.import_cache(request(cache, source)).imported == 1
    source.write_text(json.dumps({'version': 1, 'entries': [entry(translated='two'), entry(translated='three')]}))
    skip = store.import_cache(request(cache, source))
    assert (skip.imported, skip.skipped, skip.replaced) == (0, 2, 0)
    changed = store.import_cache(request(cache, source, True))
    assert (changed.imported, changed.skipped, changed.replaced) == (0, 0, 2)
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT translated_text FROM translations').fetchone() == ('three',)


def test_sql_failure_rolls_back_preceding_insert(tmp_path):
    from ayvu.cache import SCHEMA
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    with sqlite3.connect(cache) as conn:
        conn.execute(SCHEMA)
        conn.execute("CREATE TRIGGER abort_insert BEFORE INSERT ON translations WHEN NEW.original_text='b' BEGIN SELECT RAISE(ABORT,'x'); END")
    source.write_text(json.dumps({'version': 1, 'entries': [entry('a'), entry('b')]}))
    with pytest.raises(CacheAdminError):
        SQLiteLegacyStreamingStore().import_cache(request(cache, source))
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT count(*) FROM translations').fetchone()[0] == 0
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))


def test_input_size_ceiling_uses_runtime_legacy_limit(tmp_path, monkeypatch):
    import ayvu.cache_admin_store as legacy
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    source.write_bytes(b'x' * 10)
    monkeypatch.setattr(legacy, 'MAX_FILE_BYTES', 5)
    with pytest.raises(CacheAdminError, match='limit_exceeded'):
        SQLiteLegacyStreamingStore().import_cache(request(cache, source))
    assert not cache.exists()
    assert not list(tmp_path.glob('.ayvu-cache-import-legacy-*'))


def test_changed_source_before_first_transaction_does_not_create_db(tmp_path):
    cache, source = tmp_path / 'cache.sqlite', tmp_path / 'input.json'
    source.write_text(json.dumps({'version': 1, 'entries': [entry()]}))
    class Change:
        def checkpoint(self, point):
            if point == 'cache_before_mutation':
                source.write_text(source.read_text() + ' ')
    with pytest.raises(CacheAdminError, match='stale_preview'):
        SQLiteLegacyStreamingStore().import_cache(request(cache, source, cancellation=Change()))
    assert not cache.exists()


def test_invalid_input_removes_new_empty_cache_directories(tmp_path):
    cache, source = tmp_path / 'new' / 'nested' / 'cache.sqlite', tmp_path / 'input.json'
    source.write_text('{"version":1,"entries":[{}]}')
    with pytest.raises(CacheAdminError, match='malformed_entry'):
        SQLiteLegacyStreamingStore().import_cache(request(cache, source))
    assert not (tmp_path / 'new').exists()


def test_replaced_stage_is_rejected_before_insert(tmp_path):
    cache, source, victim = tmp_path / 'cache.sqlite', tmp_path / 'input.json', tmp_path / 'victim'
    source.write_text(json.dumps({'version': 1, 'entries': [entry()]}))
    victim.write_text('preserve')
    class ReplaceStage:
        def checkpoint(self, point):
            if point == 'cache_between_entries':
                stages = list(tmp_path.glob('.ayvu-cache-import-legacy-*.sqlite'))
                if stages:
                    stages[0].unlink()
                    stages[0].symlink_to(victim)
    with pytest.raises(CacheAdminError):
        SQLiteLegacyStreamingStore().import_cache(request(cache, source, cancellation=ReplaceStage()))
    assert not cache.exists()
    assert victim.read_text() == 'preserve'
