"""Real SQLite adapters over a schema snapshot, independent of bootstrap tests.

Seeding the released registry lets transaction tests exercise runner.connect
even on hosts whose ancestor ownership prevents runner.migrate. Migration tests
separately require the real bootstrap; no path guard is patched or disabled.
"""
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
import sqlite3
from uuid import UUID, uuid4

from ayvu.approved_memory import (
    Candidate, MemoryScope, MemorySource, MemoryState, PipelinePin, PipelineStage, ScopeKind, TargetValue, ValueKind,
)
from ayvu.approved_memory_service import ApprovedMemory, CreateCandidate, DeriveRevision, ReviewRevision
from ayvu.approved_memory_store import SqliteApprovedMemoryRepository, memory_migration_runner, open_approved_memory
from ayvu.domain import LanguagePair
from ayvu.job import JobRequest
from ayvu.job_store import SqliteJobRepository
from ayvu.project_schema import project_migration_runner
from ayvu.provenance import ActorKind, ConfigurationRef, ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef
from ayvu.provenance_store import SqliteProvenanceRepository
from ayvu.sqlite_migrations import APPLICATION_ID
from content_fixtures import PROJECT, epub_document, unit

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
PAIR = LanguagePair('en', 'pt-br')
PROJECT_SCOPE = MemoryScope(ScopeKind.PROJECT, PROJECT)
WORK_SCOPE = MemoryScope(ScopeKind.WORK, str(UUID(int=42)))
USER_SCOPE = MemoryScope(ScopeKind.USER, str(UUID(int=43)))
IMPORT_SCOPE = MemoryScope(ScopeKind.IMPORT, str(UUID(int=44)))
SCOPES = (PROJECT_SCOPE, WORK_SCOPE, USER_SCOPE, IMPORT_SCOPE)
PIN = PipelinePin(*(ConfigurationRef(kind, kind + ':' + str(n) * 64)
                    for n, kind in enumerate(('pipeline', 'profile', 'glossary'), 1)))
PRODUCER = ProducerIdentity('not_applicable', 'not_applicable', 'epub@1', 'linguistic@1')


def document(text='Café  Hello', **kwargs):
    return epub_document(units=[unit('body/p[1]', text)], **kwargs)


def source(text='Café  Hello', **kwargs):
    doc = document(text, **kwargs)
    return MemorySource.from_document(doc, doc.units[0].unit_id)


def final(text='Olá', pin=PIN):
    return TargetValue(text, ValueKind.LINGUISTIC_FINAL, PipelineStage.LINGUISTIC_FINAL, pin)


def seed_schema(database, runner):
    with closing(sqlite3.connect(database)) as db:
        for step in runner.registry:
            if step.version == 1:
                db.execute('CREATE TABLE ayvu_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)')
            for sql in step.statements:
                db.execute(sql)
            db.execute('INSERT INTO ayvu_schema_migrations VALUES (?, ?)', (step.version, step.checksum))
        db.execute(f'PRAGMA application_id={APPLICATION_ID}')
        db.execute(f'PRAGMA user_version={len(runner.registry)}')
        db.commit()


class MemoryEnvironment:
    def __init__(self, root, *, migrate=False):
        self.root = root
        self.database = root / 'approved-memory.sqlite'
        self.project_database = root / 'project.sqlite'
        seed_schema(self.project_database, project_migration_runner())
        self.jobs = SqliteJobRepository(project_migration_runner(), self.project_database, PROJECT, clock=lambda: NOW)
        self.job = self.jobs.create(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'a' * 64))
        self.provenance = SqliteProvenanceRepository(project_migration_runner(), self.project_database, PROJECT,
                                                    clock=lambda: NOW)
        self.allowed = frozenset(SCOPES)
        self.source = source()
        if not migrate:
            seed_schema(self.database, memory_migration_runner())
        self.repo = self.open(migrate=migrate)
        self.app = ApprovedMemory(self.repo)

    def open(self, *, migrate=False, allowed=None):
        options = dict(allowed_scopes=self.allowed if allowed is None else allowed, provenance={PROJECT: self.provenance})
        if migrate:
            return open_approved_memory(self.root, **options)
        return SqliteApprovedMemoryRepository(memory_migration_runner(), self.database, **options)

    def evidence(self, operation, operation_id, value=None, *, parents=(), actor=ActorKind.HUMAN, **changes):
        value = value or Candidate(self.source, PAIR, final(), PROJECT_SCOPE, str(uuid4()))
        request = ProvenanceRequest(**dict(
            project_id=PROJECT, job_id=self.job.job_id, operation=operation, actor_kind=actor,
            source_kind=SourceKind.HUMAN if actor is ActorKind.HUMAN else SourceKind.PIPELINE,
            producer=PRODUCER, subject=SubjectRef('content_unit', 1, value.source.unit_id),
            configuration_refs=value.target.pin.references, parents=tuple(sorted(set(parents))),
            operation_id=operation_id,
        ) | changes)
        return self.provenance.append(request)

    def creation(self, *, value=None, scope=PROJECT_SCOPE, src=None, target=None, import_origin=None):
        operation_id = str(uuid4())
        value = value or Candidate(src or self.source, PAIR, target or final(), scope, str(uuid4()), import_origin)
        evidence = self.evidence('tm_create', operation_id, value,
                                 parents=(value.import_origin,) if value.import_origin else (), actor=ActorKind.SYSTEM)
        return CreateCandidate(operation_id, replace(value, provenance_id=evidence.record_id))

    def create(self, **kwargs):
        return self.repo.create(self.creation(**kwargs))

    def derivation(self, parent, *, target=None, scope=None, human=False):
        operation_id = str(uuid4())
        value = replace(parent.value, target=target or parent.value.target, scope=scope or parent.value.scope)
        parents = [parent.event.provenance_id]
        if value.import_origin:
            parents.append(value.import_origin)
        evidence = self.evidence('tm_edit' if human else 'tm_derive', operation_id, value, parents=parents)
        return DeriveRevision(operation_id, parent.revision_id, parent.event.version,
                              replace(value, provenance_id=evidence.record_id), human)

    def review(self, revision, state=MemoryState.APPROVED, *, prior=None, actor=ActorKind.HUMAN, **changes):
        operation_id = str(uuid4())
        parents = [revision.event.provenance_id]
        if prior:
            parents.append(prior.event.provenance_id)
        if revision.value.import_origin:
            parents.append(revision.value.import_origin)
        evidence = self.evidence({MemoryState.APPROVED: 'tm_approve', MemoryState.REJECTED: 'tm_reject',
                                  MemoryState.DEPRECATED: 'tm_deprecate'}[state], operation_id, revision.value,
                                 parents=parents, actor=actor)
        return replace(ReviewRevision(operation_id, revision.revision_id, revision.event.version,
                                       revision.value.scope, evidence.record_id, state,
                                       revision.value.target if state is MemoryState.APPROVED else None,
                                       prior.revision_id if prior else None), **changes)

    def approve(self, revision, **kwargs):
        return self.repo.review(self.review(revision, **kwargs))

    def raw(self, sql, parameters=()):
        with closing(sqlite3.connect(self.database)) as db:
            result = db.execute(sql, parameters).fetchall()
            db.commit()
            return result
