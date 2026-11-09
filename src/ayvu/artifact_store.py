"""SQLite metadata and contained local files for job artifacts.

Files live under ``projects/<UUID>/artifacts``. Every path is built here from a
validated UUID and rechecked around each operation. Publication uses a hard link
because, unlike rename, it fails rather than replacing an existing destination.
Nothing in this module deletes a payload except the staging name of a file that
was verified to be the same inode as its published name.
"""
from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator
from uuid import uuid4

from .artifact import (
    MAX_ARTIFACT_BYTES, Artifact, ArtifactConflictError, ArtifactError, ArtifactIntegrityError, ArtifactNotFoundError,
    ArtifactOwnershipError, ArtifactPathError, ArtifactRequest, ArtifactState, ArtifactStoreError,
    ArtifactValidationError, abandon, mark_ready, new_artifact, ready_location, record_payload, retire,
    validate_identity,
)
from .artifact_service import PayloadEvidence, StagedArtifact
from .job import JobError, JobNotFoundError, TranslationJob
from .job_store import load_job
from .migration_contracts import MigrationError
from .sqlite_migrations import SqliteMigrationRunner, sqlite_failure_code

_READ_CHUNK = 1024 * 1024

# Frozen once released: the migration ledger checksums this exact text.
ARTIFACT_SCHEMA_STATEMENTS = (
    """CREATE TABLE job_artifacts (
    artifact_id TEXT PRIMARY KEY NOT NULL,
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES translation_jobs(job_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    stage TEXT NOT NULL,
    stage_version INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    media_type TEXT NOT NULL,
    location TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('staging', 'ready', 'abandoned')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    size INTEGER CHECK (size IS NULL OR size >= 0),
    digest TEXT,
    reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((size IS NULL) = (digest IS NULL)),
    CHECK (state <> 'ready' OR size IS NOT NULL),
    CHECK ((state = 'abandoned') = (reason_code IS NOT NULL))
)""",
    """CREATE UNIQUE INDEX job_artifacts_one_active
    ON job_artifacts(job_id, role, stage, stage_version, input_fingerprint)
    WHERE state IN ('staging', 'ready')""",
    """CREATE INDEX job_artifacts_by_state ON job_artifacts(state, created_at, artifact_id)""",
    """CREATE TRIGGER job_artifacts_immutable BEFORE UPDATE ON job_artifacts
WHEN OLD.state = 'abandoned'
    OR (OLD.state = 'ready' AND NEW.state <> 'abandoned')
    OR NEW.artifact_id IS NOT OLD.artifact_id
    OR NEW.schema_version IS NOT OLD.schema_version
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.job_id IS NOT OLD.job_id
    OR NEW.role IS NOT OLD.role
    OR NEW.stage IS NOT OLD.stage
    OR NEW.stage_version IS NOT OLD.stage_version
    OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
    OR NEW.media_type IS NOT OLD.media_type
    OR NEW.location IS NOT OLD.location
    OR NEW.created_at IS NOT OLD.created_at
    OR (OLD.size IS NOT NULL AND (NEW.size IS NOT OLD.size OR NEW.digest IS NOT OLD.digest))
    OR NEW.revision <= OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'immutable_artifact');
END""",
)

_COLUMNS = ("artifact_id, schema_version, project_id, job_id, role, stage, stage_version, input_fingerprint, "
            "media_type, location, state, revision, size, digest, reason_code, created_at, updated_at")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


