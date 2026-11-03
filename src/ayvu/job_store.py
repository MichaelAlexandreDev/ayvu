"""SQLite persistence for translation jobs inside one project database.

Each operation opens its own connection and holds one short transaction, so no
provider, document or filesystem-heavy work can run inside it. Reads use a
read-only connection: loading a job cannot change it.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from .job import (
    JobContinuationError, JobError, JobLease, JobNotFoundError, JobRequest, JobRevisionConflictError,
    JobState, JobStoreError, JobTerminalSummary, JobValidationError, TranslationJob, acquire_lease,
    new_job, release_lease, renew_lease, requires_reconciliation, transition_job, validate_job_identity,
)
from .job_service import JobSnapshot
from .migration_contracts import MigrationError
from .sqlite_migrations import SqliteMigrationRunner, sqlite_failure_code

MAX_LIST_LIMIT = 1024

# Frozen once released: the migration ledger checksums this exact text. Literal
# on purpose, so renaming a Python constant can never alter a historical migration.
JOB_SCHEMA_STATEMENTS = (
    """CREATE TABLE translation_jobs (
    job_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    command_version INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    output_roles TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'preflight', 'running', 'pausing', 'paused', 'cancelling', 'cancelled',
        'partially_completed', 'failed', 'completed', 'validating', 'validated')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_owner_token TEXT,
    lease_acquired_at TEXT,
    lease_expires_at TEXT,
    terminal_summary TEXT,
    continuation_of TEXT UNIQUE REFERENCES translation_jobs(job_id) ON DELETE RESTRICT,
    CHECK ((lease_owner_token IS NULL) = (lease_acquired_at IS NULL)
           AND (lease_owner_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK (lease_owner_token IS NULL
           OR state NOT IN ('validated', 'partially_completed', 'failed', 'cancelled')),
    CHECK (terminal_summary IS NULL
           OR state IN ('validated', 'partially_completed', 'failed', 'cancelled')),
    CHECK (continuation_of IS NULL OR continuation_of <> job_id)
)""",
    """CREATE INDEX translation_jobs_by_project_state
    ON translation_jobs(project_id, state, created_at, job_id)""",
    """CREATE TRIGGER translation_jobs_immutable BEFORE UPDATE ON translation_jobs
WHEN OLD.state IN ('validated', 'partially_completed', 'failed', 'cancelled')
    OR NEW.job_id IS NOT OLD.job_id
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.command_type IS NOT OLD.command_type
    OR NEW.command_version IS NOT OLD.command_version
    OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
    OR NEW.output_roles IS NOT OLD.output_roles
    OR NEW.created_at IS NOT OLD.created_at
    OR NEW.continuation_of IS NOT OLD.continuation_of
    OR NEW.revision < OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'immutable_job');
