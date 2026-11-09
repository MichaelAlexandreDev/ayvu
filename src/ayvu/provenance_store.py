"""SQLite persistence for the provenance records of one project.

Records are immutable. Each write is one short transaction that checks every
reference before inserting anything, so a rejected record leaves no partial row.
A new record can only reference records that already exist, which is why the
write path cannot create a cycle; reads still detect one in a tampered database.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator
from uuid import uuid4

from .job import JobError, JobNotFoundError
from .job_store import load_job
from .migration_contracts import MigrationError
from .provenance import (
    ProvenanceConflictError, ProvenanceError, ProvenanceLimitError, ProvenanceNotFoundError, ProvenanceRecord,
    ProvenanceReferenceError, ProvenanceRequest, ProvenanceStoreError, ProvenanceValidationError, SubjectRef,
    decode_record, encode_record, request_identity, validate_identity,
)
from .provenance_service import ArtifactLineage, JobInput, LineageNode, ProvenanceGraph
from .sqlite_migrations import SqliteMigrationRunner, sqlite_failure_code

MAX_LIST_LIMIT = 1024
MAX_TRAVERSAL_DEPTH = 256
MAX_TRAVERSAL_RECORDS = 4096

# Frozen once released: the migration ledger checksums this exact text.
PROVENANCE_SCHEMA_STATEMENTS = (
    """CREATE TABLE provenance_records (
    record_id TEXT PRIMARY KEY NOT NULL,
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES translation_jobs(job_id) ON DELETE RESTRICT,
    subject_namespace TEXT,
    subject_version INTEGER,
    subject_value TEXT,
    idempotency_key TEXT,
    supersedes TEXT UNIQUE REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    CHECK ((subject_namespace IS NULL) = (subject_value IS NULL)
           AND (subject_namespace IS NULL) = (subject_version IS NULL)),
    CHECK (supersedes IS NULL OR supersedes <> record_id)
)""",
    """CREATE UNIQUE INDEX provenance_records_idempotency
    ON provenance_records(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL""",
    """CREATE INDEX provenance_records_by_job ON provenance_records(project_id, job_id, recorded_at, record_id)""",
    """CREATE INDEX provenance_records_by_subject
    ON provenance_records(project_id, subject_namespace, subject_version, subject_value, recorded_at, record_id)""",
    """CREATE TABLE provenance_parents (
    record_id TEXT NOT NULL REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    parent_id TEXT NOT NULL REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    PRIMARY KEY (record_id, parent_id),
    CHECK (record_id <> parent_id)
) WITHOUT ROWID""",
    """CREATE TABLE provenance_artifacts (
    record_id TEXT NOT NULL REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES job_artifacts(artifact_id) ON DELETE RESTRICT,
    PRIMARY KEY (record_id, artifact_id)
) WITHOUT ROWID""",
    """CREATE INDEX provenance_artifacts_by_artifact ON provenance_artifacts(artifact_id, record_id)""",
    *(f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END""" for table in ("provenance_records", "provenance_parents", "provenance_artifacts")
      for action in ("UPDATE", "DELETE")),
)

_COLUMNS = "record_id, project_id, job_id, supersedes, payload"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


def _decode(row: tuple) -> ProvenanceRecord:
    record_id, project_id, job_id, supersedes, payload = row
    try:
        record = decode_record(payload)
    except ProvenanceValidationError as error:
        if str(error) == "unsupported_provenance_version":
            raise
        raise ProvenanceStoreError("invalid_provenance_record") from None
    request = record.request
    if (record.record_id, request.project_id, request.job_id, request.supersedes) != (
            record_id, project_id, job_id, supersedes):
        raise ProvenanceStoreError("invalid_provenance_record")
    return record


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except ProvenanceError:
        raise
    except JobNotFoundError:
        raise ProvenanceReferenceError("missing_job") from None
    except JobError as error:
        raise ProvenanceStoreError(str(error)) from None
    except MigrationError as error:
        raise ProvenanceStoreError(error.code) from None
    except sqlite3.Error as error:
        raise ProvenanceStoreError(sqlite_failure_code(error)) from None
    except PermissionError:
        raise ProvenanceStoreError("permission") from None
    except OSError:
        raise ProvenanceStoreError("io") from None


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ProvenanceLimitError("invalid_limit")


def _validate_traversal(max_depth: int, max_records: int) -> None:
    if (type(max_depth) is not int or not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH
            or type(max_records) is not int or not 1 <= max_records <= MAX_TRAVERSAL_RECORDS):
        raise ProvenanceLimitError("invalid_traversal_limit")


def _neighbors(record: ProvenanceRecord) -> tuple[str, ...]:
    request = record.request
    return tuple(sorted({*request.parents, *((request.supersedes,) if request.supersedes else ())}))


