from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import logging
import sqlite3
from threading import Barrier
from uuid import UUID
import pytest
from ayvu.provenance.model import (
    ActorKind, ProvenanceConflictError, ProvenanceLimitError, ProvenanceNotFoundError, ProvenanceRecord,
    ProvenanceReferenceError, ProvenanceStoreError, ProvenanceValidationError, SourceKind, encode_record,
)
from ayvu.projects.schema import PROJECT_MIGRATIONS
from ayvu.provenance.store import MAX_LIST_LIMIT, MAX_TRAVERSAL_DEPTH, MAX_TRAVERSAL_RECORDS, SqliteProvenanceRepository
from tests.support.provenance_fakes import INPUT_FINGERPRINT, MACHINE, PROJECT, START, SyntheticLineage, subject
from ayvu.kernel.sqlite import SqliteMigrationRunner

MISSING = str(UUID(int=98))


OTHER_PROJECT = str(UUID(int=99))


BIG = dict(max_depth=64, max_records=1024)


@pytest.fixture
def env(tmp_path):
    return SyntheticLineage(tmp_path)


def raw(env, sql, *parameters, foreign_keys=False):
    with closing(sqlite3.connect(env.database)) as connection:
        if foreign_keys:
            connection.execute('PRAGMA foreign_keys=ON')
        rows = connection.execute(sql, parameters).fetchall()
        connection.commit()
        return rows


def counts(env):
    return tuple(raw(env, f'SELECT COUNT(*) FROM {table}')[0][0]
                 for table in ('provenance_records', 'provenance_parents', 'provenance_artifacts'))


def foreign_record(env, record_id=MISSING):
    """A row of another project, which only a damaged or tampered database can contain."""
    record = ProvenanceRecord(record_id, START, env.request(project_id=OTHER_PROJECT))
    raw(env, 'INSERT INTO provenance_records (record_id, schema_version, project_id, job_id, recorded_at, payload) '
             'VALUES (?, 1, ?, ?, ?, ?)', record_id, OTHER_PROJECT, env.job.job_id, START.isoformat(), encode_record(record))
    return record


def insert_cyclic_pair(env):
    first, second = str(UUID(int=71)), str(UUID(int=72))
    for record_id, parent in ((first, second), (second, first)):
        record = ProvenanceRecord(record_id, START, env.request(parents=(parent,)))
        raw(env, 'INSERT INTO provenance_records (record_id, schema_version, project_id, job_id, recorded_at, payload) '
                 'VALUES (?, 1, ?, ?, ?, ?)', record_id, PROJECT, env.job.job_id, START.isoformat(), encode_record(record))
    return first


def chain(env, length):
    record = env.append('step')
    for _ in range(length - 1):
        record = env.append('step', parents=(record.record_id,))
    return record


def _replace_payload(env, record_id, payload):
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER provenance_records_no_update')
        connection.execute('UPDATE provenance_records SET payload = ? WHERE record_id = ?', (payload, record_id))
        connection.commit()


CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub', 'Translate this prompt',
            'https://user:hunter2@example.test/translate', 'whale => baleia')