def _encode_time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _decode(row: tuple) -> Artifact:
    (artifact_id, schema_version, project_id, job_id, role, stage, stage_version, input_fingerprint, media_type,
     location, state, revision, size, digest, reason_code, created_at, updated_at) = row
    try:
        artifact = Artifact(
            artifact_id=artifact_id,
            request=ArtifactRequest(project_id, job_id, role, stage, stage_version, input_fingerprint, media_type),
            state=ArtifactState(state), revision=revision,
            created_at=datetime.fromisoformat(created_at), updated_at=datetime.fromisoformat(updated_at),
            size=size, digest=digest, reason_code=reason_code, schema_version=schema_version)
    except ArtifactValidationError as error:
        if str(error) == "unsupported_artifact_version":
            raise
        raise ArtifactStoreError("invalid_artifact_record") from None
    except (ValueError, TypeError):
        raise ArtifactStoreError("invalid_artifact_record") from None
    # A stored path is never trusted: it must be the one this ID derives to.
    if location != ready_location(artifact_id):
        raise ArtifactPathError("invalid_location")
    return artifact


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except ArtifactError:
        raise
    except JobNotFoundError:
        raise ArtifactNotFoundError("job_not_found") from None
    except JobError as error:
        raise ArtifactStoreError(str(error)) from None
    except MigrationError as error:
        raise ArtifactStoreError(error.code) from None
    except sqlite3.Error as error:
        raise ArtifactStoreError(sqlite_failure_code(error)) from None
    except OSError as error:
        raise ArtifactStoreError(_os_failure_code(error)) from None


def _os_failure_code(error: OSError) -> str:
    if isinstance(error, PermissionError):
        return "permission"
    return "disk_full" if error.errno == errno.ENOSPC else "io"


