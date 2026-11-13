"""Side-effect-free v2 cache adapter for authorized offline reads.

Only stable rollback-journal databases are accepted. Existing protocol locks
exclude cooperative migrations. Without a lock, SQLite's read transaction pins
the rollback snapshot; inode/marker checks bracket opening and consumption.
As with schema.py, arbitrary non-cooperating replacement is outside the owned
directory protocol: Python SQLite cannot open a database descriptor-relatively.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .entries import CanonicalCacheRecord, CacheLookupState, CacheMissReason, ExactCacheLookup, ExactEntryKey
from .identity_values import CacheIdentityError, CacheValueKind
from .read_only_queries import candidate_records, exact_record, validate_resolution
from .scope import AuthorizedCacheScope
from .read_only_journal import journal_refusal
from .schema import (
    CacheSchemaError, SchemaState, _connect, _error, _identity, _location, _state,
    _supported, fcntl,
)

_REFUSED = ("-wal", "-shm", "-journal", ".migration", ".migration-next", ".v1-backup.sqlite.partial")


def _artifacts(path):
    return tuple(_identity(path.with_name(path.name + suffix), absent=True)
                 for suffix in (*_REFUSED, ".lock"))


def _validate(path, expected, artifacts):
    _location(path)
    if _identity(path, absent=True) != expected or _artifacts(path) != artifacts:
        raise CacheSchemaError("cache_identity_changed")
    if any(artifacts[:-1]):
        if expected is not None and artifacts[2] is not None and not any(artifacts[:2] + artifacts[3:-1]):
            raise CacheSchemaError(journal_refusal(path, expected))
        raise CacheSchemaError("cache_recovery_required")
    if expected is None:
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != expected:
            raise CacheSchemaError("cache_identity_changed")
        header = os.read(descriptor, 100)
    finally:
        os.close(descriptor)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise CacheSchemaError("cache_corrupt")
    # SQLite header read/write versions of 2 mean WAL, even without sidecars.
    if header[18:20] != b"\x01\x01":
        raise CacheSchemaError("cache_recovery_required")


@contextmanager
def _existing_lock(path, expected):
    if expected is None:
        yield
        return
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != expected or _identity(lock) != expected:
            raise CacheSchemaError("cache_identity_changed")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CacheSchemaError("cache_busy") from None
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _read_connection(path, authorized_directory):
    try:
        path = _location(path, authorized_directory)
        if not _supported(path):
            raise CacheSchemaError("cache_platform_unsupported")
        expected, artifacts = _identity(path, absent=True), _artifacts(path)
        _validate(path, expected, artifacts)
        with _existing_lock(path, artifacts[-1]):
            _validate(path, expected, artifacts)
            if expected is None:
                yield None
                _validate(path, expected, artifacts)
                return
            connection = _connect(path)  # URI mode=ro, never immutable=1.
            try:
                _validate(path, expected, artifacts)
                connection.execute("BEGIN")
                # Classify inside the same snapshot as the bounded payload reads.
                if _state(connection) is not SchemaState.V2:
                    raise CacheSchemaError("cache_recovery_required")
                _validate(path, expected, artifacts)
                yield connection
                _validate(path, expected, artifacts)
            finally:
                try:
                    connection.rollback()
                finally:
                    connection.close()
    except (OSError, sqlite3.Error, CacheSchemaError) as exc:
        raise _error(exc) from None


class ReadOnlyExactCache:
    """Lazy reader of an explicit v2 path; missing files are empty, never prepared.

    Scope authority comes exclusively from AuthorizedCacheScope, as in the
    writer. Every operation opens and closes its own short read transaction.
    """

    def __init__(self, path: Path, *, authorized_directory: Path | None = None) -> None:
        self._path = Path(path)
        self._authorized_directory = authorized_directory

    def __repr__(self) -> str:
        return "ReadOnlyExactCache(<redacted>)"

    def lookup(self, key: ExactEntryKey) -> ExactCacheLookup:
        if type(key) is not ExactEntryKey:
            raise CacheIdentityError("invalid_exact_entry_key")
        with _read_connection(self._path, self._authorized_directory) as connection:
            record = None if connection is None else exact_record(connection, key)
        if record is None:
            return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)
        return ExactCacheLookup(CacheLookupState.HIT, value=record.value)

    def canonical_records(self, scope: AuthorizedCacheScope, value_kind: CacheValueKind,
                          source_digest: str, *, limit: int) -> tuple[CanonicalCacheRecord, ...]:
        """Return validated aggregate records; overflow is a typed failure."""
        validate_resolution(scope, value_kind, source_digest, limit)
        with _read_connection(self._path, self._authorized_directory) as connection:
            return () if connection is None else candidate_records(
                connection, scope, value_kind, source_digest, limit)
