"""SQLite and local-file adapters for cache administration."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import tempfile
from pathlib import Path
from contextlib import closing
from urllib.parse import quote

from .cache import SCHEMA
from .cache_admin import (CacheAdminError, CacheSummary, CachePair, CleanSelection, ImportCounts, checkpoint, report, validate_payload)

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 1_000_000
MAX_TEXT_LENGTH = 1_000_000
MAX_METADATA_LENGTH = 256
# The same patience as TranslationCache: a running translation holds the lock for each commit.
BUSY_TIMEOUT_SECONDS = 30.0
# What a filesystem without hard links answers, such as a FAT or exFAT pen drive.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK})


class CacheStorageError(CacheAdminError):
    """Only a bounded reason code crosses the infrastructure boundary."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _error(exc: Exception) -> CacheStorageError:
    if isinstance(exc, CacheAdminError):
        return exc
    if isinstance(exc, FileNotFoundError):
        return CacheStorageError('not_found')
    if isinstance(exc, FileExistsError):
        return CacheStorageError('already_exists')
    if isinstance(exc, PermissionError):
        return CacheStorageError('permission_denied')
    if isinstance(exc, OSError):
        return CacheStorageError('disk_full' if exc.errno == errno.ENOSPC else 'file_error')
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return CacheStorageError('cache_busy')
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_SCHEMA, sqlite3.SQLITE_ERROR):
            return CacheStorageError('corrupt_cache')
        if code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH):
            return CacheStorageError('permission_denied')
        if code == sqlite3.SQLITE_FULL:
            return CacheStorageError('disk_full')
        return CacheStorageError('cache_error')
    return CacheStorageError('internal_error')


def _path(ref):
    return Path(ref.value)


def _filter(filters):
    clauses, args = [], []
    for name, column, op in [('source_lang', 'source_lang', '='), ('target_lang', 'target_lang', '='), ('before', 'created_at', '<')]:
        value = getattr(filters, name, None)
        if value is not None:
            clauses.append(f'{column} {op} ?')
            args.append(value)
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), tuple(args)


def _connect(ref, write=False):
    # mode=ro/rw forbids SQLite from creating an absent source.
    uri = 'file:' + quote(str(_path(ref).absolute()), safe='/') + ('?mode=rw' if write else '?mode=ro')
    return sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)


def _has_entries_table(conn):
    # An empty file, or one that a failed first import left behind, is a cache with nothing in it. Garbage still raises.
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'translations'").fetchone() is not None


def _checkpoint(callback):
    if callback is not None:
        callback()


def _selection(conn, filters, check):
    where, args = _filter(filters)
    digest = hashlib.sha256()
    count = 0
    # Rows are read one at a time, so the interchange limits do not apply, and cleaning is the way out of an oversized row.
    cursor = conn.execute('SELECT id, source_lang, target_lang, original_text_hash, original_text, translated_text, created_at FROM translations' + where + ' ORDER BY id', args)
    for row in cursor:
        _checkpoint(check)
        # Length-prefix serialized rows so neither separator ambiguity nor edits go unnoticed.
        data = json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
        count += 1
    return count, digest.hexdigest()


