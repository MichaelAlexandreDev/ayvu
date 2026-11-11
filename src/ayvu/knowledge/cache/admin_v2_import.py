"""Private staging, preview authority and atomic cache imports."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import json
import os
import stat
import tempfile
from threading import Lock
from pathlib import Path
import sqlite3
from dataclasses import asdict

from .admin import checkpoint
from .admin_store import CacheStorageError, _error
from .schema import V2_COLUMNS, _location, _identity
from .admin import FileRef, ImportPreview, ImportCounts
from .admin_v2_queries import _authority, _db_revision, _import_connection, _integrity, _lookup
from .admin_v2_files import _file_digest, _file_identity, _safe_file, _unlink_owned, _verify_owned
from .admin_v2_records import _LEGACY_FIELDS, _map, _read_row, _remap, _row_json, _verified_records


# The preview database is a private, bounded staging and audit artifact beside
# the cache. A completed import retains it as an audit record; it is not part
# of the cache schema and cannot be mistaken for the cache itself.


_PREVIEW_KEY = secrets.token_bytes(32)


_PREVIEWS = {}


_PREVIEW_LOCK = Lock()


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
        _verify_import_scopes(source, mapping, request, source_digest)
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
            count, duplicates = _stage_import_records(stage, stage_name, stage_identity, source, mapping, source_digest, request)
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
            _attach_import_audit(conn, stage_path, registered[1], preview)
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
                imported, skipped, replaced = _apply_import_rows(conn, preview, request)
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


def _verify_import_scopes(source, mapping, request, source_digest):
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


def _stage_import_records(stage, stage_name, stage_identity, source, mapping, source_digest, request):
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
    return count, duplicates


def _attach_import_audit(conn, stage_path, expected_identity, preview):
    if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist'):
        raise CacheStorageError('unsupported_audit_mode')
    if _file_identity(stage_path) != expected_identity:
        raise CacheStorageError('stale_preview')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('ATTACH DATABASE ? AS import_audit', (str(stage_path),))
    conn.execute('PRAGMA import_audit.synchronous=FULL')
    if conn.execute('PRAGMA import_audit.journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist'):
        raise CacheStorageError('unsupported_audit_mode')
    stage_page_size = conn.execute('PRAGMA import_audit.page_size').fetchone()[0]
    max_pages = max(1, (preview.limits.raw_input * 4 + 1024 * 1024) // stage_page_size)
    conn.execute(f'PRAGMA import_audit.max_page_count={max_pages}')


def _apply_import_rows(conn, preview, request):
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
    return imported, skipped, replaced
