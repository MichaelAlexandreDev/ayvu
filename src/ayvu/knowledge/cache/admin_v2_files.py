"""Owned cache interchange files and durable publication."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .admin import checkpoint
from .admin_store import CacheStorageError, _NO_HARD_LINKS
from .schema import _SUFFIXES


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


def _file_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino
