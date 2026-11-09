"""Losslessness across the complete production administration boundary."""
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from ayvu.cache_admin import (CacheRef, FileRef, CacheNamespace, CacheScopeMapping,
    ImportScopeMap, ExportRequest, ImportRequest, ExecuteImportRequest)
from ayvu.cache_admin_composition import cache_admin_services
from ayvu.cache_identity import CacheAuthorizationPolicy, CacheValueKind, CacheScope, CacheScopeKind
from ayvu.cache_repository import SQLiteExactCache, _digest, _entry_digest
from ayvu.cache_schema import participating_connection, V2_COLUMNS
from test_cache_repository_v2 import entry


def complete_rows(path):
    with participating_connection(path) as conn:
        return (conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2 ORDER BY scope_kind,scope_id,value_kind,semantic_fingerprint,source_digest').fetchall(),
                conn.execute('SELECT source_lang,target_lang,original_text_hash,original_text,translated_text,created_at FROM translations ORDER BY source_lang,target_lang,original_text_hash').fetchall())


@pytest.fixture
def cachewide(tmp_path):
    path = tmp_path / 'source.sqlite'
    item = entry(metadata=b'{ "flags" : [1, true] }\n')
    SQLiteExactCache(path, clock=lambda: datetime(2026, 11, 5, 10, 11, 12, 345678, UTC)).put_if_absent(item)
    # Preserve a legitimate noncanonical provenance serialization byte for byte.
    with participating_connection(path) as conn:
        row = conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2').fetchone()
        provenance = row[9] + b' \n'
        pd = _digest(provenance)
        ed = _entry_digest(item.key, row[6], row[7], row[5], row[8], provenance, row[10], pd)
        conn.execute('UPDATE cache_entries_v2 SET provenance=?,provenance_digest=?,entry_digest=?', (provenance, pd, ed))
        for text in ('z', 'a'):
            conn.execute('INSERT INTO translations(source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES(?,?,?,?,?,?)',
                         ('en', 'pt', hashlib.sha256(text.encode()).hexdigest(), text, 'legacy-' + text, '2024-02-03 00:00:00'))
        conn.commit()
    return path, item


def export(services, cache, destination, namespace):
    result = services.export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(destination)), namespace=namespace))
    assert result.failure is None, result.failure
    return result


def mapping(source, destination=None):
    legacy = CacheNamespace.legacy()
    return ImportScopeMap((CacheScopeMapping(legacy, legacy),
                           CacheScopeMapping(CacheNamespace.v2(source), CacheNamespace.v2(destination or source))))


def test_cachewide_lossless_deterministic_same_scope_and_separate_audit(cachewide, tmp_path):
    source, item = cachewide
    destination = tmp_path / 'destination.sqlite'
    SQLiteExactCache(destination)
    services = cache_admin_services()
    exported = tmp_path / 'export.json'
    receipt = export(services, source, exported, CacheNamespace.all())
    assert (receipt.value, receipt.format_version, receipt.namespace.kind) == (3, 2, 'all')
    before = complete_rows(source)
    request = ImportRequest(CacheRef(str(destination)), FileRef(str(exported)), scope_map=mapping(item.key.scope))
    preview = services.preview_import.execute(request)
    assert preview.failure is None
    result = services.execute_import.execute(ExecuteImportRequest(preview.value))
    assert result.failure is None
    assert (result.value.imported, result.value.replaced) == (3, 0)
    assert complete_rows(destination) == before
    assert SQLiteExactCache(destination).lookup(item.key).value == item.value
    again = tmp_path / 'again.json'
    export(services, destination, again, CacheNamespace.all())
    assert again.read_bytes() == exported.read_bytes()
    with sqlite3.connect(preview.value.stage.value) as audit:
        events = audit.execute('SELECT event,count,policy FROM audit').fetchall()
        assert events[0][:2] == ('committed', 3)
        assert item.key.scope.scope.scope_id in events[0][2]
    second = services.preview_import.execute(request)
    assert (second.value.skipped, second.value.conflicts) == (3, 0)
    assert services.execute_import.execute(ExecuteImportRequest(second.value)).value.skipped == 3


