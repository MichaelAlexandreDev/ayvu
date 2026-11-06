"""Dedicated local governed TM database, independent of both cache schemas.

The caller supplies the trusted project directory and explicitly authorized
scope/provenance repositories. No cache is opened, attached, scanned or copied.
Lifecycle facts and values are append-only; only the current-approval index is
replaceable. Transactions serialize optimistic approval and idempotent replay.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping
from uuid import uuid4

from .approved_memory import (
    Candidate, MemoryConflictError, MemoryError, MemoryEvent, MemoryNotFoundError, MemoryPage,
    MemoryProvenanceError, MemoryRevision, MemoryScope, MemoryScopeError, MemorySource, MemoryState,
    MemoryStorageError, MemoryTransitionError, MemoryValidationError, MemoryValueError, PipelinePin,
    PipelineStage, ScopeKind, TargetValue, ValueKind, identity, require_transition, validate_pair,
)
from .approved_memory_service import CreateCandidate, DeriveRevision, ReviewRevision
from .domain import LanguagePair
from .migration_contracts import MigrationError
from .provenance import ActorKind, ConfigurationRef, ProvenanceError, ProvenanceStoreError, SubjectRef
from .provenance_service import ProvenanceRepository
from .sqlite_migrations import MAX_DATABASE_BYTES, SqliteMigration, SqliteMigrationRunner, sqlite_failure_code

MEMORY_SCHEMA_STATEMENTS = (
    """CREATE TABLE tm_revisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL, revision_id TEXT UNIQUE NOT NULL, revision_number INTEGER NOT NULL,
    parent_revision TEXT REFERENCES tm_revisions(revision_id) ON DELETE RESTRICT,
    source_project TEXT NOT NULL, document_id TEXT NOT NULL, document_revision TEXT NOT NULL,
    unit_id TEXT NOT NULL, content_id TEXT NOT NULL,
    canonical_source TEXT NOT NULL CHECK(length(CAST(canonical_source AS BLOB)) <= 65536),
    search_key TEXT NOT NULL, normalization_version TEXT NOT NULL, search_version TEXT NOT NULL,
    source_lang TEXT NOT NULL, target_lang TEXT NOT NULL,
    target_value TEXT NOT NULL CHECK(length(CAST(target_value AS BLOB)) <= 65536),
    value_kind TEXT NOT NULL, pipeline_stage TEXT NOT NULL,
    pipeline_ref TEXT NOT NULL, profile_ref TEXT NOT NULL, glossary_ref TEXT NOT NULL,
    scope_kind TEXT NOT NULL, scope_owner TEXT NOT NULL,
    provenance_id TEXT NOT NULL, import_origin TEXT,
    approval_slot TEXT NOT NULL,
    UNIQUE(entry_id, revision_number)
)""",
    """CREATE INDEX tm_revisions_scope ON tm_revisions(scope_kind, scope_owner, sequence)""",
    """CREATE TABLE tm_events (
    revision_id TEXT NOT NULL REFERENCES tm_revisions(revision_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL, state TEXT NOT NULL, provenance_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY(revision_id, version)
) WITHOUT ROWID""",
    """CREATE TABLE tm_approvals (
    approval_slot TEXT PRIMARY KEY NOT NULL,
    revision_id TEXT UNIQUE NOT NULL REFERENCES tm_revisions(revision_id) ON DELETE RESTRICT
) WITHOUT ROWID""",
    """CREATE TABLE tm_operations (
    operation_id TEXT PRIMARY KEY NOT NULL, command_hash TEXT NOT NULL,
    revision_id TEXT NOT NULL, version INTEGER NOT NULL,
    FOREIGN KEY(revision_id, version) REFERENCES tm_events(revision_id, version) ON DELETE RESTRICT
) WITHOUT ROWID""",
    *(f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
BEGIN SELECT RAISE(ABORT, 'immutable_memory'); END"""
      for table in ("tm_revisions", "tm_events", "tm_operations") for action in ("UPDATE", "DELETE")),
)

# This namespace has its own history. Released project/cache steps stay frozen.
MEMORY_MIGRATIONS = (SqliteMigration(1), SqliteMigration(2, MEMORY_SCHEMA_STATEMENTS))


