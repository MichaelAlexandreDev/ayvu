"""Authorized SQLite views, selection and integrity checks for cache administration."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from datetime import UTC, datetime
import sqlite3

from .admin import CacheNamespaceCount, CacheNamespace, CacheFilter, checkpoint
from .admin_store import CacheStorageError, _filter
from .schema import V2_COLUMNS, participating_connection, classify_cache_waiting, SchemaState, participating_admin_connection
from . import admin_store as legacy_storage
from .identity_values import CACHE_ENTRY_V2_LIMITS
from .admin import namespace_reference
from .interchange import validate_v2_entry
from .admin_v2_records import _LEGACY_FIELDS


def _scope(namespace):
    if namespace.kind == 'v2':
        scope = namespace.scope.scope
        return scope.kind.value, scope.scope_id
    return None


def _where(namespace, filters, table):
    if table == 'cache_entries_v2':
        if filters.source_lang is not None or filters.target_lang is not None:
            raise CacheStorageError('invalid_filter')
        clauses, args = [], []
        if namespace.kind == 'v2':
            clauses.extend(('scope_kind=?', 'scope_id=?'))
            args.extend(_scope(namespace))
        if filters.before is not None:
            clauses.append('created_at < ?')
            try:
                before = datetime.fromisoformat(filters.before)
                before = before.replace(tzinfo=UTC) if before.tzinfo is None else before.astimezone(UTC)
                args.append(before.isoformat(timespec='microseconds').replace('+00:00', 'Z'))
            except (ValueError, OverflowError):
                raise CacheStorageError('invalid_filter') from None
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), tuple(args)
    return _filter(filters)


def _tables(namespace):
    if namespace.kind == 'v2':
        return ('cache_entries_v2',)
    if namespace.kind == 'legacy-v1':
        return ('translations',)
    if namespace.kind == 'all':
        return ('translations', 'cache_entries_v2')
    raise CacheStorageError('invalid_scope')


def _rows(conn, namespace, filters, request):
    for table in _tables(namespace):
        where, args = _where(namespace, filters, table)
        if not _table_exists(conn, table):
            continue
        cols = ','.join(V2_COLUMNS) if table == 'cache_entries_v2' else 'id,source_lang,target_lang,original_text_hash,original_text,translated_text,created_at'
        order = ','.join(V2_COLUMNS[:5]) if table == 'cache_entries_v2' else 'id'
        for row in conn.execute(f'SELECT {cols} FROM {table}{where} ORDER BY {order}', args):
            checkpoint(request, 'cache_between_entries')
            yield table, row


def _selection(conn, namespace, filters, request):
    digest = hashlib.sha256()
    info = Path(request.cache.value if hasattr(request, 'cache') else request.selection.cache.value).stat()
    digest.update(f'{info.st_dev}:{info.st_ino}'.encode('ascii'))
    digest.update(repr((_authority(namespace), filters.source_lang, filters.target_lang, filters.before, filters.all_entries)).encode('utf-8'))
    counts = {}
    for table, row in _rows(conn, namespace, filters, request):
        # SQLite blobs are hex encoded to preserve type and all byte values.
        encoded = json.dumps([table, [({'blob': x.hex()} if isinstance(x, bytes) else x) for x in row]],
                             ensure_ascii=True, separators=(',', ':')).encode('ascii')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
        key = ('legacy-v1', None, None) if table == 'translations' else ('v2', row[0], row[1])
        counts[key] = counts.get(key, 0) + 1
        if len(counts) > 10000:
            raise CacheStorageError('too_many_namespaces')
    return sum(counts.values()), digest.hexdigest(), tuple(CacheNamespaceCount(kind, n, scope_kind, scope_id) for (kind, scope_kind, scope_id), n in sorted(counts.items(), key=lambda item: tuple(x or '' for x in item[0])))


def _db_revision(conn, path, request):
    _validate_stored_sizes(conn, request)
    info = path.stat()
    h = hashlib.sha256(f'{info.st_dev}:{info.st_ino}'.encode('ascii'))
    for table, columns, order in (('translations', 'id,' + ','.join(_LEGACY_FIELDS), 'id'),
                                  ('cache_entries_v2', ','.join(V2_COLUMNS), ','.join(V2_COLUMNS[:5]))):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        for row in conn.execute(f'SELECT {columns} FROM {table} ORDER BY {order}'):
            checkpoint(request, 'cache_between_entries')
            data = json.dumps([table, [({'blob': x.hex()} if isinstance(x, bytes) else x) for x in row]], ensure_ascii=True, separators=(',', ':')).encode('ascii')
            h.update(len(data).to_bytes(8, 'big'))
            h.update(data)
    return h.hexdigest()


def _lookup(conn, kind, row):
    if kind == 'v2':
        found = conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2 WHERE scope_kind=? AND scope_id=? AND value_kind=? AND semantic_fingerprint=? AND source_digest=?', row[:5]).fetchone()
        if found is not None:
            try:
                descriptor = dict(kind='v2', scope_kind=found[0], scope_id=found[1])
                value = {name: item.decode('utf-8') if name in ('canonical_identity', 'metadata', 'provenance') else item
                         for name, item in zip(V2_COLUMNS[2:], found[2:])}
                validate_v2_entry(descriptor, value)
            except Exception:
                raise CacheStorageError('integrity_failure') from None
        return found
    return conn.execute('SELECT ' + ','.join(_LEGACY_FIELDS) + ' FROM translations WHERE source_lang=? AND target_lang=? AND original_text_hash=?', row[:3]).fetchone()


def _empty_selection(cache, namespace, filters):
    return hashlib.sha256(repr(('absent', str(Path(cache.value).absolute()), _authority(namespace), filters.source_lang, filters.target_lang, filters.before, filters.all_entries)).encode('utf-8')).hexdigest()


@contextmanager
def _import_connection(path, mapping):
    state = classify_cache_waiting(path, timeout=legacy_storage.BUSY_TIMEOUT_SECONDS)
    if state is SchemaState.CORRUPT:
        raise CacheStorageError('corrupt_cache')
    if state is SchemaState.V2:
        with participating_connection(path) as conn:
            yield conn
    elif state is SchemaState.V1 and all(destination[0] == 'legacy-v1' for destination in mapping.values()):
        with participating_admin_connection(path, write=True) as conn:
            yield conn
    else:
        raise CacheStorageError('namespace_unavailable')


def _authority(namespace):
    reference = namespace_reference(namespace)
    if namespace.kind == 'v2':
        return reference, namespace.scope.project_id, namespace.scope.explicitly_broader
    return reference, None, False


def _table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


@contextmanager
def _administration_connection(path, namespace, *, write=False):
    state = classify_cache_waiting(path, timeout=legacy_storage.BUSY_TIMEOUT_SECONDS)
    if state is SchemaState.CORRUPT:
        raise CacheStorageError('corrupt_cache')
    if state is SchemaState.V2:
        with participating_connection(path) as conn:
            yield conn
    elif state in (SchemaState.V1, SchemaState.EMPTY, SchemaState.FOREIGN) and namespace.kind != 'v2':
        with participating_admin_connection(path, write=write) as conn:
            yield conn
    else:
        raise CacheStorageError('namespace_unavailable')


def _integrity(conn, database):
    row = conn.execute(f'PRAGMA {database}.quick_check').fetchone()
    if row != ('ok',):
        raise CacheStorageError('integrity_failure')


def _validate_stored_sizes(conn, request):
    """Reject corrupt oversized rows in SQLite before Python allocates their text."""
    pending = []
    def interrupted():
        try:
            checkpoint(request, 'cache_between_entries')
            return 0
        except Exception as exc:
            pending.append(exc)
            return 1
    conn.set_progress_handler(interrupted, 10_000)
    try:
        namespace = getattr(request, 'namespace', None) or CacheNamespace.all()
        filters = getattr(request, 'filters', None) or CacheFilter()
        for table in _tables(namespace):
            if not _table_exists(conn, table):
                continue
            where, args = _where(namespace, filters, table)
            if table == 'cache_entries_v2':
                bounds = (('canonical_identity', CACHE_ENTRY_V2_LIMITS.canonical_identity),
                          ('scope_kind', 16), ('scope_id', 1024), ('value_kind', 85),
                          ('semantic_fingerprint', 71), ('source_digest', 71),
                          ('provenance_digest', 71), ('entry_digest', 71),
                          ('source_text', CACHE_ENTRY_V2_LIMITS.source_text),
                          ('value', CACHE_ENTRY_V2_LIMITS.value), ('metadata', CACHE_ENTRY_V2_LIMITS.metadata),
                          ('provenance', CACHE_ENTRY_V2_LIMITS.provenance), ('created_at', CACHE_ENTRY_V2_LIMITS.timestamp))
            else:
                bounds = tuple((field, 4 * (1_000_000 if field in ('original_text', 'translated_text') else 256)) for field in _LEGACY_FIELDS)
            oversized = ' OR '.join(f'length(CAST({field} AS BLOB)) > ?' for field, _ in bounds)
            row = conn.execute('SELECT 1 FROM ' + table + where + (' AND ' if where else ' WHERE ') +
                               '(' + oversized + ') LIMIT 1', (*args, *(ceiling for _, ceiling in bounds))).fetchone()
            if row is not None:
                raise CacheStorageError('text_too_large')
    except sqlite3.Error:
        if pending:
            raise pending[0]
        raise
    finally:
        conn.set_progress_handler(None, 0)
