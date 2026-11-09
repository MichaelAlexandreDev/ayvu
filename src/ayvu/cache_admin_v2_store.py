"""Administration of the semantic cache through the migration lock."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from threading import Lock
from pathlib import Path
from datetime import UTC, datetime

from .cache_admin import CacheSummary, CachePair, CleanSelection, CacheNamespaceCount, CacheNamespace, CacheFilter, ExportReceipt, checkpoint, report
from .cache_admin_store import CacheStorageError, _error, _filter, _final_mode, _new_file_mode, _NO_HARD_LINKS
from .cache_schema import V2_COLUMNS, participating_connection, _SUFFIXES, classify_cache_waiting, SchemaState, _location, _identity, participating_admin_connection, _state
from .cache_interchange import write_envelope
from .cache_admin import validate_payload
from .cache_admin_store import MAX_FILE_BYTES, MAX_ENTRIES
from . import cache_admin_store as legacy_storage
from .cache_identity import CACHE_ENTRY_V2_LIMITS


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


def _safe_file(path, cache, *, allow_owned=False):
    if os.name != 'posix' or not getattr(os, 'O_NOFOLLOW', 0):
        raise CacheStorageError('cache_platform_unsupported')
    path, cache = path.absolute(), cache.absolute()
    if not allow_owned and path.name.startswith(('.ayvu-cache-import-', '.ayvu-cache-export-')):
        raise CacheStorageError('unsafe_cache_path')
    for parent in (path.parent, *path.parent.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise CacheStorageError('unsafe_cache_path')
    protected = [cache, *(cache.with_name(cache.name + suffix) for suffix in _SUFFIXES)]
    protected += [cache.with_name(cache.name + suffix + sidecar) for suffix in ('.v1-backup.sqlite', '.v1-backup.sqlite.partial') for sidecar in ('-wal', '-shm', '-journal')]
    if path in protected or path.resolve() in [p.resolve() for p in protected]:
        raise CacheStorageError('destination_is_cache')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CacheStorageError('unsafe_cache_path')
    for other in protected:
        if other.exists() and os.path.samefile(path, other):
            raise CacheStorageError('destination_is_cache')


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
        path, cache = Path(request.destination.value), Path(request.cache.value)
        for table in _tables(request.namespace):
            _where(request.namespace, request.filters, table)
        _safe_file(path, cache)
        target_identity = _file_identity(path)
        target_mode = stat.S_IMODE(path.stat().st_mode) if target_identity is not None else None
        parent_identity = _file_identity(path.parent)
        cache_identity = _file_identity(cache)
        fd, name = tempfile.mkstemp(prefix='.ayvu-cache-export-', dir=path.parent)
        staged_info = os.fstat(fd)
        staged_identity = (staged_info.st_dev, staged_info.st_ino)
        with os.fdopen(fd, 'wb') as stream:
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


def _publication_backup(path, expected, request):
    """Restricted streaming rollback image; no hard-link capability is needed."""
    descriptor, name = tempfile.mkstemp(prefix='.ayvu-cache-export-rollback-', dir=path.parent)
    info = os.fstat(descriptor)
    backup_identity = info.st_dev, info.st_ino
    try:
        source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(source_fd, 'rb') as source, os.fdopen(descriptor, 'wb') as target:
            info = os.fstat(source.fileno())
            if (info.st_dev, info.st_ino) != expected or info.st_nlink != 1:
                raise CacheStorageError('cache_identity_changed')
            total = 0
            while chunk := source.read(65536):
                checkpoint(request, 'cache_between_entries')
                total += len(chunk)
                if total > request.limits.serialized_export:
                    raise CacheStorageError('file_too_large')
                target.write(chunk)
            after = os.fstat(source.fileno())
            if ((after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns) or
                    _file_identity(path) != expected):
                raise CacheStorageError('cache_identity_changed')
            target.flush()
            # The rollback image stays restricted until restoration is needed.
            os.fsync(target.fileno())
        _verify_owned(Path(name), backup_identity)
        return name, backup_identity
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _unlink_owned(Path(name), backup_identity)
        raise


def _publish_new_owned(staged, path):
    try:
        os.link(staged, path)
        os.unlink(staged)
        return
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 stat.S_IMODE(os.stat(staged).st_mode))
    info = os.fstat(fd)
    placeholder_identity = info.st_dev, info.st_ino
    os.close(fd)
    try:
        _verify_owned(path, placeholder_identity)
        os.replace(staged, path)
    except BaseException:
        _unlink_owned(path, placeholder_identity)
        raise


def _verify_owned(path, identity):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (info.st_dev, info.st_ino) != identity):
        raise CacheStorageError('cache_identity_changed')


def _unlink_owned(path, identity):
    try:
        _verify_owned(path, identity)
    except (FileNotFoundError, CacheStorageError):
        return
    path.unlink()


SQLiteCacheV2Store.export_interchange = _export


def _export_legacy(self, request):
    return _export(self, request, legacy_errors=True)


SQLiteCacheV2Store.export_legacy = _export_legacy

# The preview database is a private, bounded staging and audit artifact beside
# the cache. A completed import retains it as an audit record; it is not part
# of the cache schema and cannot be mistaken for the cache itself.
import sqlite3
from dataclasses import asdict
from .cache_admin import FileRef, ImportPreview, ImportCounts, namespace_reference
from .cache_interchange import iter_records, validate_v2_entry

_PREVIEW_KEY = secrets.token_bytes(32)
_PREVIEWS = {}
_PREVIEW_LOCK = Lock()
_LEGACY_FIELDS = ('source_lang', 'target_lang', 'original_text_hash', 'original_text', 'translated_text', 'created_at')


def _file_digest(path, limit, request=None):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise CacheStorageError('unsafe_cache_path' if info.st_size <= limit else 'file_too_large')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise CacheStorageError('cache_identity_changed')
        digest = hashlib.sha256(f'{info.st_dev}:{info.st_ino}:'.encode('ascii'))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            total = 0
            while chunk := stream.read(65536):
                total += len(chunk)
                if total > limit:
                    raise CacheStorageError('file_too_large')
                if request is not None:
                    checkpoint(request, 'cache_between_entries')
                digest.update(chunk)
        end = os.fstat(fd)
        if (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) or _file_identity(path) != (info.st_dev, info.st_ino):
            raise CacheStorageError('cache_identity_changed')
        return digest.hexdigest()
    finally:
        os.close(fd)


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


def _map(scope_map, limits):
    if scope_map is None or len(scope_map.entries) > limits.scope_map_entries:
        raise CacheStorageError('invalid_scope_map')
    result = {}
    for item in scope_map.entries:
        src, dst = namespace_reference(item.source), namespace_reference(item.destination)
        for value in (*src, *dst):
            if len(value.encode('utf-8')) > limits.scope_reference:
                raise CacheStorageError('scope_reference_limit')
        result[src] = dst
    encoded = json.dumps(sorted((list(src), list(dst)) for src, dst in result.items()), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > limits.scope_map_bytes:
        raise CacheStorageError('scope_map_limit')
    return result


def _policy(preview):
    mapping = [(_authority(x.source), _authority(x.destination)) for x in preview.scope_map.entries]
    return json.dumps([preview.cache.value, preview.source.value, preview.stage.value, mapping,
                       preview.replace, asdict(preview.limits), preview.count, preview.imported,
                       preview.skipped, preview.replaced, preview.conflicts, preview.duplicates],
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _revision(preview, source_digest, stage_digest, db_digest):
    h = hmac.new(_PREVIEW_KEY, _policy(preview), hashlib.sha256)
    for value in (source_digest, stage_digest, db_digest):
        h.update(value.encode('ascii'))
    return h.hexdigest()


def _remap(descriptor, entry, destination):
    if descriptor['kind'] == 'legacy-v1':
        return tuple(entry[k] for k in _LEGACY_FIELDS)
    row = list(validate_v2_entry(descriptor, entry))
    row[:2] = destination[1:]
    # Match cache_repository._entry_digest's length-prefixed key and payload.
    parts = (*row[:5], row[6], row[7], row[5], row[8], row[9], row[10], row[11])
    h = hashlib.sha256()
    for value in parts:
        data = value if isinstance(value, bytes) else value.encode('utf-8')
        h.update(len(data).to_bytes(8, 'big'))
        h.update(data)
    row[12] = 'sha256:' + h.hexdigest()
    return tuple(row)


def _row_json(row):
    return json.dumps([{'blob': v.hex()} if isinstance(v, bytes) else v for v in row], ensure_ascii=True, separators=(',', ':'))


def _read_row(value):
    return tuple(bytes.fromhex(v['blob']) if isinstance(v, dict) and set(v) == {'blob'} else v for v in json.loads(value))


def _verified_records(stream, request, expected_digest):
    """Bind the bytes actually parsed, even if an in-place edit is restored."""
    info = os.fstat(stream.fileno())
    digest = hashlib.sha256(f'{info.st_dev}:{info.st_ino}:'.encode('ascii'))
    class HashedInput:
        def read(self, size):
            data = stream.read(size)
            digest.update(data)
            return data
        def tell(self):
            return stream.tell()
    yield from iter_records(HashedInput(), request.limits, request)
    if digest.hexdigest() != expected_digest:
        raise CacheStorageError('stale_preview')


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


def _preview_import(self, request):
    stage_name = None
    stage_identity = None
    try:
        checkpoint(request, 'cache_before_read')
        cache, source = Path(request.cache.value), Path(request.source.value)
        mapping = _map(request.scope_map, request.limits)
        _safe_file(source, cache)
        with _import_connection(cache, mapping) as conn:
            if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist'):
                raise CacheStorageError('unsupported_audit_mode')
        source_digest = _file_digest(source, request.limits.raw_input, request)
        # A closed map must cover the whole verified envelope before creating
        # even the private stage. This pass retains only bounded descriptors.
        preflight_scopes = set()
        source_identity = _file_identity(source)
        preflight_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(preflight_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != source_identity:
                raise CacheStorageError('cache_identity_changed')
            for descriptor, entry in _verified_records(stream, request, source_digest):
                if entry is None:
                    reference = (descriptor['kind'],) if descriptor['kind'] == 'legacy-v1' else ('v2', descriptor['scope_kind'], descriptor['scope_id'])
                    if reference not in mapping:
                        raise CacheStorageError('unmapped_scope')
                    preflight_scopes.add(reference)
        if preflight_scopes != set(mapping):
            raise CacheStorageError('unmapped_scope')
        if _file_digest(source, request.limits.raw_input, request) != source_digest:
            raise CacheStorageError('stale_preview')
        fd, stage_name = tempfile.mkstemp(prefix='.ayvu-cache-import-', suffix='.sqlite', dir=cache.parent)
        info = os.fstat(fd)
        stage_identity = info.st_dev, info.st_ino
        os.close(fd)
        _verify_owned(Path(stage_name), stage_identity)
        stage = sqlite3.connect(Path(stage_name).absolute().as_uri() + '?mode=rw', uri=True)
        try:
            _verify_owned(Path(stage_name), stage_identity)
            max_bytes = request.limits.raw_input * 4 + 1024 * 1024
            page_size = stage.execute('PRAGMA page_size').fetchone()[0]
            stage.execute(f'PRAGMA max_page_count={max(1, max_bytes // page_size)}')
            stage.execute('CREATE TABLE rows (kind TEXT NOT NULL, key TEXT NOT NULL, row TEXT NOT NULL, PRIMARY KEY(kind,key)) WITHOUT ROWID')
            count = duplicates = 0
            seen = set()
            source_identity = _file_identity(source)
            source_fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            source_stat = os.fstat(source_fd)
            if (source_stat.st_dev, source_stat.st_ino) != source_identity:
                os.close(source_fd)
                raise CacheStorageError('cache_identity_changed')
            with os.fdopen(source_fd, 'rb') as stream:
                for descriptor, entry in _verified_records(stream, request, source_digest):
                    _verify_owned(Path(stage_name), stage_identity)
                    source_key = (descriptor['kind'],) if descriptor['kind'] == 'legacy-v1' else ('v2', descriptor['scope_kind'], descriptor['scope_id'])
                    if entry is None:
                        seen.add(source_key)
                    if source_key not in mapping:
                        raise CacheStorageError('unmapped_scope')
                    if entry is None:
                        continue
                    count += 1
                    row = _remap(descriptor, entry, mapping[source_key])
                    kind = descriptor['kind']
                    key = json.dumps(row[:5] if kind == 'v2' else row[:3], ensure_ascii=True, separators=(',', ':'))
                    encoded = _row_json(row)
                    existing = stage.execute('SELECT row FROM rows WHERE kind=? AND key=?', (kind, key)).fetchone()
                    if existing is not None:
                        if existing[0] != encoded:
                            raise CacheStorageError('duplicate_conflict')
                        duplicates += 1
                    else:
                        stage.execute('INSERT INTO rows VALUES (?,?,?)', (kind, key, encoded))
            if seen != set(mapping):
                raise CacheStorageError('unmapped_scope')
            if _file_digest(source, request.limits.raw_input, request) != source_digest:
                raise CacheStorageError('stale_preview')
            imported = replaced = conflicts = 0
            skipped = duplicates
            with _import_connection(cache, mapping) as conn:
                conn.execute('BEGIN')
                db_digest = _db_revision(conn, cache, request)
                for kind, value in stage.execute('SELECT kind,row FROM rows ORDER BY kind,key'):
                    checkpoint(request, 'cache_between_entries')
                    row = _read_row(value)
                    old = _lookup(conn, kind, row)
                    if old is None:
                        imported += 1
                    elif old == row:
                        skipped += 1
                    else:
                        conflicts += 1
                        if request.replace:
                            replaced += 1
                        else:
                            skipped += 1
            _verify_owned(Path(stage_name), stage_identity)
            stage.execute('CREATE TABLE audit (event TEXT NOT NULL, count INTEGER NOT NULL, policy TEXT NOT NULL)')
            stage.commit()
        finally:
            stage.close()
        preview = ImportPreview(request.cache, request.source, request.scope_map, request.replace, '0' * 64,
                                count, imported, skipped, replaced, conflicts, duplicates, FileRef(stage_name), request.limits)
        stage_digest = _file_digest(Path(stage_name), request.limits.raw_input * 4 + 1024 * 1024, request)
        from dataclasses import replace
        preview = replace(preview, revision=_revision(preview, source_digest, stage_digest, db_digest))
        with _PREVIEW_LOCK:
            _PREVIEWS[preview.revision] = (preview.stage.value, _file_identity(Path(stage_name)), _policy(preview))
        stage_name = None
        return preview
    except Exception as exc:
        raise _error(exc) from None
    finally:
        if stage_name is not None:
            _unlink_owned(Path(stage_name), stage_identity)


def _execute_import(self, request):
    owned_stage = None
    completed = False
    try:
        preview = request.preview
        cache, source, stage_path = Path(preview.cache.value), Path(preview.source.value), Path(preview.stage.value)
        with _PREVIEW_LOCK:
            registered = _PREVIEWS.get(preview.revision)
            if registered is None or registered[0] != preview.stage.value:
                raise CacheStorageError('invalid_preview')
            if registered[2] != _policy(preview):
                raise CacheStorageError('stale_preview')
            del _PREVIEWS[preview.revision]
        owned_stage = (stage_path, registered[1])
        checkpoint(request, 'cache_before_mutation')
        mapping = _map(preview.scope_map, preview.limits)
        _safe_file(source, cache)
        _safe_file(stage_path, cache, allow_owned=True)
        if any(stage_path.with_name(stage_path.name + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise CacheStorageError('unsafe_cache_path')
        if stage_path.parent.absolute() != cache.parent.absolute() or not stage_path.name.startswith('.ayvu-cache-import-'):
            raise CacheStorageError('invalid_preview')
        with _import_connection(cache, mapping) as conn:
            if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist'):
                raise CacheStorageError('unsupported_audit_mode')
            if _file_identity(stage_path) != registered[1]:
                raise CacheStorageError('stale_preview')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('ATTACH DATABASE ? AS import_audit', (str(stage_path),))
            conn.execute('PRAGMA import_audit.synchronous=FULL')
            if conn.execute('PRAGMA import_audit.journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist'):
                raise CacheStorageError('unsupported_audit_mode')
            stage_page_size = conn.execute('PRAGMA import_audit.page_size').fetchone()[0]
            max_pages = max(1, (preview.limits.raw_input * 4 + 1024 * 1024) // stage_page_size)
            conn.execute(f'PRAGMA import_audit.max_page_count={max_pages}')
            checkpoint(request, 'cache_before_mutation')
            conn.execute('BEGIN IMMEDIATE')
            cache_identity = _file_identity(cache)
            source_digest = _file_digest(source, preview.limits.raw_input, request)
            stage_digest = _file_digest(stage_path, preview.limits.raw_input * 4 + 1024 * 1024, request)
            db_digest = _db_revision(conn, cache, request)
            if _revision(preview, source_digest, stage_digest, db_digest) != preview.revision:
                raise CacheStorageError('stale_preview')
            if preview.conflicts and not preview.replace:
                raise CacheStorageError('import_conflict')
            try:
                imported = replaced = 0
                skipped = preview.duplicates
                for kind, value in conn.execute('SELECT kind,row FROM import_audit.rows ORDER BY kind,key'):
                    checkpoint(request, 'cache_between_entries')
                    row = _read_row(value)
                    old = _lookup(conn, kind, row)
                    if old == row or (old is not None and not preview.replace):
                        skipped += 1
                    elif kind == 'v2':
                        conn.execute('INSERT OR REPLACE INTO cache_entries_v2 (' + ','.join(V2_COLUMNS) + ') VALUES (' + ','.join('?' for _ in V2_COLUMNS) + ')', row)
                        if old is None: imported += 1
                        else: replaced += 1
                    else:
                        if old is None:
                            conn.execute('INSERT INTO translations (' + ','.join(_LEGACY_FIELDS) + ') VALUES (?,?,?,?,?,?)', row)
                            imported += 1
                        else:
                            conn.execute('UPDATE translations SET original_text=?,translated_text=?,created_at=? WHERE source_lang=? AND target_lang=? AND original_text_hash=?', (*row[3:], *row[:3]))
                            replaced += 1
                if (imported, skipped, replaced) != (preview.imported, preview.skipped, preview.replaced):
                    raise CacheStorageError('stale_preview')
                checkpoint(request, 'cache_before_commit')
                _integrity(conn, 'main')
                checkpoint(request, 'cache_before_commit')
                _location(cache)
                if (_identity(cache) != cache_identity or
                        _file_digest(source, preview.limits.raw_input, request) != source_digest or
                        _file_digest(stage_path, preview.limits.raw_input * 4 + 1024 * 1024, request) != stage_digest):
                    raise CacheStorageError('stale_preview')
                conn.execute('INSERT INTO import_audit.audit VALUES (?,?,?)', ('committed', imported + skipped + replaced, _policy(preview).decode('utf-8')))
                _integrity(conn, 'import_audit')
                checkpoint(request, 'cache_before_commit')
                _location(cache)
                if (_identity(cache) != cache_identity or _file_identity(stage_path) != registered[1] or
                        _file_digest(source, preview.limits.raw_input, request) != source_digest):
                    raise CacheStorageError('stale_preview')
                conn.commit()
            finally:
                pass
        # A retained preview is a bounded audit artifact; no cache schema changes.
        with _PREVIEW_LOCK:
            _PREVIEWS.pop(preview.revision, None)
        completed = True
        return ImportCounts(imported, skipped, replaced)
    except Exception as exc:
        raise _error(exc) from None
    finally:
        if not completed and owned_stage is not None:
            with _PREVIEW_LOCK:
                _PREVIEWS.pop(request.preview.revision, None)
            stage_path, identity = owned_stage
            try:
                if _file_identity(stage_path) == identity and stat.S_ISREG(stage_path.lstat().st_mode) and stage_path.lstat().st_nlink == 1:
                    stage_path.unlink()
            except OSError:
                pass


SQLiteCacheV2Store.preview_import = _preview_import
SQLiteCacheV2Store.execute_import = _execute_import


def _file_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


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


def _empty_selection(cache, namespace, filters):
    return hashlib.sha256(repr(('absent', str(Path(cache.value).absolute()), _authority(namespace), filters.source_lang, filters.target_lang, filters.before, filters.all_entries)).encode('utf-8')).hexdigest()


def _discard_import(self, preview):
    with _PREVIEW_LOCK:
        registered = _PREVIEWS.get(preview.revision)
        if registered is None or registered[0] != preview.stage.value or registered[2] != _policy(preview):
            return 0
        path = Path(preview.stage.value)
        try:
            info = path.lstat()
        except FileNotFoundError:
            _PREVIEWS.pop(preview.revision, None)
            return 0
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != registered[1]:
            raise CacheStorageError('cache_identity_changed')
        path.unlink()
        _PREVIEWS.pop(preview.revision, None)
        return 1


SQLiteCacheV2Store.discard_import = _discard_import


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