class SqliteArtifactRepository:
    """Artifact records of one project. The database must already be at the runner's target schema."""

    def __init__(self, runner: SqliteMigrationRunner, database: Path, project_id: str, *,
                 clock: Callable[[], datetime] = _utc_now, new_id: Callable[[], str] = _new_id) -> None:
        validate_identity(project_id)
        self._runner = runner
        self._database = database
        self._project_id = project_id
        self._clock = clock
        self._new_id = new_id

    def __repr__(self) -> str:
        return f"SqliteArtifactRepository(project_id={self._project_id!r})"

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with _translated_errors(), closing(self._runner.connect(self._database, readonly=True)) as connection:
            yield connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with _translated_errors(), closing(self._runner.connect(self._database, readonly=False)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")

    @staticmethod
    def _load(connection: sqlite3.Connection, artifact_id: str) -> Artifact:
        validate_identity(artifact_id)
        row = connection.execute(f"SELECT {_COLUMNS} FROM job_artifacts WHERE artifact_id = ?",
                                 (artifact_id,)).fetchone()
        if row is None:
            raise ArtifactNotFoundError("artifact_not_found")
        return _decode(row)

    @staticmethod
    def _store(connection: sqlite3.Connection, before: Artifact, after: Artifact) -> Artifact:
        cursor = connection.execute(
            "UPDATE job_artifacts SET state = ?, revision = ?, size = ?, digest = ?, reason_code = ?, updated_at = ? "
            "WHERE artifact_id = ? AND revision = ?",
            (after.state.value, after.revision, after.size, after.digest, after.reason_code,
             _encode_time(after.updated_at), before.artifact_id, before.revision))
        if cursor.rowcount != 1:
            raise ArtifactConflictError("stale_revision")
        return after

    def _require_owner(self, job: TranslationJob, owner_token: str, now: datetime) -> None:
        if job.lease is None or job.lease.owner_token != owner_token:
            raise ArtifactOwnershipError("lease_not_held")
        if job.lease.expired(now):
            raise ArtifactOwnershipError("lease_expired")

    def _owner_change(self, artifact_id: str, owner_token: str,
                      change: Callable[[Artifact, datetime], Artifact]) -> Artifact:
        with self._write() as connection:
            artifact = self._load(connection, artifact_id)
            now = self._clock()
            self._require_owner(load_job(connection, artifact.request.job_id), owner_token, now)
            return self._store(connection, artifact, change(artifact, now))

    def _stale_change(self, artifact_id: str, change: Callable[[Artifact, datetime], Artifact]) -> Artifact:
        with self._write() as connection:
            artifact = self._load(connection, artifact_id)
            now = self._clock()
            lease = load_job(connection, artifact.request.job_id).lease
            if lease is not None and not lease.expired(now):
                raise ArtifactConflictError("job_active")
            return self._store(connection, artifact, change(artifact, now))

    def reserve(self, request: ArtifactRequest, owner_token: str) -> Artifact:
        if type(request) is not ArtifactRequest:
            raise ArtifactValidationError("invalid_request")
        if request.project_id != self._project_id:
            raise ArtifactOwnershipError("project_mismatch")
        with self._write() as connection:
            job = load_job(connection, request.job_id)
            if job.request.project_id != request.project_id:
                raise ArtifactOwnershipError("ownership_mismatch")
            now = self._clock()
            self._require_owner(job, owner_token, now)
            row = connection.execute(
                "SELECT state FROM job_artifacts WHERE job_id = ? AND role = ? AND stage = ? AND stage_version = ? "
                "AND input_fingerprint = ? AND state IN ('staging', 'ready')",
                (request.job_id, request.role, request.stage, request.stage_version,
                 request.input_fingerprint)).fetchone()
            if row is not None:
                raise ArtifactConflictError("artifact_exists" if row[0] == "ready" else "artifact_in_progress")
            artifact = new_artifact(request, self._new_id(), now)
            connection.execute(
                f"INSERT INTO job_artifacts ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (artifact.artifact_id, artifact.schema_version, request.project_id, request.job_id, request.role,
                 request.stage, request.stage_version, request.input_fingerprint, request.media_type,
                 ready_location(artifact.artifact_id), artifact.state.value, artifact.revision,
                 _encode_time(artifact.created_at), _encode_time(artifact.updated_at)))
            return artifact

    def record_payload(self, artifact_id: str, expected_revision: int, size: int, digest: str,
                       owner_token: str) -> Artifact:
        return self._owner_change(artifact_id, owner_token, lambda artifact, now: record_payload(
            artifact, size, digest, expected_revision=expected_revision, now=now))

    def mark_ready(self, artifact_id: str, expected_revision: int, owner_token: str) -> Artifact:
        return self._owner_change(artifact_id, owner_token, lambda artifact, now: mark_ready(
            artifact, expected_revision=expected_revision, now=now))

    def abandon(self, artifact_id: str, expected_revision: int, reason_code: str, owner_token: str) -> Artifact:
        return self._owner_change(artifact_id, owner_token, lambda artifact, now: abandon(
            artifact, reason_code, expected_revision=expected_revision, now=now))

    def settle_ready(self, artifact_id: str, expected_revision: int) -> Artifact:
        return self._stale_change(artifact_id, lambda artifact, now: mark_ready(
            artifact, expected_revision=expected_revision, now=now))

    def settle_abandoned(self, artifact_id: str, expected_revision: int, reason_code: str) -> Artifact:
        return self._stale_change(artifact_id, lambda artifact, now: abandon(
            artifact, reason_code, expected_revision=expected_revision, now=now))

    def retire(self, artifact_id: str, expected_revision: int, reason_code: str) -> Artifact:
        with self._write() as connection:
            artifact = self._load(connection, artifact_id)
            return self._store(connection, artifact, retire(
                artifact, reason_code, expected_revision=expected_revision, now=self._clock()))

    def get(self, artifact_id: str) -> Artifact:
        with self._read() as connection:
            return self._load(connection, artifact_id)

    def find_ready(self, request: ArtifactRequest) -> Artifact | None:
        if type(request) is not ArtifactRequest:
            raise ArtifactValidationError("invalid_request")
        with self._read() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM job_artifacts WHERE job_id = ? AND role = ? AND stage = ? "
                "AND stage_version = ? AND input_fingerprint = ? AND state = 'ready'",
                (request.job_id, request.role, request.stage, request.stage_version,
                 request.input_fingerprint)).fetchone()
            return None if row is None else _decode(row)

    def list_ready(self, job_id: str, limit: int = 256) -> tuple[Artifact, ...]:
        """The ready records of one job, oldest first. Read only; it says nothing about their files."""
        validate_identity(job_id)
        if type(limit) is not int or limit < 1:
            raise ArtifactValidationError("invalid_limit")
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM job_artifacts WHERE job_id = ? AND state = 'ready' ORDER BY created_at, artifact_id LIMIT ?",
                (job_id, limit)).fetchall()
            return tuple(_decode(row) for row in rows)

    def list_staging(self, limit: int) -> tuple[StagedArtifact, ...]:
        if type(limit) is not int or limit < 1:
            raise ArtifactValidationError("invalid_limit")
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM job_artifacts WHERE state = 'staging' "
                "ORDER BY created_at, artifact_id LIMIT ?", (limit,)).fetchall()
            now = self._clock()
            result = []
            for row in rows:
                artifact = _decode(row)
                lease = load_job(connection, artifact.request.job_id).lease
                result.append(StagedArtifact(artifact, lease is not None and not lease.expired(now)))
            return tuple(result)


