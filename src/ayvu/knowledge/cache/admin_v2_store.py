"""Administration of the semantic cache through the migration lock."""
from __future__ import annotations

from pathlib import Path

from .admin import CacheSummary, CachePair, CleanSelection, checkpoint, report
from .admin_store import CacheStorageError, _error
from .schema import classify_cache_waiting, SchemaState, _location, _identity, _state
from . import admin_store as legacy_storage
from .admin_v2_queries import _administration_connection, _empty_selection, _integrity, _selection, _table_exists, _tables, _validate_stored_sizes, _where
from .admin_v2_export import _export, _export_legacy
from .admin_v2_import import _discard_import, _execute_import, _preview_import


class SQLiteCacheV2Store:
    """Explicit namespace administration; never prepares or migrates a database."""

    def inspect(self, request):
        try:
            checkpoint(request, 'cache_before_read')
            report(request, 'inspect', 0, 1)
            path = Path(request.cache.value)
            for table in _tables(request.namespace):
                _where(request.namespace, request.filters, table)
            if classify_cache_waiting(path, timeout=legacy_storage.BUSY_TIMEOUT_SECONDS) is SchemaState.ABSENT:
                report(request, 'inspect', 1, 1)
                semantic = request.namespace.kind == 'v2'
                return CacheSummary(False, 0, 0, (), adapter='semantic_v2' if semantic else 'legacy_v1',
                                    layout='sqlite_cache_v2' if semantic else 'sqlite_translations', namespace=request.namespace)
            with _administration_connection(path, request.namespace) as conn:
                if not conn.in_transaction:
                    conn.execute('BEGIN')
                _validate_stored_sizes(conn, request)
                count, _, counts = _selection(conn, request.namespace, request.filters, request)
                semantic = _state(conn, integrity=False) is SchemaState.V2
                pairs = ()
                # Per-language summaries have no meaning for semantic entries.
                if (request.namespace.kind == 'legacy-v1' or not semantic) and _table_exists(conn, 'translations'):
                    where, args = _where(request.namespace, request.filters, 'translations')
                    rows = conn.execute('SELECT source_lang,target_lang,COUNT(*),MIN(created_at),MAX(created_at) FROM translations' + where + ' GROUP BY source_lang,target_lang ORDER BY source_lang,target_lang LIMIT 1001', args).fetchall()
                    if len(rows) > 1000:
                        raise CacheStorageError('metadata_limit')
                    pairs = tuple(CachePair(*row) for row in rows)
            report(request, 'inspect', 1, 1)
            return CacheSummary(True, count, path.stat().st_size, pairs,
                                adapter='semantic_v2' if semantic else 'legacy_v1',
                                layout='sqlite_cache_v2' if semantic else 'sqlite_translations',
                                namespace=request.namespace, namespaces=counts)
        except Exception as exc:
            raise _error(exc) from None

    def preview_clean(self, request):
        try:
            checkpoint(request, 'cache_before_read')
            report(request, 'preview', 0, 1)
            for table in _tables(request.namespace):
                _where(request.namespace, request.filters, table)
            if classify_cache_waiting(Path(request.cache.value), timeout=legacy_storage.BUSY_TIMEOUT_SECONDS) is SchemaState.ABSENT:
                return CleanSelection(request.cache, request.filters, 0, _empty_selection(request.cache, request.namespace, request.filters), namespace=request.namespace, namespaces=())
            with _administration_connection(Path(request.cache.value), request.namespace) as conn:
                if not conn.in_transaction:
                    conn.execute('BEGIN')
                count, digest, counts = _selection(conn, request.namespace, request.filters, request)
            report(request, 'preview', 1, 1)
            return CleanSelection(request.cache, request.filters, count, digest, namespace=request.namespace, namespaces=counts)
        except Exception as exc:
            raise _error(exc) from None

    def execute_clean(self, request):
        try:
            selection = request.selection
            checkpoint(request, 'cache_before_mutation')
            report(request, 'clean', 0, selection.count)
            if classify_cache_waiting(Path(selection.cache.value), timeout=legacy_storage.BUSY_TIMEOUT_SECONDS) is SchemaState.ABSENT:
                if selection.count == 0 and selection.digest == _empty_selection(selection.cache, selection.namespace, selection.filters):
                    return 0
                raise CacheStorageError('stale_preview')
            path = Path(selection.cache.value)
            expected = _identity(path)
            with _administration_connection(path, selection.namespace, write=True) as conn:
                checkpoint(request, 'cache_before_mutation')
                conn.execute('BEGIN IMMEDIATE')
                count, digest, _ = _selection(conn, selection.namespace, selection.filters, request)
                if (count, digest) != (selection.count, selection.digest):
                    raise CacheStorageError('stale_preview')
                checkpoint(request, 'cache_before_mutation')
                deleted = 0
                for table in _tables(selection.namespace):
                    where, args = _where(selection.namespace, selection.filters, table)
                    if not _table_exists(conn, table):
                        continue
                    deleted += conn.execute('DELETE FROM ' + table + where, args).rowcount
                checkpoint(request, 'cache_before_commit')
                _integrity(conn, 'main')
                checkpoint(request, 'cache_before_commit')
                report(request, 'clean', deleted, count)
                _location(path)
                if _identity(path) != expected:
                    raise CacheStorageError('cache_identity_changed')
                conn.commit()
                return deleted
        except Exception as exc:
            raise _error(exc) from None


SQLiteCacheV2Store.export_interchange = _export


SQLiteCacheV2Store.export_legacy = _export_legacy


SQLiteCacheV2Store.preview_import = _preview_import


SQLiteCacheV2Store.execute_import = _execute_import


SQLiteCacheV2Store.discard_import = _discard_import