def memory_migration_runner() -> SqliteMigrationRunner:
    return SqliteMigrationRunner(MEMORY_MIGRATIONS)


def open_approved_memory(project_directory: Path, *, allowed_scopes: frozenset[MemoryScope],
                         provenance: Mapping[str, ProvenanceRepository]) -> SqliteApprovedMemoryRepository:
    """Use the directory resolved by Projects, alongside project.sqlite, never its cache."""
    runner = memory_migration_runner()
    database = project_directory / "approved-memory.sqlite"
    result = runner.migrate(database, project_directory / "backups" / "approved-memory-schema")
    if not result.ok:
        raise MemoryStorageError(result.error or "io")
    return SqliteApprovedMemoryRepository(runner, database, allowed_scopes=allowed_scopes, provenance=provenance)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def _slot(value: Candidate) -> str:
    source = value.source
    return _digest([value.scope.kind, value.scope.owner_id, source.project_id, source.unit_id,
                    source.content_id, value.languages.source, value.languages.target, value.target.stage])


@contextmanager
def _errors(*, write=False):
    try:
        yield
    except MemoryError:
        raise
    except MigrationError as error:
        if write and error.code == 'busy':
            raise MemoryConflictError('concurrent_write') from None
        raise MemoryStorageError(error.code) from None
    except sqlite3.Error as error:
        code = sqlite_failure_code(error)
        if write and code == 'busy':
            raise MemoryConflictError('concurrent_write') from None
        raise MemoryStorageError(code) from None
    except (OSError, ValueError, TypeError, KeyError):
        raise MemoryStorageError("invalid_memory_store") from None


def _bounds(limit: int, after: int) -> None:
    if (type(limit) is not int or not 1 <= limit <= 256
            or type(after) is not int or not 0 <= after < 2**63):
        raise MemoryValidationError("invalid_page")