class ArtifactFileOps:
    """The only calls that change the filesystem; tests replace them to inject faults."""

    def open_new(self, path: Path) -> BinaryIO:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        return os.fdopen(os.open(path, flags, 0o600), "wb")

    def write(self, stream: BinaryIO, data: bytes) -> None:
        stream.write(data)

    def sync(self, stream: BinaryIO) -> None:
        stream.flush()
        os.fsync(stream.fileno())

    def link(self, source: Path, target: Path) -> None:
        os.link(source, target, follow_symlinks=os.link not in os.supports_follow_symlinks)

    def unlink(self, path: Path) -> None:
        path.unlink()

    def sync_directory(self, path: Path) -> None:
        if os.name != "nt":
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class LocalArtifactPayloads:
    def __init__(self, root: Path, *, ops: ArtifactFileOps | None = None) -> None:
        self._root = Path(os.path.abspath(root))
        self._ops = ops or ArtifactFileOps()

    def __repr__(self) -> str:
        return "LocalArtifactPayloads()"

    def _staged(self, artifact_id: str) -> Path:
        validate_identity(artifact_id)
        return self._root / "staging" / f"{artifact_id}.part"

    def _final(self, artifact_id: str) -> Path:
        validate_identity(artifact_id)
        return self._root / "ready" / f"{artifact_id}.bin"

    def _contain(self, path: Path) -> Path:
        """Refuse a symlink anywhere on the way, before and after each operation."""
        for component in (*reversed(path.parents), path):
            if component.is_symlink():
                raise ArtifactPathError("symlink_path")
        return path

    def _prepare_directory(self, directory: Path) -> None:
        for item in (self._root, directory):
            self._contain(item)
            created = not item.exists()
            item.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._contain(item)
            if not item.is_dir():
                raise ArtifactPathError("invalid_artifact_directory")
            if created:
                # Otherwise a power loss could drop the directory with a "ready" file inside.
                self._ops.sync_directory(item.parent)

    @staticmethod
    def _hash(stream: BinaryIO) -> tuple[int, str]:
        hasher = hashlib.sha256()
        size = 0
        while chunk := stream.read(_READ_CHUNK):
            size += len(chunk)
            hasher.update(chunk)
        return size, f"sha256:{hasher.hexdigest()}"

    def _open_regular(self, path: Path) -> tuple[BinaryIO, os.stat_result] | None:
        """No-follow open; the checks apply to this handle, not to a later lookup of the name."""
        self._contain(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ArtifactPathError("symlink_path") from None
            raise
        # Checked on the descriptor: wrapping a directory in a stream would raise first.
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise ArtifactPathError("not_a_regular_file")
        return os.fdopen(fd, "rb"), info

    def _measure(self, path: Path) -> tuple[int, str, os.stat_result] | None:
        opened = self._open_regular(path)
        if opened is None:
            return None
        stream, info = opened
        with stream:
            size, digest = self._hash(stream)
        return size, digest, info

    def write_staged(self, artifact_id: str, chunks: Iterable[bytes],
                     between_chunks: Callable[[], None]) -> tuple[int, str]:
        path = self._staged(artifact_id)
        with _translated_errors():
            self._prepare_directory(path.parent)
            self._contain(path)
            try:
                stream = self._ops.open_new(path)
            except FileExistsError:
                raise ArtifactConflictError("staging_path_exists") from None
            hasher = hashlib.sha256()
            size = 0
            with stream:
                for chunk in chunks:
                    if type(chunk) is not bytes:
                        raise ArtifactValidationError("invalid_chunk")
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise ArtifactValidationError("artifact_too_large")
                    hasher.update(chunk)
                    self._ops.write(stream, chunk)
                    between_chunks()
                self._ops.sync(stream)
            self._ops.sync_directory(path.parent)
            digest = f"sha256:{hasher.hexdigest()}"
            measured = self._measure(path)
            if measured is None or measured[:2] != (size, digest) or measured[2].st_nlink != 1:
                raise ArtifactIntegrityError("staged_payload_mismatch")
            return size, digest

    def publish(self, artifact_id: str, size: int, digest: str) -> None:
        staged, final = self._staged(artifact_id), self._final(artifact_id)
        with _translated_errors():
            self._prepare_directory(final.parent)
            self._contain(staged)
            self._contain(final)
            try:
                self._ops.link(staged, final)
            except FileExistsError:
                raise ArtifactConflictError("ready_path_collision") from None
            self._ops.sync_directory(final.parent)
            measured = self._measure(final)
            if measured is None or measured[:2] != (size, digest):
                raise ArtifactIntegrityError("final_payload_mismatch")
            self._ops.unlink(staged)
            self._ops.sync_directory(staged.parent)

    def _same_file(self, staged: os.stat_result | None, final: os.stat_result) -> bool:
        return staged is not None and (staged.st_dev, staged.st_ino) == (final.st_dev, final.st_ino)

    def evidence(self, artifact_id: str, size: int | None, digest: str | None) -> PayloadEvidence:
        staged, final = self._staged(artifact_id), self._final(artifact_id)
        with _translated_errors():
            opened = self._open_regular(final)
            if opened is None:
                return PayloadEvidence("absent")
            stream, info = opened
            with stream:
                measured = self._hash(stream)
            if measured != (size, digest):
                return PayloadEvidence("mismatch")
            if info.st_nlink == 1:
                return PayloadEvidence("match")
            # Two names are legitimate only as the interrupted link of this very artifact.
            other = self._open_regular(staged)
            if other is None:
                return PayloadEvidence("mismatch")
            other[0].close()
            same = info.st_nlink == 2 and self._same_file(other[1], info)
            return PayloadEvidence("match" if same else "mismatch")

    def complete_publication(self, artifact_id: str) -> None:
        staged, final = self._staged(artifact_id), self._final(artifact_id)
        with _translated_errors():
            opened = self._open_regular(staged)
            if opened is None:
                return
            opened[0].close()
            final_opened = self._open_regular(final)
            if final_opened is None:
                return
            final_opened[0].close()
            if self._same_file(opened[1], final_opened[1]):
                self._ops.unlink(staged)
                self._ops.sync_directory(staged.parent)

    def open_verified(self, artifact_id: str, size: int, digest: str) -> BinaryIO:
        final = self._final(artifact_id)
        with _translated_errors():
            opened = self._open_regular(final)
            if opened is None:
                raise ArtifactIntegrityError("payload_missing")
            stream, info = opened
            try:
                if info.st_nlink != 1:
                    raise ArtifactIntegrityError("payload_has_aliases")
                if self._hash(stream) != (size, digest):
                    raise ArtifactIntegrityError("payload_mismatch")
                stream.seek(0)
                return stream
            except BaseException:
                stream.close()
                raise