def test_remap_changes_only_scope_partition_and_derived_digest(cachewide, tmp_path):
    source, item = cachewide
    destination = tmp_path / 'destination.sqlite'
    SQLiteExactCache(destination)
    authority = CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004333').resolve()
    services, exported = cache_admin_services(), tmp_path / 'export.json'
    export(services, source, exported, CacheNamespace.all())
    req = ImportRequest(CacheRef(str(destination)), FileRef(str(exported)), scope_map=mapping(item.key.scope, authority))
    preview = services.preview_import.execute(req)
    assert preview.failure is None
    assert services.execute_import.execute(ExecuteImportRequest(preview.value)).failure is None
    original, _ = complete_rows(source)
    remapped, _ = complete_rows(destination)
    assert remapped[0][:2] == ('project', authority.scope.scope_id)
    assert remapped[0][2:12] == original[0][2:12]
    assert remapped[0][12] != original[0][12]
    assert SQLiteExactCache(destination).lookup(replace(item.key, scope=authority)).value == item.value
    with sqlite3.connect(preview.value.stage.value) as audit:
        policy = json.loads(audit.execute('SELECT policy FROM audit').fetchone()[0])
        assert item.key.scope.scope.scope_id in repr(policy)
        assert authority.scope.scope_id in repr(policy)


def test_unused_map_source_rejected_before_mutation_and_stage_cleanup(cachewide, tmp_path, monkeypatch):
    source, item = cachewide
    destination = tmp_path / 'destination.sqlite'
    SQLiteExactCache(destination)
    services, exported = cache_admin_services(), tmp_path / 'export.json'
    export(services, source, exported, CacheNamespace.all())
    extra = CacheNamespace.v2(CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004334').resolve())
    base = mapping(item.key.scope)
    scope_map = ImportScopeMap((*base.entries, CacheScopeMapping(extra, extra)))
    before = destination.read_bytes()
    stages = []
    def forbidden_stage(*args, **kwargs):
        stages.append(True)
        raise AssertionError('scope coverage must fail before staging')
    monkeypatch.setattr('ayvu.cache_admin_v2_store.tempfile.mkstemp', forbidden_stage)
    result = services.preview_import.execute(ImportRequest(CacheRef(str(destination)), FileRef(str(exported)), scope_map=scope_map))
    assert result.failure.code == 'unmapped_scope'
    assert destination.read_bytes() == before
    assert stages == []
    assert not list(tmp_path.glob('.ayvu-cache-import-*'))


def test_replace_preserves_other_kind_and_scope_and_replaces_complete_payload(tmp_path):
    source, destination = tmp_path / 'source.sqlite', tmp_path / 'destination.sqlite'
    original = entry(value='original', metadata=b'{"count":1}')
    incoming = replace(original, value='replacement', metadata=b' { "count" : 2 } ',
                       provenance=replace(original.provenance, operation='translate-again'))
    other_kind = replace(original, key=replace(original.key, value_kind=CacheValueKind('another-stage', 1)))
    other_scope = CacheAuthorizationPolicy('00000000-0000-0000-0000-000000004335').resolve()
    unrelated = replace(original, key=replace(original.key, scope=other_scope))
    SQLiteExactCache(source, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)).put_if_absent(incoming)
    target = SQLiteExactCache(destination, clock=lambda: datetime(2025, 1, 1, tzinfo=UTC))
    for value in (original, other_kind, unrelated): target.put_if_absent(value)
    before = complete_rows(destination)[0]
    services, exported = cache_admin_services(), tmp_path / 'payload.json'
    export(services, source, exported, CacheNamespace.v2(original.key.scope))
    ns = CacheNamespace.v2(original.key.scope)
    req = ImportRequest(CacheRef(str(destination)), FileRef(str(exported)), replace=True,
                        scope_map=ImportScopeMap((CacheScopeMapping(ns, ns),)))
    preview = services.preview_import.execute(req)
    assert (preview.value.conflicts, preview.value.replaced) == (1, 1)
    assert services.execute_import.execute(ExecuteImportRequest(preview.value)).value.replaced == 1
    after = complete_rows(destination)[0]
    incoming_row = complete_rows(source)[0][0]
    assert len(after) == 3
    assert incoming_row in after
    assert [row for row in after if row[:5] != incoming_row[:5]] == [row for row in before if row[:5] != incoming_row[:5]]


