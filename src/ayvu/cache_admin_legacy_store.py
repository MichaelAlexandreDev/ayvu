"""Bounded, transactional streaming import for the original v1 interface."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote

from .cache import SCHEMA
from .cache_admin import ImportCounts, CacheAdminError, checkpoint, report, _invalid
from . import cache_admin_store as legacy
from .cache_admin_store import CacheStorageError, _error
from .cache_admin_v2_store import _safe_file, _file_digest, _file_identity
from .cache_schema import (_location, _identity, classify_cache_waiting,
                           participating_admin_connection, SchemaState)

# Legacy unknown objects require a disk-backed set of decoded key digests.
# Bound that index separately from the six-field rows without rejecting dense
# legacy objects merely because their keys expand in the staging index.
_STAGE_MULTIPLIER = 16

_FIELDS = ('source_lang', 'target_lang', 'original_text_hash', 'original_text',
           'translated_text', 'created_at')


def _verify_stage(path, expected):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (info.st_dev, info.st_ino) != expected):
        raise CacheStorageError('cache_identity_changed')


def _create_stage_directory(directory, owned):
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(item.lstat().st_mode):
                raise CacheStorageError('unsafe_cache_path') from None
        else:
            info = item.lstat()
            owned.append((item, (info.st_dev, info.st_ino)))


def _check_schema(path):
    state = classify_cache_waiting(path, timeout=legacy.BUSY_TIMEOUT_SECONDS)
    if state in (SchemaState.V2, SchemaState.PARTIAL):
        raise CacheStorageError('v2-admin-required')
    if state is SchemaState.CORRUPT:
        raise CacheStorageError('corrupt_cache')


class SQLiteLegacyStreamingStore:
    """Validate the complete file on disk before entering a cache transaction."""

    def import_cache(self, request):
        stage_path = None
        stage_identity = None
        owned_dirs = []
        try:
            from .cache_legacy_stream import iter_legacy_entries
            checkpoint(request, 'cache_before_read')
            raw_limit = min(legacy.MAX_FILE_BYTES, request.limits.raw_input)
            cache = _location(Path(request.cache.value))
            source = Path(request.source.value)
            _safe_file(source, cache)
            _check_schema(cache)
            cache_identity = _identity(cache, absent=True)
            try:
                source_digest = _file_digest(source, raw_limit, request)
            except CacheAdminError as exc:
                if exc.code == 'file_too_large':
                    raise CacheStorageError('limit_exceeded') from None
                raise
            _create_stage_directory(cache.parent, owned_dirs)
            # Recheck after creating the directory, before placing temporary data in it.
            _location(cache)
            if _identity(cache, absent=True) != cache_identity:
                raise CacheStorageError('cache_identity_changed')
            fd, name = tempfile.mkstemp(prefix='.ayvu-cache-import-legacy-', suffix='.sqlite', dir=cache.parent)
            stage_path = Path(name)
            info = os.fstat(fd)
            stage_identity = (info.st_dev, info.st_ino)
            os.close(fd)
            _verify_stage(stage_path, stage_identity)
            stage = sqlite3.connect('file:' + quote(str(stage_path), safe='/') + '?mode=rw', uri=True)
            try:
                _verify_stage(stage_path, stage_identity)
                page_size = stage.execute('PRAGMA page_size').fetchone()[0]
                ceiling = raw_limit * _STAGE_MULTIPLIER + 1024 * 1024
                stage.execute(f'PRAGMA max_page_count={max(1, ceiling // page_size)}')
                stage.execute('CREATE TABLE rows (seq INTEGER PRIMARY KEY, source_lang TEXT NOT NULL, target_lang TEXT NOT NULL, original_text_hash TEXT NOT NULL, original_text TEXT NOT NULL, translated_text TEXT NOT NULL, created_at TEXT NOT NULL)')
                stage.execute('CREATE TABLE json_keys (object_id INTEGER NOT NULL, digest BLOB NOT NULL, PRIMARY KEY(object_id,digest)) WITHOUT ROWID')
                def key_seen(object_id, digest):
                    _verify_stage(stage_path, stage_identity)
                    try:
                        stage.execute('INSERT INTO json_keys VALUES (?,?)', (object_id, bytes.fromhex(digest)))
                    except sqlite3.IntegrityError:
                        _invalid('malformed_json')
                def object_closed(object_id):
                    _verify_stage(stage_path, stage_identity)
                    stage.execute('DELETE FROM json_keys WHERE object_id=?', (object_id,))
                source_identity = _file_identity(source)
                source_fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                source_info = os.fstat(source_fd)
                if (source_info.st_dev, source_info.st_ino) != source_identity:
                    os.close(source_fd)
                    raise CacheStorageError('cache_identity_changed')
                count = 0
                with os.fdopen(source_fd, 'rb') as stream:
                    for entry in iter_legacy_entries(stream, key_seen, object_closed, request=request,
                                                     max_bytes=raw_limit,
                                                     max_entries=min(legacy.MAX_ENTRIES, request.limits.entries)):
                        checkpoint(request, 'cache_between_entries')
                        count += 1
                        _verify_stage(stage_path, stage_identity)
                        stage.execute('INSERT INTO rows VALUES (?,?,?,?,?,?,?)',
                                      (count, *(entry[name] for name in _FIELDS)))
                if _file_digest(source, raw_limit, request) != source_digest:
                    raise CacheStorageError('stale_preview')
                _verify_stage(stage_path, stage_identity)
                stage.commit()
            finally:
                stage.close()
            _verify_stage(stage_path, stage_identity)
            stage_digest = _file_digest(stage_path, raw_limit * _STAGE_MULTIPLIER + 1024 * 1024, request)
            checkpoint(request, 'cache_before_mutation')
            if (_file_digest(source, raw_limit) != source_digest or
                    _file_digest(stage_path, raw_limit * _STAGE_MULTIPLIER + 1024 * 1024) != stage_digest):
                raise CacheStorageError('stale_preview')
            _location(cache)
            _check_schema(cache)
            if _identity(cache, absent=True) != cache_identity:
                raise CacheStorageError('cache_identity_changed')
            with participating_admin_connection(cache, write=True, create=True,
                                                timeout=legacy.BUSY_TIMEOUT_SECONDS) as conn:
                current = _identity(cache)
                if cache_identity is not None and current != cache_identity:
                    raise CacheStorageError('cache_identity_changed')
                checkpoint(request, 'cache_before_mutation')
                conn.execute('BEGIN IMMEDIATE')
                try:
                    if (_file_digest(source, raw_limit, request) != source_digest or
                            _file_digest(stage_path, raw_limit * _STAGE_MULTIPLIER + 1024 * 1024, request) != stage_digest):
                        raise CacheStorageError('stale_preview')
                    conn.execute(SCHEMA)
                    _verify_stage(stage_path, stage_identity)
                    stage = sqlite3.connect('file:' + quote(str(stage_path), safe='/') + '?mode=ro', uri=True)
                    try:
                        _verify_stage(stage_path, stage_identity)
                        imported = skipped = replaced = 0
                        for row in stage.execute('SELECT ' + ','.join(_FIELDS) + ' FROM rows ORDER BY seq'):
                            checkpoint(request, 'cache_between_entries')
                            report(request, 'import', imported + skipped + replaced, count)
                            key = row[:3]
                            exists = conn.execute('SELECT 1 FROM translations WHERE source_lang=? AND target_lang=? AND original_text_hash=?', key).fetchone()
                            if exists is None:
                                conn.execute('INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text,created_at) VALUES (?,?,?,?,?,?)', row)
                                imported += 1
                            elif request.replace:
                                conn.execute('UPDATE translations SET original_text=?,translated_text=?,created_at=? WHERE source_lang=? AND target_lang=? AND original_text_hash=?', (*row[3:], *key))
                                replaced += 1
                            else:
                                skipped += 1
                        checkpoint(request, 'cache_before_commit')
                        if conn.execute('PRAGMA main.quick_check').fetchone() != ('ok',):
                            raise CacheStorageError('integrity_failure')
                        checkpoint(request, 'cache_before_commit')
                        _location(cache)
                        if (_identity(cache) != current or
                                _file_digest(source, raw_limit, request) != source_digest or
                                _file_digest(stage_path, raw_limit * _STAGE_MULTIPLIER + 1024 * 1024, request) != stage_digest):
                            raise CacheStorageError('stale_preview')
                        # The digest loop's cancellation callback may change paths.
                        _location(cache)
                        if (_file_digest(source, raw_limit) != source_digest or
                                _file_digest(stage_path, raw_limit * _STAGE_MULTIPLIER + 1024 * 1024) != stage_digest or
                                _identity(cache) != current):
                            raise CacheStorageError('stale_preview')
                        conn.commit()
                        return ImportCounts(imported, skipped, replaced)
                    finally:
                        stage.close()
                except BaseException:
                    conn.rollback()
                    raise
        except Exception as exc:
            raise _error(exc) from None
        finally:
            if stage_path is not None and stage_identity is not None:
                try:
                    _verify_stage(stage_path, stage_identity)
                    stage_path.unlink()
                    for suffix in ('-journal', '-wal', '-shm'):
                        sidecar = stage_path.with_name(stage_path.name + suffix)
                        try:
                            info = sidecar.lstat()
                            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                sidecar.unlink()
                        except FileNotFoundError:
                            pass
                except (FileNotFoundError, CacheAdminError, OSError):
                    pass
            for directory, identity in reversed(owned_dirs):
                try:
                    info = directory.lstat()
                    if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                        directory.rmdir()
                except OSError:
                    pass
