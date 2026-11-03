"""Project-local SQLite bootstrap and all-or-nothing, backed-up upgrades.

Registries are allowlisted in-tree SQL, never document/plugin input. The caller
owns layout resolution. No connection, SQL text or absolute path enters results.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from .migration_contracts import MigrationError, MigrationPlan, MigrationResult

APPLICATION_ID = 0x41595655
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_BACKUPS = 1024
LEDGER = 'ayvu_schema_migrations'


@dataclass(frozen=True, repr=False)
class SqliteMigration:
    version: int
    statements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.version) is not int or not 1 <= self.version <= 64
                or type(self.statements) is not tuple or len(self.statements) > 128
                or any(not isinstance(sql, str) or not sql.strip() or len(sql) > 65536
                       or '\x00' in sql for sql in self.statements)):
            raise MigrationError('invalid_registry')
        try:
            for sql in self.statements:
                sql.encode('utf-8')
        except UnicodeError:
            raise MigrationError('invalid_registry') from None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(b'\x00'.join(sql.encode('utf-8') for sql in self.statements)).hexdigest()


@dataclass
class _Attempt:
    current: int | None = None
    backup_name: str | None = None
    backup_verified: bool = False


def _safe(path: Path) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    for item in (*absolute.parents, absolute):
        if item.is_symlink():
            raise MigrationError('unsafe_path')
    if absolute.exists() and absolute.is_file() and absolute.stat().st_nlink != 1:
        if _interrupted_link_publication(absolute):
            raise MigrationError('recovery_required')
        raise MigrationError('unsafe_path')
    return absolute


def _interrupted_link_publication(path: Path) -> bool:
    """Recognize an owned staging/final pair; never remove either name here."""
    def staged(name: str) -> bool:
        return name.startswith(('.migration-', '.backup-')) and name.endswith('.sqlite')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 2:
        return False
    for count, candidate in enumerate(path.parent.iterdir()):
        if count >= MAX_BACKUPS:
            return False
        if candidate == path or staged(candidate.name) == staged(path.name):
            continue
        other = candidate.lstat()
        if (stat.S_ISREG(other.st_mode) and other.st_nlink == 2
                and (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino)):
            return True
    return False


def _private_parents(path: Path) -> None:
    if os.name == 'nt':
        return
    for item in path.parents:
        try:
            info = item.stat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise MigrationError('unsafe_path')


def _regular(path: Path, *, missing: bool = False) -> None:
    path = _safe(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        if missing:
            return
        raise MigrationError('io') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MigrationError('unsafe_path')
    if info.st_size > MAX_DATABASE_BYTES:
        raise MigrationError('resource_limit')


@contextmanager
def _migration_lock(path: Path, timeout: float):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(_safe(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
            raise MigrationError('unsafe_path')
        with os.fdopen(fd, 'r+b') as stream:
            fd = None
            if info.st_size == 0:
                stream.write(b'0')
                stream.flush()
            deadline = time.monotonic() + timeout
            locked = False
            try:
                while True:
                    try:
                        if os.name == 'nt':
                            import msvcrt
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except (BlockingIOError, PermissionError):
                        if time.monotonic() >= deadline:
                            raise MigrationError('busy') from None
                        time.sleep(0.01)
                yield
            finally:
                if locked:
                    if os.name == 'nt':
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        if fd is not None:
            os.close(fd)


def _sqlite_failure(error: sqlite3.Error) -> str:
    code = getattr(error, 'sqlite_errorcode', 0)
    if code in {getattr(sqlite3, 'SQLITE_READONLY_ROLLBACK', 776),
                getattr(sqlite3, 'SQLITE_READONLY_RECOVERY', 264)}:
        return 'recovery_required'
    primary = code & 255
    if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return 'busy'
    if primary == sqlite3.SQLITE_INTERRUPT:
        return 'deadline'
    if primary in {sqlite3.SQLITE_TOOBIG, sqlite3.SQLITE_NOMEM}:
        return 'resource_limit'
    if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return 'integrity_failed'
    if primary in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM}:
        return 'permission'
    if primary in {sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL, sqlite3.SQLITE_CANTOPEN}:
        return 'io'
    return 'sql_failure'


class SqliteMigrationRunner:
    def __init__(self, registry: tuple[SqliteMigration, ...] | None = None, *,
                 busy_timeout: float = 0.5, budget_seconds: float = 5.0) -> None:
        self.registry = registry if registry is not None else (SqliteMigration(1),)
        if (type(self.registry) is not tuple or not self.registry
                or any(type(step) is not SqliteMigration for step in self.registry)
                or self.registry[0].statements
                or isinstance(busy_timeout, bool) or not 0 < busy_timeout <= 2
                or isinstance(budget_seconds, bool) or not busy_timeout <= budget_seconds <= 30):
            raise MigrationError('invalid_registry')
        MigrationPlan(tuple(step.version for step in self.registry), len(self.registry))
        self.busy_timeout = busy_timeout
        self.budget_seconds = budget_seconds

    def _connect(self, path: Path, deadline: float, *, readonly: bool) -> sqlite3.Connection:
        if time.monotonic() >= deadline:
            raise MigrationError('deadline')
        _regular(path)
        uri = path.as_uri() + ('?mode=ro' if readonly else '?mode=rw')
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=self.busy_timeout)
        try:
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_DATABASE_BYTES)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 262144)
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if readonly:
                connection.execute('PRAGMA query_only=ON')
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> None:
        if connection.execute('PRAGMA integrity_check(1)').fetchmany(2) != [('ok',)]:
            raise MigrationError('integrity_failed')

    def _state(self, connection: sqlite3.Connection, plan: MigrationPlan) -> int:
        self._integrity(connection)
        current = connection.execute('PRAGMA user_version').fetchone()[0]
        plan.pending(current)
        if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise MigrationError('unsupported_journal')
        application = connection.execute('PRAGMA application_id').fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchmany(257)
        if len(tables) > 256:
            raise MigrationError('integrity_failed')
        if current == 0:
            if application != 0 or tables or connection.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone():
                raise MigrationError('unmanaged_database')
            return 0
        if application != APPLICATION_ID or (LEDGER,) not in tables:
            raise MigrationError('unmanaged_database')
        columns = connection.execute('PRAGMA table_info(ayvu_schema_migrations)').fetchmany(4)
        if [row[1:3] for row in columns] != [('version', 'INTEGER'), ('checksum', 'TEXT')]:
            raise MigrationError('version_gap')
        rows = connection.execute('SELECT version, checksum FROM ayvu_schema_migrations ORDER BY version').fetchmany(65)
        if [row[0] for row in rows] != list(range(1, current + 1)):
            raise MigrationError('version_gap')
        if any(checksum != self.registry[version - 1].checksum for version, checksum in rows):
            raise MigrationError('checksum_mismatch')
        return current

    @staticmethod
    def _step_authorizer(action, first, second, database, trigger):
        denied = {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_ATTACH,
                  sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
                  sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE}
        if action in denied or LEDGER in tuple(value.lower() for value in (first, second) if isinstance(value, str)):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() == 'load_extension':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _apply(self, connection: sqlite3.Connection, plan: MigrationPlan, current: int) -> tuple[int, ...]:
        pending = plan.pending(current)
        if current == 0:
            connection.execute('CREATE TABLE ayvu_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        for version in pending:
            connection.set_authorizer(self._step_authorizer)
            try:
                for statement in self.registry[version - 1].statements:
                    connection.execute(statement)
            finally:
                connection.set_authorizer(None)
            if not connection.in_transaction:
                raise MigrationError('sql_failure')
            connection.execute('INSERT INTO ayvu_schema_migrations(version, checksum) VALUES (?, ?)',
                               (version, self.registry[version - 1].checksum))
            connection.execute(f'PRAGMA user_version={version}')
        pages = connection.execute('PRAGMA page_count').fetchone()[0]
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        if pages * page_size > MAX_DATABASE_BYTES:
            raise MigrationError('resource_limit')
        self._integrity(connection)
        self._before_commit(connection)
        connection.execute('COMMIT')
        return pending

    def _before_commit(self, connection: sqlite3.Connection) -> None:
        """Transaction-boundary seam for deterministic interruption tests."""

    @staticmethod
    def _sync_file(path: Path) -> None:
        with path.open('rb') as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name != 'nt':
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _publish_unique(staged: Path, final: Path) -> None:
        # Atomically install a complete file without ever replacing an existing one.
        os.link(staged, final)
        staged.unlink()

    def _verify_backup(self, path: Path, plan: MigrationPlan, current: int, deadline: float) -> None:
        with closing(self._connect(path, deadline, readonly=True)) as connection:
            connection.execute('BEGIN')
            if self._state(connection, plan) != current:
                raise MigrationError('backup_failed')

    def _backup(self, database: Path, directory: Path, plan: MigrationPlan, attempt: _Attempt, deadline: float) -> None:
        try:
            directory = _safe(directory)
            _private_parents(directory / 'snapshot')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            entries = set()
            for count, entry in enumerate(directory.iterdir()):
                if count >= MAX_BACKUPS:
                    raise MigrationError('backup_limit')
                _regular(entry)
                entries.add(entry.name)
            sequence = next(number for number in range(1, MAX_BACKUPS + 1)
                            if f'v{attempt.current:08d}-to-{plan.target:08d}-{number:06d}.sqlite' not in entries)
            name = f'v{attempt.current:08d}-to-{plan.target:08d}-{sequence:06d}.sqlite'
            fd, temporary_name = tempfile.mkstemp(prefix='.backup-', suffix='.sqlite', dir=directory)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                with closing(self._connect(database, deadline, readonly=True)) as source:
                    source.execute('BEGIN')
                    with closing(self._connect(temporary, deadline, readonly=False)) as target:
                        self._copy_backup(source, target, deadline)
                self._verify_backup(temporary, plan, attempt.current, deadline)
                self._sync_file(temporary)
                self._publish_unique(temporary, directory / name)
                attempt.backup_name = name
                self._sync_directory(directory)
                self._verify_backup(directory / name, plan, attempt.current, deadline)
                attempt.backup_verified = True
            finally:
                temporary.unlink(missing_ok=True)
        except MigrationError as error:
            if error.code in {'deadline', 'backup_limit', 'unsafe_path', 'recovery_required', 'resource_limit'}:
                raise
            raise MigrationError('backup_failed') from None
        except (OSError, sqlite3.Error, StopIteration):
            raise MigrationError('backup_failed') from None

    def _copy_backup(self, source: sqlite3.Connection, target: sqlite3.Connection, deadline: float) -> None:
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise MigrationError('deadline')
            if total * page_size > MAX_DATABASE_BYTES:
                raise MigrationError('backup_failed')
        source.backup(target, pages=64, progress=progress, sleep=0.01)

    def _fresh(self, database: Path, plan: MigrationPlan, deadline: float) -> MigrationResult:
        fd, name = tempfile.mkstemp(prefix='.migration-', suffix='.sqlite', dir=database.parent)
        os.close(fd)
        staged = Path(name)
        try:
            with closing(self._connect(staged, deadline, readonly=False)) as connection:
                connection.execute('BEGIN IMMEDIATE')
                applied = self._apply(connection, plan, 0)
            self._verify_backup(staged, plan, plan.target, deadline)
            self._sync_file(staged)
            self._publish_unique(staged, database)
            try:
                self._sync_directory(database.parent)
            except OSError:
                raise MigrationError('publication_uncertain') from None
            return MigrationResult('created', 0, plan.target, applied)
        finally:
            staged.unlink(missing_ok=True)

    def migrate(self, database: Path, backups: Path, *, target_version: int | None = None) -> MigrationResult:
        attempt = _Attempt()
        try:
            plan = MigrationPlan(tuple(step.version for step in self.registry),
                                 len(self.registry) if target_version is None else target_version)
            deadline = time.monotonic() + self.budget_seconds
            database, backups = _safe(database), _safe(backups)
            _private_parents(database)
            if (not backups.is_relative_to(database.parent)
                    or backups == database or backups.is_relative_to(database)
                    or database.is_relative_to(backups) or not database.parent.is_dir()):
                raise MigrationError('unsafe_path')
            for suffix in ('-journal', '-wal', '-shm'):
                _regular(database.with_name(database.name + suffix), missing=True)
            with _migration_lock(database.with_name('.' + database.name + '.migrate.lock'), self.busy_timeout):
                if not database.exists():
                    return self._fresh(database, plan, deadline)
                with closing(self._connect(database, deadline, readonly=True)) as reader:
                    reader.execute('BEGIN')
                    attempt.current = self._state(reader, plan)
                if not plan.pending(attempt.current):
                    return MigrationResult('unchanged', attempt.current, attempt.current)
                with closing(self._connect(database, deadline, readonly=False)) as writer:
                    try:
                        writer.execute('BEGIN IMMEDIATE')
                        attempt.current = self._state(writer, plan)
                        if not plan.pending(attempt.current):
                            return MigrationResult('unchanged', attempt.current, attempt.current)
                        self._backup(database, backups, plan, attempt, deadline)
                        applied = self._apply(writer, plan, attempt.current)
                    finally:
                        if writer.in_transaction:
                            writer.execute('ROLLBACK')
                return MigrationResult('upgraded', attempt.current, plan.target, applied,
                                       backup_name=attempt.backup_name, backup_verified=True)
        except MigrationError as error:
            code = error.code
        except sqlite3.Error as error:
            code = _sqlite_failure(error)
        except PermissionError:
            code = 'permission'
        except OSError:
            code = 'io'
        return MigrationResult('failed', attempt.current, attempt.current, error=code,
                               backup_name=attempt.backup_name, backup_verified=attempt.backup_verified)