@pytest.mark.parametrize('kind', [CacheScopeKind.WORK, CacheScopeKind.LOCAL_USER])
def test_broader_authorized_namespaces_roundtrip_real_repository(tmp_path, kind):
    original = entry()
    scope = CacheScope(kind, 'opaque-local-scope')
    authorized = CacheAuthorizationPolicy(original.key.scope.project_id, (scope,)).resolve(scope)
    item = replace(original, key=replace(original.key, scope=authorized))
    source, destination = tmp_path / 'source.sqlite', tmp_path / 'destination.sqlite'
    SQLiteExactCache(source).put_if_absent(item)
    SQLiteExactCache(destination)
    services, payload = cache_admin_services(), tmp_path / 'data.json'
    ns = CacheNamespace.v2(authorized)
    export(services, source, payload, ns)
    req = ImportRequest(CacheRef(str(destination)), FileRef(str(payload)),
                        scope_map=ImportScopeMap((CacheScopeMapping(ns, ns),)))
    preview = services.preview_import.execute(req)
    assert preview.failure is None
    assert services.execute_import.execute(ExecuteImportRequest(preview.value)).value.imported == 1
    assert complete_rows(source) == complete_rows(destination)


def test_export_format_receipt_comes_from_the_pinned_snapshot(tmp_path):
    from ayvu.cache import SCHEMA
    cache, output = tmp_path / 'v1.sqlite', tmp_path / 'data.json'
    with sqlite3.connect(cache) as conn:
        conn.execute(SCHEMA)
    checkpoints = []
    class MigrateBeforeRead:
        def checkpoint(self, point):
            if point == 'cache_before_read' and not checkpoints:
                checkpoints.append('migrated')
                SQLiteExactCache(cache).put_if_absent(entry())
            return None
    services = cache_admin_services()
    result = services.export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(output)),
        namespace=CacheNamespace.all(), cancellation=MigrateBeforeRead()))
    assert result.failure is None
    assert checkpoints == ['migrated']
    assert result.format_version == json.loads(output.read_text())['version'] == 2


def test_v2_before_filter_canonicalizes_legacy_cli_spelling_and_utc_offsets(tmp_path):
    from ayvu.cache_admin import CacheFilter, InspectRequest, PreviewCleanRequest, ExecuteCleanRequest
    path = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(path, clock=lambda: datetime(2026, 11, 5, 10, 11, 12, 345678, UTC)).put_if_absent(item)
    services, ref, namespace = cache_admin_services(), CacheRef(str(path)), CacheNamespace.v2(item.key.scope)
    for before, count in [('2026-11-05 12:00:00', 1), ('2026-11-05T10:11:12.345678Z', 0),
                           ('2026-11-05T12:11:12.345678+02:00', 0), ('2026-11-05T12:11:12.345679+02:00', 1)]:
        result = services.inspect.execute(InspectRequest(ref, CacheFilter(before=before), namespace=namespace))
        assert result.value.count == count
    selection = services.preview.execute(PreviewCleanRequest(ref, CacheFilter(before='2026-11-05 12:00:00'), namespace=namespace))
    assert selection.value.count == 1
    assert services.clean.execute(ExecuteCleanRequest(selection.value)).value == 1