class SqliteApprovedMemoryRepository:
    def __init__(self, runner: SqliteMigrationRunner, database: Path, *, allowed_scopes: frozenset[MemoryScope],
                 provenance: Mapping[str, ProvenanceRepository]) -> None:
        if (type(allowed_scopes) is not frozenset or not allowed_scopes or len(allowed_scopes) > 32
                or any(type(scope) is not MemoryScope for scope in allowed_scopes)):
            raise MemoryScopeError("invalid_allowed_scopes")
        self._runner = runner
        self._database = database
        self._allowed = allowed_scopes
        self._provenance = dict(provenance)

    def __repr__(self) -> str:
        return "SqliteApprovedMemoryRepository(<private>)"

    def _scopes(self, scopes: tuple[MemoryScope, ...]) -> None:
        if (type(scopes) is not tuple or not scopes or len(scopes) > 32
                or any(type(scope) is not MemoryScope for scope in scopes) or len(set(scopes)) != len(scopes)):
            raise MemoryScopeError("invalid_query_scopes")
        if not set(scopes) <= self._allowed:
            raise MemoryScopeError("scope_denied")

    @contextmanager
    def _transaction(self, *, write=False):
        with _errors(write=write), closing(self._runner.connect(self._database, readonly=not write)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                if write:
                    if (connection.execute('PRAGMA page_count').fetchone()[0]
                            * connection.execute('PRAGMA page_size').fetchone()[0] > MAX_DATABASE_BYTES):
                        raise MemoryStorageError('resource_limit')
                    connection.execute("COMMIT")
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")

    def _load(self, db, revision_id, scopes, version=None) -> MemoryRevision:
        row = db.execute("SELECT * FROM tm_revisions WHERE revision_id = ?", (revision_id,)).fetchone()
        if row is None:
            raise MemoryNotFoundError("revision_not_found")
        scope = MemoryScope(ScopeKind(row['scope_kind']), row['scope_owner'])
        if scope not in scopes:
            raise MemoryScopeError("scope_denied")
        source = MemorySource(*(row[name] for name in (
            'source_project', 'document_id', 'document_revision', 'unit_id', 'content_id', 'canonical_source',
            'search_key', 'normalization_version', 'search_version')))
        pin = PipelinePin(*(ConfigurationRef(kind, row[kind + '_ref']) for kind in ('pipeline', 'profile', 'glossary')))
        target = TargetValue(row['target_value'], ValueKind(row['value_kind']), PipelineStage(row['pipeline_stage']), pin)
        value = Candidate(source, LanguagePair(row['source_lang'], row['target_lang']), target, scope,
                          row['provenance_id'], row['import_origin'])
        event = db.execute("SELECT * FROM tm_events WHERE revision_id = ? "
                           + ("ORDER BY version DESC LIMIT 1" if version is None else "AND version = ?"),
                           (revision_id,) if version is None else (revision_id, version)).fetchone()
        if event is None:
            raise MemoryStorageError("missing_lifecycle")
        return MemoryRevision(row['sequence'], row['entry_id'], revision_id, row['revision_number'],
                              row['parent_revision'], value, self._event(event))

    @staticmethod
    def _event(row) -> MemoryEvent:
        return MemoryEvent(row['version'], MemoryState(row['state']), row['provenance_id'],
                           datetime.fromisoformat(row['recorded_at']))

    def _evidence(self, value, provenance_id, operation_id, operation, *, human=False, parents=()):
        try:
            node = self._provenance[value.source.project_id].get(provenance_id)
        except ProvenanceStoreError:
            raise MemoryStorageError("provenance_storage_failed") from None
        except (KeyError, ProvenanceError):
            raise MemoryProvenanceError("missing_provenance") from None
        record, request = node.record, node.record.request
        if (not node.current or request.project_id != value.source.project_id or request.operation != operation
                or request.operation_id != operation_id
                or request.subject != SubjectRef('content_unit', 1, value.source.unit_id)
                or (human and request.actor_kind is not ActorKind.HUMAN)
                or not set(parents) <= set(request.parents)
                or not set(value.target.pin.references) <= set(request.configuration_refs)):
            raise MemoryProvenanceError("incompatible_provenance")
        if value.import_origin is not None:
            try:
                origin = self._provenance[value.source.project_id].get(value.import_origin)
            except ProvenanceStoreError:
                raise MemoryStorageError("provenance_storage_failed") from None
            except ProvenanceError:
                raise MemoryProvenanceError("missing_import_origin") from None
            if (origin.record.request.project_id != value.source.project_id
                    or origin.record.request.operation != 'tm_import'
                    or origin.record.request.actor_kind is not ActorKind.HUMAN
                    or value.import_origin not in request.parents):
                raise MemoryProvenanceError("incompatible_import_origin")
        return record

    def _replay(self, db, command):
        identity(command.operation_id)
        fingerprint = _digest([type(command).__name__, asdict(command)])
        row = db.execute("SELECT * FROM tm_operations WHERE operation_id = ?", (command.operation_id,)).fetchone()
        if row is None:
            return fingerprint, None
        if row['command_hash'] != fingerprint:
            raise MemoryConflictError("operation_conflict")
        return fingerprint, self._load(db, row['revision_id'], self._allowed, row['version'])

    @staticmethod
    def _remember(db, command, fingerprint, revision):
        db.execute("INSERT INTO tm_operations VALUES (?, ?, ?, ?)",
                   (command.operation_id, fingerprint, revision.revision_id, revision.event.version))
        return revision

    @staticmethod
    def _append(db, revision_id, version, state, record):
        db.execute("INSERT INTO tm_events VALUES (?, ?, ?, ?, ?)",
                   (revision_id, version, state.value, record.record_id,
                    record.recorded_at.isoformat(timespec='microseconds')))

    def _insert(self, db, value, record, *, parent=None, human_edit=False):
        entry_id = parent.entry_id if parent else str(uuid4())
        revision_id = str(uuid4())
        number = db.execute("SELECT COALESCE(MAX(revision_number), 0) + 1 FROM tm_revisions WHERE entry_id = ?",
                            (entry_id,)).fetchone()[0]
        source, target = value.source, value.target
        parameters = (entry_id, revision_id, number, parent.revision_id if parent else None,
                      source.project_id, source.document_id, source.document_revision, source.unit_id, source.content_id,
                      source.canonical_text, source.search_key, source.normalization_version, source.search_version,
                      value.languages.source, value.languages.target, target.text, target.kind.value, target.stage.value,
                      *(ref.reference for ref in target.pin.references), value.scope.kind.value, value.scope.owner_id,
                      value.provenance_id, value.import_origin, _slot(value))
        db.execute("INSERT INTO tm_revisions (entry_id, revision_id, revision_number, parent_revision, source_project, "
                   "document_id, document_revision, unit_id, content_id, canonical_source, search_key, "
                   "normalization_version, search_version, source_lang, target_lang, target_value, value_kind, "
                   "pipeline_stage, pipeline_ref, profile_ref, glossary_ref, scope_kind, scope_owner, provenance_id, "
                   "import_origin, approval_slot) VALUES (" + ','.join('?' for _ in parameters) + ')', parameters)
        self._append(db, revision_id, 1, MemoryState.MACHINE_CANDIDATE, record)
        if human_edit:
            require_transition(MemoryState.MACHINE_CANDIDATE, MemoryState.HUMAN_EDITED)
            self._append(db, revision_id, 2, MemoryState.HUMAN_EDITED, record)
        return self._load(db, revision_id, self._allowed)

    @staticmethod
    def _expected(revision, expected):
        if type(expected) is not int or expected != revision.event.version:
            raise MemoryConflictError("stale_revision")

    def create(self, command: CreateCandidate) -> MemoryRevision:
        self._scopes((command.value.scope,))
        with self._transaction(write=True) as db:
            fingerprint, replay = self._replay(db, command)
            if replay is not None:
                return replay
            record = self._evidence(command.value, command.value.provenance_id, command.operation_id, 'tm_create')
            revision = self._insert(db, command.value, record)
            return self._remember(db, command, fingerprint, revision)

    def derive(self, command: DeriveRevision) -> MemoryRevision:
        identity(command.parent_revision)
        self._scopes((command.value.scope,))
        if type(command.human_edit) is not bool:
            raise MemoryValidationError("invalid_edit_action")
        with self._transaction(write=True) as db:
            fingerprint, replay = self._replay(db, command)
            if replay is not None:
                return replay
            parent = self._load(db, command.parent_revision, self._allowed)
            self._expected(parent, command.expected_version)
            if (parent.value.source != command.value.source or parent.value.languages != command.value.languages):
                raise MemoryConflictError("source_identity_changed")
            record = self._evidence(command.value, command.value.provenance_id, command.operation_id,
                                    'tm_edit' if command.human_edit else 'tm_derive', human=command.human_edit,
                                    parents=(parent.event.provenance_id,))
            revision = self._insert(db, command.value, record, parent=parent, human_edit=command.human_edit)
            return self._remember(db, command, fingerprint, revision)

    def review(self, command: ReviewRevision) -> MemoryRevision:
        identity(command.revision_id)
        identity(command.provenance_id)
        self._scopes((command.scope,))
        if command.expected_approval is not None:
            identity(command.expected_approval)
        if (type(command.state) is not MemoryState
                or command.state not in {MemoryState.APPROVED, MemoryState.REJECTED, MemoryState.DEPRECATED}):
            raise MemoryTransitionError("invalid_review_action")
        with self._transaction(write=True) as db:
            fingerprint, replay = self._replay(db, command)
            if replay is not None:
                return replay
            revision = self._load(db, command.revision_id, (command.scope,))
            self._expected(revision, command.expected_version)
            require_transition(revision.state, command.state)
            prior = None
            parents = [revision.event.provenance_id]
            if command.state is MemoryState.APPROVED:
                revision.value.target.require_approval_eligible()
                if command.reviewed_value != revision.value.target:
                    raise MemoryValueError("reviewed_value_mismatch")
                row = db.execute("SELECT revision_id FROM tm_approvals WHERE approval_slot = ?",
                                 (_slot(revision.value),)).fetchone()
                current_id = row[0] if row else None
                if current_id != command.expected_approval:
                    raise MemoryConflictError("stale_approval")
                if current_id is not None:
                    prior = self._load(db, current_id, (command.scope,))
                    require_transition(prior.state, MemoryState.SUPERSEDED)
                    parents.append(prior.event.provenance_id)
            operation = {MemoryState.APPROVED: 'tm_approve', MemoryState.REJECTED: 'tm_reject',
                         MemoryState.DEPRECATED: 'tm_deprecate'}[command.state]
            record = self._evidence(revision.value, command.provenance_id, command.operation_id,
                                    operation, human=True, parents=parents)
            if prior is not None:
                self._append(db, prior.revision_id, prior.event.version + 1, MemoryState.SUPERSEDED, record)
            self._append(db, revision.revision_id, revision.event.version + 1, command.state, record)
            if command.state is MemoryState.APPROVED:
                db.execute("INSERT INTO tm_approvals VALUES (?, ?) ON CONFLICT(approval_slot) DO UPDATE "
                           "SET revision_id = excluded.revision_id", (_slot(revision.value), revision.revision_id))
            else:
                db.execute("DELETE FROM tm_approvals WHERE revision_id = ?", (revision.revision_id,))
            result = self._load(db, revision.revision_id, self._allowed)
            return self._remember(db, command, fingerprint, result)

    def get(self, revision_id: str, *, scopes: tuple[MemoryScope, ...]) -> MemoryRevision:
        identity(revision_id)
        self._scopes(scopes)
        with self._transaction() as db:
            return self._load(db, revision_id, scopes)

    def list_entries(self, *, scopes: tuple[MemoryScope, ...], limit=100, after=0,
                     state: MemoryState | None = None, entry_id: str | None = None) -> MemoryPage:
        self._scopes(scopes)
        _bounds(limit, after)
        if entry_id is not None:
            identity(entry_id)
        if state is not None and type(state) is not MemoryState:
            raise MemoryValidationError("invalid_state_filter")
        where = ['r.sequence > ?', '(' + ' OR '.join('(r.scope_kind = ? AND r.scope_owner = ?)' for _ in scopes) + ')']
        parameters = [after, *(part for scope in scopes for part in (scope.kind.value, scope.owner_id))]
        if state is not None:
            where.append('e.state = ?')
            parameters.append(state.value)
        if entry_id is not None:
            where.append('r.entry_id = ?')
            parameters.append(entry_id)
        with self._transaction() as db:
            rows = db.execute("SELECT r.revision_id, r.sequence FROM tm_revisions r JOIN tm_events e "
                              "ON e.revision_id = r.revision_id AND e.version = "
                              "(SELECT MAX(version) FROM tm_events WHERE revision_id = r.revision_id) WHERE "
                              + ' AND '.join(where) + " ORDER BY r.sequence LIMIT ?", (*parameters, limit + 1)).fetchall()
            revisions = tuple(self._load(db, row[0], scopes) for row in rows[:limit])
            return MemoryPage(revisions, rows[limit - 1][1] if len(rows) > limit else None)

    def history(self, revision_id: str, *, scopes: tuple[MemoryScope, ...], limit=100, after=0) -> tuple[MemoryEvent, ...]:
        identity(revision_id)
        self._scopes(scopes)
        _bounds(limit, after)
        with self._transaction() as db:
            self._load(db, revision_id, scopes)
            return tuple(self._event(row) for row in db.execute(
                "SELECT * FROM tm_events WHERE revision_id = ? AND version > ? ORDER BY version LIMIT ?",
                (revision_id, after, limit)).fetchall())

    def current(self, source: MemorySource, languages: LanguagePair, *, scopes: tuple[MemoryScope, ...]):
        self._scopes(scopes)
        validate_pair(languages)
        if type(source) is not MemorySource:
            raise MemoryValidationError("invalid_source")
        with self._transaction() as db:
            for scope in scopes:
                # Construct the identity without using either linguistic text or its search key.
                slot = _digest([scope.kind, scope.owner_id, source.project_id, source.unit_id, source.content_id,
                                languages.source, languages.target, PipelineStage.LINGUISTIC_FINAL])
                row = db.execute("SELECT revision_id FROM tm_approvals WHERE approval_slot = ?", (slot,)).fetchone()
                if row is not None:
                    revision = self._load(db, row[0], (scope,))
                    if revision.state is not MemoryState.APPROVED:
                        raise MemoryStorageError("invalid_current_approval")
                    return revision
        return None
