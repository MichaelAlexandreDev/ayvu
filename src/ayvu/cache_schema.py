"""Standalone cache ledger, exclusive migration and bounded restart recovery.

This protocol owns only the cache directory. It deliberately does not use the
project migration runner. SQLite cannot be opened descriptor-relatively by the
Python API: identity checks and cooperative locks narrow, but do not eliminate,
the race against arbitrary processes replacing files in an owned directory.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised through the capability gate
    fcntl = None

from .cache import SCHEMA as LEGACY_SCHEMA

V2_SCHEMA = """CREATE TABLE cache_entries_v2 (
    scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, value_kind TEXT NOT NULL,
    semantic_fingerprint TEXT NOT NULL, source_digest TEXT NOT NULL,
    canonical_identity BLOB NOT NULL, source_text TEXT NOT NULL, value TEXT NOT NULL,
    metadata BLOB NOT NULL, provenance BLOB NOT NULL, created_at TEXT NOT NULL,
    provenance_digest TEXT NOT NULL, entry_digest TEXT NOT NULL,
    PRIMARY KEY (scope_kind, scope_id, value_kind, semantic_fingerprint, source_digest)
) WITHOUT ROWID"""
LEDGER_SCHEMA = """CREATE TABLE cache_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 2),
    legacy_namespace TEXT NOT NULL CHECK (legacy_namespace = 'legacy-v1')
)"""
V2_COLUMNS = (
    "scope_kind", "scope_id", "value_kind", "semantic_fingerprint", "source_digest",
    "canonical_identity", "source_text", "value", "metadata", "provenance", "created_at",
    "provenance_digest", "entry_digest",
)
COMPATIBLE_INDEX = """CREATE INDEX cache_entries_v2_compatible ON cache_entries_v2
    (scope_kind, scope_id, value_kind, source_digest, semantic_fingerprint)"""
PHASES = ("classified", "backup-verified", "migration-pending", "v2-built", "integrity-verified", "committed")
_SUFFIXES = ("-wal", "-shm", "-journal", ".lock", ".migration", ".migration-next", ".v1-backup.sqlite",
             ".v1-backup.sqlite.partial")
_V1_COLUMNS = ("id", "source_lang", "target_lang", "original_text_hash", "original_text", "translated_text", "created_at")


class SchemaState(StrEnum):
    ABSENT = "absent"
    EMPTY = "empty"
    V1 = "v1"
    V2 = "v2"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    FOREIGN = "foreign"


class CacheSchemaError(ValueError):
    def __init__(self, code: str):
        allowed = {"unsafe_cache_path", "cache_identity_changed", "cache_busy", "cache_platform_unsupported",
                   "cache_corrupt", "cache_foreign", "cache_recovery_required", "cache_backup_exists",
                   "cache_backup_invalid", "cache_io_error", "cache_disk_full", "cache_permission_denied",
                   "cache_invalid_transition", "v2-admin-required"}
        self.code = code if code in allowed else "cache_io_error"
        super().__init__(self.code)


def _error(exc: BaseException) -> CacheSchemaError:
    if isinstance(exc, CacheSchemaError):
        return exc
    if isinstance(exc, OSError):
        return CacheSchemaError({errno.ENOSPC: "cache_disk_full", errno.EACCES: "cache_permission_denied",
                                 errno.EPERM: "cache_permission_denied"}.get(exc.errno, "cache_io_error"))
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        return CacheSchemaError({sqlite3.SQLITE_BUSY: "cache_busy", sqlite3.SQLITE_LOCKED: "cache_busy",
                                 sqlite3.SQLITE_FULL: "cache_disk_full", sqlite3.SQLITE_READONLY: "cache_permission_denied",
                                 sqlite3.SQLITE_CORRUPT: "cache_corrupt", sqlite3.SQLITE_NOTADB: "cache_corrupt"}.get(code, "cache_io_error"))
    return CacheSchemaError("cache_io_error")


def _identity(path: Path, *, absent=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if absent:
            return None
        raise CacheSchemaError("cache_identity_changed") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino:
        raise CacheSchemaError("unsafe_cache_path")
    return info.st_dev, info.st_ino


def _location(path, authorized_directory=None) -> Path:
    path = Path(path).absolute()
    directory = Path(authorized_directory).absolute() if authorized_directory is not None else path.parent
    # Every protocol artifact is an immediate child of the authorized directory.
    if path.parent != directory or path.name in ("", ".", ".."):
        raise CacheSchemaError("unsafe_cache_path")
    for item in reversed((directory, *directory.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise CacheSchemaError("unsafe_cache_path")
    if directory.exists():
        info = directory.stat()
        if info.st_mode & 0o022 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
            raise CacheSchemaError("unsafe_cache_path")
    _identity(path, absent=True)
    for suffix in _SUFFIXES:
        other = path.with_name(path.name + suffix)
        _identity(other, absent=True)
        if suffix in (".v1-backup.sqlite", ".v1-backup.sqlite.partial"):
            for sidecar in ("-wal", "-shm", "-journal"):
                _identity(other.with_name(other.name + sidecar), absent=True)
    return path


def _supported(path: Path) -> bool:
    """The required identity capability, also injectable without host permissions."""
    if os.name != "posix" or fcntl is None or not getattr(os, "O_NOFOLLOW", 0):
        return False
    existing = next(item for item in (path, *path.parents) if item.exists())
    info = existing.stat()
    return bool(info.st_ino and info.st_dev is not None and info.st_nlink > 0)


def _revalidate(path: Path, expected, gate: str):
    # gate is intentionally a static label, useful for deterministic race tests.
    _location(path)
    if _identity(path, absent=True) != expected:
        raise CacheSchemaError("cache_identity_changed")


def _connect(path: Path, *, write=False, create=False):
    mode = "rwc" if create else "rw" if write else "ro"
    return sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=" + mode,
                           uri=True, timeout=0.0, isolation_level=None)


def _columns(conn, table):
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def _state(conn, *, integrity=True) -> SchemaState:
    if integrity and conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        return SchemaState.CORRUPT
    objects = conn.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    tables = {name for kind, name in objects if kind == "table"}
    if any(kind not in ("table", "index") for kind, _ in objects):
        return SchemaState.PARTIAL if tables & {"cache_schema", "cache_entries_v2"} else SchemaState.FOREIGN
    if (conn.execute("PRAGMA application_id").fetchone()[0] != 0
            or conn.execute("PRAGMA user_version").fetchone()[0] != 0):
        return SchemaState.PARTIAL if tables & {"cache_schema", "cache_entries_v2"} else SchemaState.FOREIGN
    if not tables:
        return SchemaState.EMPTY
    if tables & {"cache_schema", "cache_entries_v2"}:
        if tables != {"translations", "cache_schema", "cache_entries_v2"}:
            return SchemaState.PARTIAL
        if (_columns(conn, "translations") != _V1_COLUMNS or _columns(conn, "cache_entries_v2") != V2_COLUMNS
                or _columns(conn, "cache_schema") != ("singleton", "version", "legacy_namespace")
                or conn.execute("SELECT * FROM cache_schema").fetchall() != [(1, 2, "legacy-v1")]):
            return SchemaState.PARTIAL
        keys = tuple(row[1] for row in sorted(conn.execute("PRAGMA table_info(cache_entries_v2)"), key=lambda row: row[5]) if row[5])
        if keys != V2_COLUMNS[:5]:
            return SchemaState.PARTIAL
        info = conn.execute("PRAGMA table_info(cache_entries_v2)").fetchall()
        if any(row[2:] != ("BLOB" if row[1] in ("canonical_identity", "metadata", "provenance") else "TEXT",
                           1, None, index + 1 if index < 5 else 0) for index, row in enumerate(info)):
            return SchemaState.PARTIAL
        indexes = conn.execute("PRAGMA index_info(cache_entries_v2_compatible)").fetchall()
        if tuple(row[2] for row in indexes) != ("scope_kind", "scope_id", "value_kind", "source_digest", "semantic_fingerprint"):
            return SchemaState.PARTIAL
        return SchemaState.V2
    if tables != {"translations"}:
        return SchemaState.FOREIGN
    if _columns(conn, "translations") != _V1_COLUMNS:
        return SchemaState.CORRUPT
    indexes = conn.execute("PRAGMA index_list(translations)").fetchall()
    unique = [tuple(row[2] for row in conn.execute('PRAGMA index_info("' + index[1].replace('"', '""') + '")'))
              for index in indexes if index[2]]
    if ("source_lang", "target_lang", "original_text_hash") not in unique:
        return SchemaState.CORRUPT
    return SchemaState.V1


def classify_cache(path, *, authorized_directory=None) -> SchemaState:
    """Read-only classification, including absent parents; never creates a lock."""
    try:
        path = _location(path, authorized_directory)
        if _marker(path).exists() or _next_marker(path).exists():
            # A hot rollback journal may need a read/write recovery open. The
            # marker is actionable; read-only classification never recovers it.
            return SchemaState.PARTIAL
        if not path.exists():
            return SchemaState.ABSENT
        with _connection_closed(_connect(path)) as conn:
            state = _state(conn)
        return state
    except sqlite3.DatabaseError as exc:
        if (getattr(exc, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise CacheSchemaError("cache_busy") from None
        return SchemaState.CORRUPT
    except OSError as exc:
        raise _error(exc) from None



def classify_cache_waiting(path, *, timeout: float, authorized_directory=None) -> SchemaState:
    """Like classify_cache, but waits up to timeout seconds while another connection holds the cache."""
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            return classify_cache(path, authorized_directory=authorized_directory)
        except CacheSchemaError as exc:
            remaining = deadline - time.monotonic()
            if exc.code != "cache_busy" or remaining <= 0:
                raise
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.25)

@contextmanager
def _connection_closed(conn):
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _application_lock(path: Path, *, exclusive=False, create=False):
    lock = path.with_name(path.name + ".lock")
    if not lock.exists() and not create:
        yield lambda: None
        return
    if fcntl is None:
        raise CacheSchemaError("cache_platform_unsupported")
    before = _identity(lock, absent=True)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError:
        descriptor = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != _identity(lock):
            raise CacheSchemaError("unsafe_cache_path")
        if before is not None and (info.st_dev, info.st_ino) != before:
            raise CacheSchemaError("cache_identity_changed")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except BlockingIOError:
            raise CacheSchemaError("cache_busy") from None
        def verify_ownership():
            if (info.st_dev, info.st_ino) != _identity(lock):
                raise CacheSchemaError("cache_identity_changed")
        verify_ownership()
        yield verify_ownership
    finally:
        os.close(descriptor)


def _marker(path):
    return path.with_name(path.name + ".migration")


def _next_marker(path):
    return path.with_name(path.name + ".migration-next")


def _backup(path):
    return path.with_name(path.name + ".v1-backup.sqlite")


def _partial_backup(path):
    return path.with_name(path.name + ".v1-backup.sqlite.partial")


def _partial_backup_files(path):
    partial = _partial_backup(path)
    return (partial, *(partial.with_name(partial.name + sidecar) for sidecar in ("-wal", "-shm", "-journal")))


def _discard_partial_backup(path):
    """A snapshot that never reached its final name is ours to redo, and the database is still v1."""
    for item in _partial_backup_files(path):
        if _identity(item, absent=True) is not None:
            item.unlink()


def _flush_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_directory(directory):
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o700)
        except FileExistsError:
            if not item.is_dir() or item.is_symlink():
                raise CacheSchemaError("unsafe_cache_path") from None


def _restrict_files(path):
    for suffix in ("", "-wal", "-shm", "-journal", ".lock"):
        target = path.with_name(path.name + suffix)
        if target.exists():
            expected = _identity(target)
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != expected or info.st_nlink != 1:
                    raise CacheSchemaError("cache_identity_changed")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)


def _read_record(path):
    marker = _marker(path)
    if not marker.exists():
        return None
    _identity(marker)
    with marker.open("rb") as stream:
        raw = stream.read(4097)
    try:
        record = json.loads(raw)
        if (len(raw) > 4096 or set(record) != {"format", "phase", "database_identity", "backup_digest", "legacy_digest"}
                or record["format"] != 1 or record["phase"] not in PHASES
                or record["database_identity"] != list(_identity(path))
                or any(value is not None and (not isinstance(value, str) or len(value) != 64
                                             or any(ch not in "0123456789abcdef" for ch in value))
                       for value in (record["backup_digest"], record["legacy_digest"]))):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise CacheSchemaError("cache_recovery_required") from None
    return record


def _advance(path, record, phase, checkpoint):
    current = record.get("phase")
    if current is not None and PHASES.index(current) >= PHASES.index(phase):
        return
    if (current is None and phase != "classified") or (current is not None and PHASES.index(phase) != PHASES.index(current) + 1):
        raise CacheSchemaError("cache_invalid_transition")
    record["phase"] = phase
    temporary = _next_marker(path)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        _identity(temporary)
        _identity(_marker(path), absent=True)
        os.replace(temporary, _marker(path))
        _flush_directory(path.parent)
    finally:
        if temporary.exists():
            _identity(temporary)
            temporary.unlink()
    if checkpoint is not None:
        checkpoint(phase)


def _digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_digest(conn):
    digest = hashlib.sha256()
    for row in conn.execute("SELECT id,source_lang,target_lang,original_text_hash,original_text,translated_text,created_at FROM translations ORDER BY id"):
        if (not isinstance(row[0], int) or any(not isinstance(v, str) for v in row[1:])
                or hashlib.sha256(row[4].encode("utf-8")).hexdigest() != row[3]):
            raise CacheSchemaError("cache_corrupt")
        encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _verify_backup(path, record):
    backup = _backup(path)
    _location(path)
    if not backup.exists():
        raise CacheSchemaError("cache_backup_invalid")
    if any(backup.with_name(backup.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise CacheSchemaError("cache_backup_invalid")
    with _connection_closed(_connect(backup)) as conn:
        if _state(conn) is not SchemaState.V1 or conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise CacheSchemaError("cache_backup_invalid")
        if _legacy_digest(conn) != record["legacy_digest"]:
            raise CacheSchemaError("cache_backup_invalid")
    digest = _digest_file(backup)
    if record["backup_digest"] is not None and digest != record["backup_digest"]:
        raise CacheSchemaError("cache_backup_invalid")
    record["backup_digest"] = digest


def _snapshot(conn, path):
    """VACUUM INTO is SQLite's consistent snapshot, including committed WAL pages.

    The connection retains locking_mode=EXCLUSIVE across its commits. A separate
    Connection.backup while this connection owns an exclusive lock would block.
    """
    # Written under another name: a crash during VACUUM INTO must not look like a damaged backup.
    partial, target = _partial_backup(path), _backup(path)
    fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        conn.execute("VACUUM INTO ?", (str(partial),))
        if (info.st_dev, info.st_ino) != _identity(partial):
            raise CacheSchemaError("cache_identity_changed")
        os.fsync(fd)
    finally:
        os.close(fd)
    # A rename and not link plus unlink: two names for one file would fail the hard-link check on reopen.
    # The caller saw no backup under the exclusive lock, and this checks again right before it.
    if _identity(target, absent=True) is not None:
        raise CacheSchemaError("cache_backup_exists")
    os.rename(partial, target)
    _flush_directory(path.parent)


def _build(conn):
    conn.execute(LEGACY_SCHEMA)
    conn.execute(LEDGER_SCHEMA)
    conn.execute("INSERT INTO cache_schema VALUES (1, 2, 'legacy-v1')")
    conn.execute(V2_SCHEMA)
    conn.execute(COMPATIBLE_INDEX)


def _finish_marker(path):
    if _marker(path).exists():
        _identity(_marker(path))
        _marker(path).unlink()
        _flush_directory(path.parent)


def prepare_cache(path, *, authorized_directory=None, checkpoint=None) -> None:
    """Run once at the v2 composition boundary, before workers are constructed.

    Failure keeps a transactional v1 or verified v2 plus a bounded recovery
    record. Backups are retained, including incomplete backups needing review.
    """
    try:
        path = _location(path, authorized_directory)
        expected = _identity(path, absent=True)
        initial = classify_cache(path, authorized_directory=authorized_directory)
        if initial in (SchemaState.CORRUPT, SchemaState.FOREIGN):
            raise CacheSchemaError("cache_corrupt" if initial is SchemaState.CORRUPT else "cache_foreign")
        if not _supported(path):
            raise CacheSchemaError("cache_platform_unsupported")
        _create_directory(path.parent)
        with _application_lock(path, exclusive=True, create=True) as verify_lock:
            _revalidate(path, expected, "after-application-lock")
            if expected is None:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
                expected = _identity(path)
            with _connection_closed(_connect(path, write=True)) as conn:
                # Set before reading WAL, so SQLite requests its strongest lock.
                conn.execute("PRAGMA locking_mode=EXCLUSIVE")
                _revalidate(path, expected, "after-sqlite-open")
                conn.execute("BEGIN EXCLUSIVE")
                state = _state(conn)
                if state in (SchemaState.CORRUPT, SchemaState.FOREIGN, SchemaState.PARTIAL):
                    raise CacheSchemaError("cache_recovery_required" if state is SchemaState.PARTIAL else "cache_corrupt" if state is SchemaState.CORRUPT else "cache_foreign")
                record = _read_record(path)
                if _next_marker(path).exists():
                    # An interrupted marker publication is never a seventh state.
                    # Retain it for deliberate recovery, with the database intact.
                    raise CacheSchemaError("cache_recovery_required")
                if state is SchemaState.V2:
                    if record is not None:
                        if record["phase"] not in ("integrity-verified", "committed"):
                            raise CacheSchemaError("cache_recovery_required")
                        _verify_backup(path, record)
                        if _legacy_digest(conn) != record["legacy_digest"]:
                            raise CacheSchemaError("cache_recovery_required")
                        _advance(path, record, "committed", checkpoint)
                        _finish_marker(path)
                    conn.commit()
                    verify_lock()
                    _restrict_files(path)
                    return
                if state is SchemaState.EMPTY:
                    if record is not None:
                        raise CacheSchemaError("cache_recovery_required")
                    _build(conn)
                    if _state(conn) is not SchemaState.V2:
                        raise CacheSchemaError("cache_corrupt")
                    _revalidate(path, expected, "before-promotion")
                    verify_lock()
                    conn.commit()
                    _restrict_files(path)
                    _flush_directory(path.parent)
                    return
                legacy_digest = _legacy_digest(conn)
                # EXCLUSIVE locking mode retains the SQLite lock after COMMIT,
                # including while VACUUM INTO makes the backup outside a txn.
                conn.commit()
                if record is None:
                    # The marker is written first, so a snapshot without one is not ours.
                    if _backup(path).exists() or any(item.exists() for item in _partial_backup_files(path)):
                        raise CacheSchemaError("cache_backup_exists")
                    record = {"format": 1, "phase": None, "database_identity": list(expected),
                              "backup_digest": None, "legacy_digest": legacy_digest}
                    _advance(path, record, "classified", checkpoint)
                elif record["phase"] == "committed" or record["legacy_digest"] != legacy_digest:
                    raise CacheSchemaError("cache_recovery_required")
                _revalidate(path, expected, "before-backup")
                verify_lock()
                if record["phase"] == "classified":
                    _discard_partial_backup(path)
                if not _backup(path).exists():
                    if record["phase"] != "classified":
                        raise CacheSchemaError("cache_backup_invalid")
                    _snapshot(conn, path)
                _verify_backup(path, record)
                verified_backup_identity = _identity(_backup(path))
                _advance(path, record, "backup-verified", checkpoint)
                _restrict_files(path)
                _advance(path, record, "migration-pending", checkpoint)
                conn.execute("BEGIN EXCLUSIVE")
                try:
                    _build(conn)
                    _advance(path, record, "v2-built", checkpoint)
                    if (_state(conn) is not SchemaState.V2 or conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                            or _legacy_digest(conn) != legacy_digest
                            or conn.execute("SELECT COUNT(*) FROM cache_entries_v2").fetchone()[0] != 0):
                        raise CacheSchemaError("cache_corrupt")
                    _advance(path, record, "integrity-verified", checkpoint)
                    _revalidate(path, expected, "before-promotion")
                    verify_lock()
                    if _identity(_backup(path)) != verified_backup_identity:
                        raise CacheSchemaError("cache_identity_changed")
                    _verify_backup(path, record)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                _advance(path, record, "committed", checkpoint)
                _finish_marker(path)
                _restrict_files(path)
                _flush_directory(path.parent)
    except (OSError, sqlite3.Error, CacheSchemaError) as exc:
        raise _error(exc) from None


@contextmanager
def participating_connection(path, *, authorized_directory=None):
    """Worker operation: shared application lock, immutable v2 schema boundary."""
    path = _location(path, authorized_directory)
    expected = _identity(path)
    with _application_lock(path):
        _revalidate(path, expected, "after-application-lock")
        if _marker(path).exists() or _next_marker(path).exists():
            raise CacheSchemaError("cache_recovery_required")
        with _connection_closed(_connect(path, write=True)) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            _revalidate(path, expected, "after-sqlite-open")
            if _state(conn, integrity=False) is not SchemaState.V2:
                raise CacheSchemaError("cache_recovery_required")
            try:
                yield conn
            finally:
                conn.rollback()


@contextmanager
def participating_admin_connection(path, *, write=False, create=False, timeout=30.0):
    """Shared v1 adapter: never promotes or silently projects v2 into JSON v1."""
    path = _location(path)
    if create:
        _create_directory(path.parent)
    expected = _identity(path, absent=True)
    with _application_lock(path, create=create):
        _revalidate(path, expected, "after-application-lock")
        if _marker(path).exists() or _next_marker(path).exists():
            raise CacheSchemaError("v2-admin-required")
        with _connection_closed(_connect(path, write=write, create=create)) as conn:
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            # Read transactions pin the pre-migration snapshot even if no lock
            # file existed at opening time. BEGIN IMMEDIATE stays with writers.
            if not write:
                conn.execute("BEGIN")
            _revalidate(path, _identity(path) if expected is None and create else expected, "after-sqlite-open")
            state = _state(conn, integrity=False)
            if state in (SchemaState.V2, SchemaState.PARTIAL):
                raise CacheSchemaError("v2-admin-required")
            yield conn