class SQLiteCacheStore:
    def inspect(self, request):
        ref, filters = request.cache, request.filters
        try:
            checkpoint(request, "cache_before_read")
            report(request, "inspect", 0, 1)
            if not _path(ref).exists():
                report(request, "inspect", 1, 1)
                return CacheSummary(False, 0, 0, ())
            where, args = _filter(filters)
            with closing(_connect(ref)) as conn:
                if not _has_entries_table(conn):
                    report(request, "inspect", 1, 1)
                    return CacheSummary(True, 0, _path(ref).stat().st_size, ())
                oversized = conn.execute('SELECT 1 FROM translations' + where + (' AND ' if where else ' WHERE ') + '(length(CAST(source_lang AS BLOB)) > ? OR length(CAST(target_lang AS BLOB)) > ? OR length(CAST(created_at AS BLOB)) > ?) LIMIT 1', (*args, *([MAX_METADATA_LENGTH * 4] * 3))).fetchone()
                if oversized:
                    raise CacheStorageError('metadata_limit')
                rows = conn.execute('SELECT source_lang, target_lang, COUNT(*), MIN(created_at), MAX(created_at) FROM translations' + where + ' GROUP BY source_lang, target_lang ORDER BY source_lang, target_lang LIMIT 1001', args).fetchall()
            if len(rows) > 1000 or any(any(not isinstance(v, str) or len(v) > MAX_METADATA_LENGTH for v in (r[0], r[1], r[3], r[4])) for r in rows):
                raise CacheStorageError('metadata_limit')
            report(request, "inspect", 1, 1)
            return CacheSummary(True, sum(row[2] for row in rows), _path(ref).stat().st_size, tuple(CachePair(*row) for row in rows))
        except Exception as exc:
            raise _error(exc) from None

    def preview_clean(self, request):
        ref, filters = request.cache, request.filters
        check = lambda: checkpoint(request, "cache_between_entries")
        try:
            checkpoint(request, "cache_before_read")
            report(request, "preview", 0, 1)
            if not _path(ref).exists():
                report(request, "preview", 1, 1)
                return CleanSelection(ref, filters, 0, hashlib.sha256().hexdigest())
            with closing(_connect(ref)) as conn:
                count, digest = _selection(conn, filters, check) if _has_entries_table(conn) else (0, hashlib.sha256().hexdigest())
                report(request, "preview", 1, 1)
                return CleanSelection(ref, filters, count, digest)
        except Exception as exc:
            raise _error(exc) from None

    def execute_clean(self, request):
        ref, filters, count, digest = request.selection.cache, request.selection.filters, request.selection.count, request.selection.digest
        check = lambda: checkpoint(request, "cache_between_entries")
        try:
            checkpoint(request, "cache_before_mutation")
            report(request, "clean", 0, count)
            if not _path(ref).exists():
                if count == 0 and digest == hashlib.sha256().hexdigest():
                    return 0
                raise CacheStorageError('stale_preview')
            conn = _connect(ref, write=True)
            try:
                conn.execute('BEGIN IMMEDIATE')
                if not _has_entries_table(conn):
                    if (count, digest) != (0, hashlib.sha256().hexdigest()):
                        raise CacheStorageError('stale_preview')
                    conn.rollback()
                    return 0
                if _selection(conn, filters, check) != (count, digest):
                    raise CacheStorageError('stale_preview')
                checkpoint(request, "cache_before_mutation")
                where, args = _filter(filters)
                deleted = conn.execute('DELETE FROM translations' + where, args).rowcount
                checkpoint(request, "cache_before_commit")
                report(request, "clean", deleted, count)
                conn.commit()
                return deleted
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as exc:
            raise _error(exc) from None

    def export_payload(self, request):
        ref, filters = request.cache, request.filters
        try:
            if not _path(ref).exists():
                return b'{\n  \"version\": 1,\n  \"entries\": []\n}\n'
            where, args = _filter(filters)
            entries = []
            byte_count = 0
            with closing(_connect(ref)) as conn:
                if not _has_entries_table(conn):
                    return b'{\n  \"version\": 1,\n  \"entries\": []\n}\n'
                oversized = conn.execute('SELECT 1 FROM translations' + where + (' AND ' if where else ' WHERE ') + '(length(CAST(original_text AS BLOB)) > ? OR length(CAST(translated_text AS BLOB)) > ? OR length(CAST(source_lang AS BLOB)) > ? OR length(CAST(target_lang AS BLOB)) > ? OR length(CAST(original_text_hash AS BLOB)) > ? OR length(CAST(created_at AS BLOB)) > ?) LIMIT 1', (*args, MAX_TEXT_LENGTH * 4, MAX_TEXT_LENGTH * 4, *([MAX_METADATA_LENGTH * 4] * 4))).fetchone()
                if oversized:
                    raise CacheStorageError('limit_exceeded')
                for row in conn.execute('SELECT source_lang, target_lang, original_text_hash, original_text, translated_text, created_at FROM translations' + where + ' ORDER BY created_at, id', args):
                    checkpoint(request, "cache_between_entries")
                    if len(entries) >= MAX_ENTRIES or any(not isinstance(v, str) or len(v) > (MAX_TEXT_LENGTH if i in (3, 4) else MAX_METADATA_LENGTH) for i, v in enumerate(row)):
                        raise CacheStorageError('limit_exceeded')
                    item = dict(zip(('source_lang', 'target_lang', 'original_text_hash', 'original_text', 'translated_text', 'created_at'), row))
                    byte_count += len(json.dumps(item, ensure_ascii=False).encode('utf-8')) + 32
                    if byte_count > MAX_FILE_BYTES:
                        raise CacheStorageError('limit_exceeded')
                    entries.append(item)
                    report(request, 'export', len(entries), None)
            payload = (json.dumps({'version': 1, 'entries': entries}, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            if len(payload) > MAX_FILE_BYTES:
                raise CacheStorageError('limit_exceeded')
            return payload
        except Exception as exc:
            raise _error(exc) from None

    def import_payload(self, request, payload):
        ref, replace = request.cache, request.replace
        try:
            validate_payload(payload)
            entries = json.loads(payload)["entries"]
            path = _path(ref)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
            imported = skipped = replaced = 0
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(SCHEMA)
                for entry in entries:
                    report(request, "import", imported + skipped + replaced, len(entries))
                    checkpoint(request, "cache_between_entries")
                    key = (entry['source_lang'], entry['target_lang'], entry['original_text_hash'])
                    exists = conn.execute('SELECT 1 FROM translations WHERE source_lang=? AND target_lang=? AND original_text_hash=?', key).fetchone()
                    if exists and not replace:
                        skipped += 1
                    elif exists:
                        conn.execute('UPDATE translations SET original_text=?, translated_text=?, created_at=? WHERE source_lang=? AND target_lang=? AND original_text_hash=?', (entry['original_text'], entry['translated_text'], entry['created_at'], *key))
                        replaced += 1
                    else:
                        conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', (*key, entry['original_text'], entry['translated_text'], entry['created_at']))
                        imported += 1
                checkpoint(request, "cache_before_commit")
                conn.commit()
                return ImportCounts(imported, skipped, replaced)
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as exc:
            raise _error(exc) from None


def _new_file_mode(directory):
    """What an ordinary new file gets here. Asked of the system, because reading the umask means changing it for a moment."""
    probe = Path(directory) / f'.ayvu-cache-mode-{secrets.token_hex(16)}'
    descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o666)
    try:
        return stat.S_IMODE(os.fstat(descriptor).st_mode)
    finally:
        os.close(descriptor)
        os.unlink(probe)


def _final_mode(path):
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return _new_file_mode(path.parent)


def _publish_new(staged, path):
    try:
        os.link(staged, path)
        os.unlink(staged)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
    # An exclusive create takes the name, so this cannot overwrite anything. An empty file is visible for a moment.
    mode = stat.S_IMODE(os.stat(staged).st_mode)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode))
    os.replace(staged, path)