END""",
)

_COLUMNS = ("job_id, project_id, command_type, command_version, input_fingerprint, output_roles, "
            "state, revision, created_at, updated_at, lease_owner_token, lease_acquired_at, "
            "lease_expires_at, terminal_summary, continuation_of")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


def _new_token() -> str:
    return secrets.token_hex(32)


def _encode_time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _encode_summary(summary: JobTerminalSummary | None) -> str | None:
    if summary is None:
        return None
    # Pairs, not an object: counter order must survive the round trip exactly.
    return _encode_json({"counters": [list(entry) for entry in summary.counters],
                         "reason_code": summary.reason_code})


def _decode_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    return datetime.fromisoformat(value)


def _decode_summary(value: str | None) -> JobTerminalSummary | None:
    if value is None:
        return None
    data = json.loads(value)
    if type(data) is not dict or set(data) != {"counters", "reason_code"} or type(data["counters"]) is not list:
        raise ValueError
    return JobTerminalSummary(data["reason_code"], tuple(tuple(entry) for entry in data["counters"]))


def _decode(row: tuple) -> TranslationJob:
    (job_id, project_id, command_type, command_version, input_fingerprint, output_roles, state, revision,
     created_at, updated_at, lease_owner_token, lease_acquired_at, lease_expires_at, summary,
     continuation_of) = row
    try:
        roles = json.loads(output_roles)
        if type(roles) is not list:
            raise ValueError
        lease = None
        if lease_owner_token is not None:
            lease = JobLease(lease_owner_token, _decode_time(lease_acquired_at), _decode_time(lease_expires_at))
        return TranslationJob(
            job_id=job_id,
            request=JobRequest(project_id, command_type, command_version, input_fingerprint, tuple(roles)),
            state=JobState(state), revision=revision,
            created_at=_decode_time(created_at), updated_at=_decode_time(updated_at),
            lease=lease, terminal_summary=_decode_summary(summary), continuation_of=continuation_of)
    except (ValueError, TypeError):
        # JobError is a ValueError: a stored row that fails validation is corrupt data.
        raise JobStoreError("invalid_job_record") from None


def load_job(connection: sqlite3.Connection, job_id: str) -> TranslationJob:
    """Read one job inside a caller's transaction on the project database."""
    row = connection.execute(f"SELECT {_COLUMNS} FROM translation_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFoundError("job_not_found")
    return _decode(row)


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except JobError:
        raise
    except MigrationError as error:
        raise JobStoreError(error.code) from None
    except sqlite3.Error as error:
        raise JobStoreError(sqlite_failure_code(error)) from None
    except PermissionError:
        raise JobStoreError("permission") from None
    except OSError:
        raise JobStoreError("io") from None


class SqliteJobRepository:
    """Jobs of one project. The database must already be at the runner's target schema."""

    def __init__(self, runner: SqliteMigrationRunner, database: Path, project_id: str, *,
                 clock: Callable[[], datetime] = _utc_now, new_id: Callable[[], str] = _new_id,
                 new_token: Callable[[], str] = _new_token) -> None:
        validate_job_identity(project_id)
        self._runner = runner
        self._database = database
        self._project_id = project_id
        self._clock = clock
        self._new_id = new_id
        self._new_token = new_token

    def __repr__(self) -> str:
        return f"SqliteJobRepository(project_id={self._project_id!r})"

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
    def _load(connection: sqlite3.Connection, job_id: str) -> TranslationJob:
        return load_job(connection, job_id)

    @staticmethod
    def _insert(connection: sqlite3.Connection, job: TranslationJob) -> None:
        request = job.request
        connection.execute(
            f"INSERT INTO translation_jobs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
            (job.job_id, request.project_id, request.command_type, request.command_version,
             request.input_fingerprint, _encode_json(list(request.output_roles)), job.state.value, job.revision,
             _encode_time(job.created_at), _encode_time(job.updated_at), job.continuation_of))

    @staticmethod
    def _update(connection: sqlite3.Connection, before: TranslationJob, after: TranslationJob) -> None:
        lease = after.lease
        cursor = connection.execute(
            "UPDATE translation_jobs SET state = ?, revision = ?, updated_at = ?, lease_owner_token = ?, "
            "lease_acquired_at = ?, lease_expires_at = ?, terminal_summary = ? WHERE job_id = ? AND revision = ?",
            (after.state.value, after.revision, _encode_time(after.updated_at),
             lease.owner_token if lease else None, _encode_time(lease.acquired_at) if lease else None,
             _encode_time(lease.expires_at) if lease else None, _encode_summary(after.terminal_summary),
             before.job_id, before.revision))
        if cursor.rowcount != 1:
            raise JobRevisionConflictError("stale_revision")

    def _snapshot(self, job: TranslationJob) -> JobSnapshot:
        return JobSnapshot(job, requires_reconciliation(job, self._clock()))

    def _check_project(self, project_id: str) -> None:
        validate_job_identity(project_id)
        if project_id != self._project_id:
            raise JobValidationError("project_mismatch")

    def create(self, request: JobRequest) -> TranslationJob:
        self._check_project(request.project_id)
        with self._write() as connection:
            job = new_job(request, self._new_id(), self._clock())
            self._insert(connection, job)
            return job

    def get(self, job_id: str) -> JobSnapshot:
        validate_job_identity(job_id)
        with self._read() as connection:
            return self._snapshot(self._load(connection, job_id))

    def list_jobs(self, project_id: str, *, states: tuple[JobState, ...] | None = None,
                  limit: int = 256) -> tuple[JobSnapshot, ...]:
        """At most ``limit`` jobs, oldest first."""
        self._check_project(project_id)
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise JobValidationError("invalid_limit")
        if states is not None and (type(states) is not tuple or any(type(state) is not JobState for state in states)):
            raise JobValidationError("unknown_state")
        query = f"SELECT {_COLUMNS} FROM translation_jobs WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if states is not None:
            query += f" AND state IN ({', '.join('?' for _ in states)})"
            parameters.extend(state.value for state in states)
        query += " ORDER BY created_at, job_id LIMIT ?"
        parameters.append(limit)
        with self._read() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return tuple(self._snapshot(_decode(row)) for row in rows)

    def transition(self, job_id: str, target: JobState, *, expected_revision: int,
                   summary: JobTerminalSummary | None = None) -> TranslationJob:
        validate_job_identity(job_id)
        with self._write() as connection:
            job = self._load(connection, job_id)
            updated = transition_job(job, target, expected_revision=expected_revision,
                                     now=self._clock(), summary=summary)
            if updated is not job:
                self._update(connection, job, updated)
            return updated

    def _change_lease(self, job_id: str, change: Callable[[TranslationJob, datetime], TranslationJob]) -> TranslationJob:
        validate_job_identity(job_id)
        with self._write() as connection:
            job = self._load(connection, job_id)
            updated = change(job, self._clock())
            self._update(connection, job, updated)
            return updated

    def acquire_lease(self, job_id: str, duration: timedelta) -> TranslationJob:
        return self._change_lease(job_id, lambda job, now: acquire_lease(job, self._new_token(), now, duration))

    def renew_lease(self, job_id: str, owner_token: str, duration: timedelta) -> TranslationJob:
        return self._change_lease(job_id, lambda job, now: renew_lease(job, owner_token, now, duration))

    def release_lease(self, job_id: str, owner_token: str) -> TranslationJob:
        return self._change_lease(job_id, lambda job, now: release_lease(job, owner_token, now))

    def create_continuation(self, terminal_job_id: str, request: JobRequest) -> TranslationJob:
        """Link from the new job: the terminal record is immutable and stays untouched."""
        validate_job_identity(terminal_job_id)
        self._check_project(request.project_id)
        with self._write() as connection:
            parent = self._load(connection, terminal_job_id)
            if not parent.terminal:
                raise JobContinuationError("job_not_terminal")
            existing = connection.execute("SELECT 1 FROM translation_jobs WHERE continuation_of = ?",
                                          (terminal_job_id,)).fetchone()
            if existing is not None:
                raise JobContinuationError("continuation_exists")
            job = new_job(request, self._new_id(), self._clock(), continuation_of=terminal_job_id)
            self._insert(connection, job)
            return job

    def get_continuation(self, job_id: str) -> JobSnapshot | None:
        validate_job_identity(job_id)
        with self._read() as connection:
            self._load(connection, job_id)
            row = connection.execute(f"SELECT {_COLUMNS} FROM translation_jobs WHERE continuation_of = ?",
                                     (job_id,)).fetchone()
            return None if row is None else self._snapshot(_decode(row))
