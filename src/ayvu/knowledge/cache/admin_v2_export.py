"""Streaming cache exports and format compatibility."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .admin import ExportReceipt, checkpoint, report
from .admin_store import CacheStorageError, _error, _filter, _final_mode, _new_file_mode
from .schema import V2_COLUMNS, classify_cache_waiting, SchemaState, _location, _state
from .interchange import write_envelope
from .admin import validate_payload
from . import admin_store as legacy_storage
from .admin_v2_queries import _administration_connection, _scope, _table_exists, _tables, _validate_stored_sizes, _where
from .admin_v2_files import _file_identity, _publication_backup, _publish_new_owned, _safe_file, _unlink_owned, _verify_owned
from .admin_v2_records import _LEGACY_FIELDS


def _sections(conn, request):
    namespace, filters = request.namespace, request.filters
    for table in _tables(namespace):
        where, args = _where(namespace, filters, table)
        if not _table_exists(conn, table):
            continue
        if table == 'translations':
            def legacy_rows():
                fields = ('source_lang', 'target_lang', 'original_text_hash', 'original_text', 'translated_text', 'created_at')
                for row in conn.execute('SELECT ' + ','.join(fields) + ' FROM translations' + where + ' ORDER BY source_lang,target_lang,original_text_hash', args):
                    yield dict(zip(fields, row))
            yield {'kind': 'legacy-v1'}, legacy_rows()
        else:
            descriptors = [_scope(namespace)] if namespace.kind == 'v2' else conn.execute('SELECT DISTINCT scope_kind, scope_id FROM cache_entries_v2' + where + ' ORDER BY scope_kind,scope_id', args)
            for kind, scope_id in descriptors:
                descriptor = {'kind': 'v2', 'scope_kind': kind, 'scope_id': scope_id}
                def rows(kind=kind, scope_id=scope_id):
                    clause = ' AND ' if where else ' WHERE '
                    query = 'SELECT ' + ','.join(V2_COLUMNS[2:]) + ' FROM cache_entries_v2' + where + clause + 'scope_kind=? AND scope_id=? ORDER BY value_kind,semantic_fingerprint,source_digest'
                    for row in conn.execute(query, (*args, kind, scope_id)):
                        yield {key: value.decode('utf-8') if key in ('canonical_identity', 'metadata', 'provenance') else value for key, value in zip(V2_COLUMNS[2:], row)}
                yield descriptor, rows()


def _export(self, request, *, legacy_errors=False):
    name = None
    backup = None
    backup_identity = None
    try:
        checkpoint(request, 'cache_before_read')
        path, cache, target_identity, target_mode, parent_identity, cache_identity = _export_target_snapshot(request)
        fd, name = tempfile.mkstemp(prefix='.ayvu-cache-export-', dir=path.parent)
        staged_info = os.fstat(fd)
        staged_identity = (staged_info.st_dev, staged_info.st_ino)
        with os.fdopen(fd, 'wb') as stream:
            format_version, count = _write_export_payload(stream, cache, request)
            stream.flush()
            os.fchmod(stream.fileno(), _final_mode(path) if request.overwrite else _new_file_mode(path.parent))
            os.fsync(stream.fileno())
        checkpoint(request, 'cache_before_publish')
        _safe_file(path, cache)
        _safe_file(Path(name), cache, allow_owned=True)
        if (_file_identity(path) != target_identity or _file_identity(cache) != cache_identity or
                _file_identity(path.parent) != parent_identity or _file_identity(Path(name)) != staged_identity):
            raise CacheStorageError('cache_identity_changed')
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            parent_stat = os.fstat(directory_fd)
            if (parent_stat.st_dev, parent_stat.st_ino) != parent_identity:
                raise CacheStorageError('cache_identity_changed')
            os.fsync(directory_fd)
            if request.overwrite and target_identity is not None:
                backup, backup_identity = _publication_backup(path, target_identity, request)
            checkpoint(request, 'cache_before_publish')
            _safe_file(path, cache)
            _safe_file(Path(name), cache, allow_owned=True)
            _location(cache)
            if backup is not None:
                _verify_owned(Path(backup), backup_identity)
            if (_file_identity(path) != target_identity or _file_identity(path.parent) != parent_identity or
                    _file_identity(Path(name)) != staged_identity or _file_identity(cache) != cache_identity):
                raise CacheStorageError('cache_identity_changed')
            try:
                if request.overwrite:
                    os.replace(name, path)
                else:
                    _publish_new_owned(name, path)
            except OSError as exc:
                if legacy_errors and _error(exc).code == 'file_error':
                    raise CacheStorageError('publication_failed') from None
                raise
            name = None
            try:
                os.fsync(directory_fd)
            except OSError:
                # Restore the reviewed target if durable publication failed.
                # Never unlink an inode installed by another participant.
                if _file_identity(path) == staged_identity:
                    if backup is not None:
                        _verify_owned(Path(backup), backup_identity)
                        os.chmod(backup, target_mode, follow_symlinks=False)
                        os.replace(backup, path)
                        backup = None
                    else:
                        path.unlink()
                    os.fsync(directory_fd)
                raise
        finally:
            os.close(directory_fd)
        return ExportReceipt(count, format_version)
    except Exception as exc:
        raise _error(exc) from None
    finally:
        if name is not None:
            _unlink_owned(Path(name), staged_identity)
        if backup is not None:
            _unlink_owned(Path(backup), backup_identity)


def _export_legacy(self, request):
    return _export(self, request, legacy_errors=True)


def _write_legacy(stream, conn, request):
    count = total = 0
    ceiling = min(request.limits.serialized_export, legacy_storage.MAX_FILE_BYTES)
    def emit(data):
        nonlocal total
        total += len(data)
        if total > ceiling:
            raise CacheStorageError('file_too_large')
        stream.write(data)
    where, args = _filter(request.filters)
    emit(b'{\n  "version": 1,\n  "entries": [')
    if not _table_exists(conn, 'translations'):
        emit(b']\n}\n')
        return 0
    rows = conn.execute('SELECT ' + ','.join(_LEGACY_FIELDS) + ' FROM translations' + where + ' ORDER BY created_at,id', args) if _table_exists(conn, 'translations') else ()
    for row in rows:
        checkpoint(request, 'cache_between_entries')
        count += 1
        if count > min(legacy_storage.MAX_ENTRIES, request.limits.entries):
            raise CacheStorageError('too_many_entries')
        entry = dict(zip(_LEGACY_FIELDS, row))
        validate_payload(json.dumps({'version': 1, 'entries': [entry]}, ensure_ascii=False).encode('utf-8'))
        encoded = json.dumps(entry, ensure_ascii=False, indent=2).encode('utf-8')
        emit((b',' if count > 1 else b'') + b'\n    ' + encoded.replace(b'\n', b'\n    '))
        report(request, 'export', count, None)
    emit((b'\n  ' if count else b'') + b']\n}\n')
    return count


def _export_target_snapshot(request):
    path, cache = Path(request.destination.value), Path(request.cache.value)
    for table in _tables(request.namespace):
        _where(request.namespace, request.filters, table)
    _safe_file(path, cache)
    target_identity = _file_identity(path)
    target_mode = stat.S_IMODE(path.stat().st_mode) if target_identity is not None else None
    parent_identity = _file_identity(path.parent)
    cache_identity = _file_identity(cache)
    return path, cache, target_identity, target_mode, parent_identity, cache_identity


def _write_export_payload(stream, cache, request):
    if classify_cache_waiting(cache, timeout=legacy_storage.BUSY_TIMEOUT_SECONDS) is SchemaState.ABSENT:
        if request.namespace.kind in ('legacy-v1', 'all'):
            format_version = 1
            payload = b'{\n  \"version\": 1,\n  \"entries\": []\n}\n'
            if len(payload) > min(request.limits.serialized_export, legacy_storage.MAX_FILE_BYTES):
                raise CacheStorageError('file_too_large')
            stream.write(payload)
            count = 0
        else:
            format_version = 2
            sections = [({'kind': 'v2', 'scope_kind': _scope(request.namespace)[0], 'scope_id': _scope(request.namespace)[1]}, ())] if request.namespace.kind == 'v2' else []
            count = write_envelope(stream, sections, request.limits, request)
    else:
        with _administration_connection(cache, request.namespace) as conn:
            if not conn.in_transaction:
                conn.execute('BEGIN')
            legacy_format = request.namespace.kind == 'legacy-v1' or (request.namespace.kind == 'all' and _state(conn, integrity=False) is not SchemaState.V2)
            _validate_stored_sizes(conn, request)
            format_version = 1 if legacy_format else 2
            count = _write_legacy(stream, conn, request) if legacy_format else write_envelope(stream, _sections(conn, request), request.limits, request)
    if request.format_version is not None and request.format_version != format_version:
        raise CacheStorageError('unsupported_version')
    return format_version, count