def _reject_cycles(records: dict[str, ProvenanceRecord]) -> None:
    """Kahn's algorithm over the collected records: anything left over is on a cycle."""
    remaining = {record_id: sum(item in records for item in _neighbors(record)) for record_id, record in records.items()}
    dependents: dict[str, list[str]] = {}
    for record_id, record in records.items():
        for neighbor in _neighbors(record):
            if neighbor in records:
                dependents.setdefault(neighbor, []).append(record_id)
    ready = [record_id for record_id, count in remaining.items() if count == 0]
    resolved = 0
    while ready:
        resolved += 1
        for dependent in dependents.get(ready.pop(), ()):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if resolved != len(records):
        raise ProvenanceStoreError("lineage_cycle")


class SqliteProvenanceRepository:
    """Records of one project. The database must already be at the runner's target schema."""

    def __init__(self, runner: SqliteMigrationRunner, database: Path, project_id: str, *,
                 clock: Callable[[], datetime] = _utc_now, new_id: Callable[[], str] = _new_id) -> None:
        validate_identity(project_id)
        self._runner = runner
        self._database = database
        self._project_id = project_id
        self._clock = clock
        self._new_id = new_id

    def __repr__(self) -> str:
        return f"SqliteProvenanceRepository(project_id={self._project_id!r})"

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with _translated_errors(), closing(self._runner.connect(self._database, readonly=True)) as connection:
            # One snapshot, so a traversal cannot observe a record without its parents.
            connection.execute("BEGIN")
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

    def _load(self, connection: sqlite3.Connection, record_id: str) -> ProvenanceRecord | None:
        row = connection.execute(f"SELECT {_COLUMNS} FROM provenance_records WHERE record_id = ? AND project_id = ?",
                                 (record_id, self._project_id)).fetchone()
        return None if row is None else _decode(row)

    def _node(self, connection: sqlite3.Connection, record: ProvenanceRecord) -> LineageNode:
        row = connection.execute("SELECT record_id FROM provenance_records WHERE supersedes = ?",
                                 (record.record_id,)).fetchone()
        return LineageNode(record, None if row is None else row[0])

    def _require_reference(self, connection: sqlite3.Connection, record_id: str, missing: str) -> ProvenanceRecord:
        row = connection.execute(f"SELECT {_COLUMNS} FROM provenance_records WHERE record_id = ?",
                                 (record_id,)).fetchone()
        if row is None:
            raise ProvenanceReferenceError(missing)
        if row[1] != self._project_id:
            raise ProvenanceReferenceError("cross_project_reference")
        return _decode(row)

    def _check_references(self, connection: sqlite3.Connection, request: ProvenanceRequest) -> None:
        if load_job(connection, request.job_id).request.project_id != self._project_id:
            raise ProvenanceReferenceError("cross_project_reference")
        for parent in request.parents:
            self._require_reference(connection, parent, "missing_parent")
        if request.supersedes is not None:
            target = self._require_reference(connection, request.supersedes, "missing_superseded_record")
            if target.request.subject != request.subject:
                raise ProvenanceConflictError("supersession_subject_mismatch")
            if connection.execute("SELECT 1 FROM provenance_records WHERE supersedes = ?",
                                  (request.supersedes,)).fetchone() is not None:
                raise ProvenanceConflictError("already_superseded")
        for artifact in request.artifacts:
            row = connection.execute("SELECT project_id FROM job_artifacts WHERE artifact_id = ?",
                                     (artifact,)).fetchone()
            if row is None:
                raise ProvenanceReferenceError("missing_artifact")
            if row[0] != self._project_id:
                raise ProvenanceReferenceError("cross_project_reference")

    def append(self, request: ProvenanceRequest) -> ProvenanceRecord:
        """Idempotent for an exact repeat of a key; any other use of that key fails."""
        if type(request) is not ProvenanceRequest:
            raise ProvenanceValidationError("invalid_request")
        if request.project_id != self._project_id:
            raise ProvenanceReferenceError("cross_project_reference")
        with self._write() as connection:
            if request.idempotency_key is not None:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM provenance_records WHERE project_id = ? AND idempotency_key = ?",
                    (self._project_id, request.idempotency_key)).fetchone()
                if row is not None:
                    existing = _decode(row)
                    if request_identity(existing.request) != request_identity(request):
                        raise ProvenanceConflictError("idempotency_conflict")
                    return existing
            self._check_references(connection, request)
            record = ProvenanceRecord(self._new_id(), self._clock(), request)
            subject = request.subject
            connection.execute(
                "INSERT INTO provenance_records (record_id, schema_version, project_id, job_id, subject_namespace, "
                "subject_version, subject_value, idempotency_key, supersedes, recorded_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.record_id, record.schema_version, request.project_id, request.job_id,
                 subject.namespace if subject else None, subject.version if subject else None,
                 subject.value if subject else None, request.idempotency_key, request.supersedes,
                 record.recorded_at.isoformat(timespec="microseconds"), encode_record(record)))
            connection.executemany("INSERT INTO provenance_parents (record_id, parent_id) VALUES (?, ?)",
                                   [(record.record_id, parent) for parent in request.parents])
            connection.executemany("INSERT INTO provenance_artifacts (record_id, artifact_id) VALUES (?, ?)",
                                   [(record.record_id, artifact) for artifact in request.artifacts])
            return record

    def get(self, record_id: str) -> LineageNode:
        validate_identity(record_id)
        with self._read() as connection:
            record = self._load(connection, record_id)
            if record is None:
                raise ProvenanceNotFoundError("record_not_found")
            return self._node(connection, record)

    def _page(self, connection: sqlite3.Connection, rows: list[tuple], limit: int) -> ProvenanceGraph:
        nodes = tuple(self._node(connection, _decode(row)) for row in rows[:limit])
        return ProvenanceGraph(nodes, len(rows) > limit)

    def list_by_job(self, job_id: str, *, limit: int) -> ProvenanceGraph:
        validate_identity(job_id)
        _validate_limit(limit)
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM provenance_records WHERE project_id = ? AND job_id = ? "
                "ORDER BY recorded_at, record_id LIMIT ?", (self._project_id, job_id, limit + 1)).fetchall()
            return self._page(connection, rows, limit)

    def list_by_subject(self, subject: SubjectRef, *, limit: int) -> ProvenanceGraph:
        if type(subject) is not SubjectRef:
            raise ProvenanceValidationError("invalid_subject")
        _validate_limit(limit)
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM provenance_records WHERE project_id = ? AND subject_namespace = ? "
                "AND subject_version = ? AND subject_value = ? ORDER BY recorded_at, record_id LIMIT ?",
                (self._project_id, subject.namespace, subject.version, subject.value, limit + 1)).fetchall()
            return self._page(connection, rows, limit)

    def _artifact_rows(self, connection: sqlite3.Connection, artifact_id: str, limit: int) -> list[tuple]:
        columns = ", ".join(f"records.{name.strip()}" for name in _COLUMNS.split(","))
        return connection.execute(
            f"SELECT {columns} FROM provenance_artifacts AS links "
            "JOIN provenance_records AS records ON records.record_id = links.record_id "
            "WHERE links.artifact_id = ? AND records.project_id = ? "
            "ORDER BY records.recorded_at, records.record_id LIMIT ?",
            (artifact_id, self._project_id, limit)).fetchall()

    def list_by_artifact(self, artifact_id: str, *, limit: int) -> ProvenanceGraph:
        validate_identity(artifact_id)
        _validate_limit(limit)
        with self._read() as connection:
            return self._page(connection, self._artifact_rows(connection, artifact_id, limit + 1), limit)

    def _walk(self, connection: sqlite3.Connection, starts: Iterable[str], max_depth: int,
              max_records: int) -> ProvenanceGraph:
        """Breadth first toward the origins, in a fixed order, so the same bound gives the same result."""
        records: dict[str, ProvenanceRecord] = {}
        frontier = sorted(set(starts))
        depth = 0
        truncated = False
        while frontier:
            following: set[str] = set()
            for record_id in frontier:
                if len(records) >= max_records:
                    truncated = True
                    break
                record = self._load(connection, record_id)
                if record is None:
                    raise ProvenanceStoreError("invalid_provenance_record")
                records[record_id] = record
                following.update(_neighbors(record))
            following -= records.keys()
            if truncated:
                break
            if depth == max_depth and following:
                truncated = True
                break
            depth += 1
            frontier = sorted(following)
        _reject_cycles(records)
        return ProvenanceGraph(tuple(self._node(connection, record) for record in records.values()), truncated)

    def lineage(self, record_id: str, *, max_depth: int, max_records: int) -> ProvenanceGraph:
        """The record and what led to it. Corrected records are included and marked as historical."""
        validate_identity(record_id)
        _validate_traversal(max_depth, max_records)
        with self._read() as connection:
            if self._load(connection, record_id) is None:
                raise ProvenanceNotFoundError("record_not_found")
            return self._walk(connection, (record_id,), max_depth, max_records)

    def trace_artifact(self, artifact_id: str, *, max_depth: int, max_records: int) -> ArtifactLineage:
        validate_identity(artifact_id)
        _validate_traversal(max_depth, max_records)
        with self._read() as connection:
            row = connection.execute("SELECT project_id FROM job_artifacts WHERE artifact_id = ?",
                                     (artifact_id,)).fetchone()
            if row is None or row[0] != self._project_id:
                raise ProvenanceNotFoundError("artifact_not_found")
            producers = self._artifact_rows(connection, artifact_id, max_records + 1)
            graph = self._walk(connection, (producer[0] for producer in producers[:max_records]), max_depth, max_records)
            job_ids = sorted({node.record.request.job_id for node in graph.nodes})
            jobs = tuple(JobInput(job_id, load_job(connection, job_id).request.input_fingerprint) for job_id in job_ids)
            truncated = graph.truncated or len(producers) > max_records
            return ArtifactLineage(artifact_id, tuple(producer[0] for producer in producers[:max_records]),
                                   ProvenanceGraph(graph.nodes, truncated), jobs)