class LocalCacheFiles:
    def read(self, request):
        ref = request.source
        try:
            with _path(ref).open('rb') as stream:
                payload = stream.read(MAX_FILE_BYTES + 1)
            if len(payload) > MAX_FILE_BYTES:
                raise CacheStorageError('limit_exceeded')
            return payload
        except Exception as exc:
            raise _error(exc) from None

    def publish(self, request, payload):
        ref, overwrite = request.destination, request.overwrite
        name = None
        try:
            if len(payload) > MAX_FILE_BYTES:
                raise CacheStorageError('limit_exceeded')
            path = _path(ref)
            cache_path = _path(request.cache)
            if path.resolve() == cache_path.resolve() or (path.exists() and cache_path.exists() and os.path.samefile(path, cache_path)):
                raise CacheStorageError('destination_is_cache')
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.ayvu-cache-', dir=path.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), _final_mode(path) if overwrite else _new_file_mode(path.parent))
            checkpoint(request, "cache_before_publish")
            try:
                if overwrite:
                    os.replace(name, path)
                    name = None
                else:
                    _publish_new(name, path)
                    name = None
            except OSError as exc:
                failure = _error(exc)
                # Publication errors are distinct from errors creating or writing the temporary file.
                if failure.code == 'file_error':
                    raise CacheStorageError('publication_failed') from None
                raise failure from None
            return None
        except Exception as exc:
            raise _error(exc) from None
        finally:
            if name is not None:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass
